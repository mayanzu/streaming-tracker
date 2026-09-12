"""fetcher 包共享工具：正则、翻译、海报、国家代码、日期窗口、候选合并、统计。"""

import asyncio
import logging
import random
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache

from app.config import (
    ASIAN_MIN_IMDB_VOTES,
    ASIAN_ORIGIN_COUNTRIES,
    DETAIL_REFRESH_DAYS,
    MIN_IMDB_RATING,
    MIN_IMDB_VOTES,
    MIN_IMDB_VOTES_GRACE,
    NEW_TITLE_GRACE_DAYS,
    PROVIDERS,
    WATCH_MONETIZATION_TYPES,
)
from app.fetcher.translate import translate_sync

logger = logging.getLogger(__name__)
TRUSTED_RATING_SOURCES = {"imdb", "omdb"}
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
IMDB_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])(tt\d{7,12})(?!\d)", re.IGNORECASE)
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
WATCH_PROVIDER_FIELDS = ("flatrate", "ads", "free", "rent", "buy")
# 只把配置中的可看渠道计入平台可用性；默认 flatrate，避免 rent/buy 把"可租可买"算成"流媒体可看"
MONETIZATION_FIELDS = tuple(
    field for field in WATCH_MONETIZATION_TYPES.split("|") if field in WATCH_PROVIDER_FIELDS
) or WATCH_PROVIDER_FIELDS
PRIMARY_PROVIDER_ALIASES = {
    "netflix": "netflix",
    "disney plus": "disney",
    "disney+": "disney",
    "max": "max",
    "hbo max": "max",
    "amazon prime video": "amazon",
    "prime video": "amazon",
    "apple tv plus": "apple",
    "apple tv+": "apple",
    "hulu": "hulu",
}


class ExternalRequestError(RuntimeError):
    def __init__(self, service, message, status_code=None):
        super().__init__(message)
        self.service = service
        self.status_code = status_code


def normalize_imdb_id(value):
    """Accept a bare IMDb ID or an IMDb title URL and return a canonical ID."""
    match = IMDB_ID_PATTERN.search(str(value or "").strip())
    if not match:
        raise ValueError("invalid IMDb title ID or URL")
    return match.group(1).lower()


def _contains_cjk(text):
    """判断文本是否含中文字符；TMDB 无中文译文时字段会回退原文，需要据此兜底。"""
    return bool(CJK_PATTERN.search(str(text or "")))


def _cjk_ratio(text):
    """CJK 字符占非空白字符比例；用于识别中英混排/机翻不完整的简介。"""
    value = str(text or "")
    chars = [char for char in value if not char.isspace()]
    if not chars:
        return 0.0
    return len(CJK_PATTERN.findall(value)) / len(chars)


def _looks_chinese(text, min_ratio=0.3):
    """D04：二值 _contains_cjk 会把含一句中文的英文简介判为已中文化；
    按 CJK 占比判断，只有达到阈值才认为无需翻译。"""
    return _cjk_ratio(text) >= min_ratio


def _zh_quality(title, original_title, overview, overview_translated=False):
    """区分中文资料质量，避免 has_zh 布尔值承担全部质量承诺。

    返回：full（标题与简介均为中文）、machine（简介为机器翻译）、
    overview_only（仅简介中文）、title_only（仅片名中文）、none（暂无中文）。
    """
    if overview_translated:
        return "machine"
    title_zh = _contains_cjk(title) or _contains_cjk(original_title)
    overview_zh = _contains_cjk(overview)
    if title_zh and overview_zh:
        return "full"
    if overview_zh:
        return "overview_only"
    if title_zh:
        return "title_only"
    return "none"


def _release_days_since(release_date):
    """距上映/首播的天数；未来日期为负，空值或非法日期返回 None。"""
    if not release_date:
        return None
    try:
        return (date.today() - date.fromisoformat(str(release_date)[:10])).days
    except (TypeError, ValueError):
        return None


def _is_in_grace_period(title, days=NEW_TITLE_GRACE_DAYS):
    """新剧（已上映且首播 ≤days 天）；未来上映日期不算宽限期。"""
    elapsed = _release_days_since((title or {}).get("release_date"))
    return elapsed is not None and 0 <= elapsed <= days


def _is_recent(title, days=60):
    """首播 N 天内：可能是 IMDb 还没开分，值得比 missing_rating 更积极地重试。"""
    elapsed = _release_days_since((title or {}).get("release_date"))
    return elapsed is not None and 0 <= elapsed <= days


def _min_votes_for(title):
    """votes 门槛：新剧宽限 > 亚洲产地放宽 > 默认。"""
    if _is_in_grace_period(title):
        return MIN_IMDB_VOTES_GRACE
    countries = set(_normalize_country_codes((title or {}).get("origin_countries")))
    if countries & set(ASIAN_ORIGIN_COUNTRIES):
        return ASIAN_MIN_IMDB_VOTES
    return MIN_IMDB_VOTES


_TRANSLATION_ERROR_PATTERN = re.compile(
    r"<html|<!doctype|error\s+5\d\d|server error|that['’]s an error"
    r"|please try again later|we['’]re sorry|unusual traffic",
    re.IGNORECASE,
)


def _valid_translation(result):
    """Google 偶发返回错误页正文，不能当译文写库。"""
    if not result:
        return False
    return not _TRANSLATION_ERROR_PATTERN.search(result)


@lru_cache(maxsize=5000)
def _translate_cached(text: str) -> str:
    result = translate_sync(text)
    if result and result != text and not _valid_translation(result):
        raise ValueError("translation provider returned an error page")
    return result


def _localized_poster_path(details):
    posters = (details.get("images") or {}).get("posters") or []
    if not posters:
        return details.get("poster_path")

    for language in ("zh", None, "en"):
        candidates = [
            poster for poster in posters
            if poster.get("iso_639_1") == language and poster.get("file_path")
        ]
        if candidates:
            return max(candidates, key=lambda item: item.get("vote_average") or 0)["file_path"]
    return details.get("poster_path") or posters[0].get("file_path")


def _poster_url(path):
    return f"https://image.tmdb.org/t/p/w500{path}" if path else None


def _normalize_country_codes(values):
    codes = []
    for value in values or []:
        code = value.get("iso_3166_1") if isinstance(value, dict) else value
        code = str(code or "").strip().upper()
        if len(code) == 2 and code.isalpha() and code not in codes:
            codes.append(code)
    return codes


def _origin_countries_from_details(details):
    countries = _normalize_country_codes(details.get("origin_country"))
    if countries:
        return countries
    return _normalize_country_codes(details.get("production_countries"))


async def translate_to_chinese(text):
    if not text:
        return text
    try:
        return await asyncio.to_thread(_translate_cached, text[:800])
    except Exception:
        # 失败结果不缓存，下次同步/回填会重试
        return text


def _retry_delay(response, attempt):
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, min((retry_at - datetime.now(timezone.utc)).total_seconds(), 60.0))
            except (TypeError, ValueError):
                pass
    return min(2 ** attempt + random.uniform(0.0, 0.75), 30.0)


def _date_ranges(days_back, window_days, end_date=None):
    end = end_date or date.today()
    start = end - timedelta(days=max(days_back, 0))
    if window_days <= 0 or window_days >= days_back:
        return [(start, end)]
    ranges = []
    cursor = end
    while cursor >= start:
        window_start = max(start, cursor - timedelta(days=window_days - 1))
        ranges.append((window_start, cursor))
        cursor = window_start - timedelta(days=1)
    return ranges


def _merge_candidate(target, incoming):
    target["providers"] = list(dict.fromkeys((target.get("providers") or []) + (incoming.get("providers") or [])))
    regions = target.setdefault("provider_regions", {})
    for provider, values in (incoming.get("provider_regions") or {}).items():
        regions[provider] = list(dict.fromkeys((regions.get(provider) or []) + values))
    labels = target.setdefault("provider_labels", {})
    for provider, values in (incoming.get("provider_labels") or {}).items():
        labels[provider] = list(dict.fromkeys((labels.get(provider) or []) + values))
    target["discovery_channels"] = list(dict.fromkeys(
        (target.get("discovery_channels") or []) + (incoming.get("discovery_channels") or [])
    ))
    target["origin_countries"] = list(dict.fromkeys(
        (target.get("origin_countries") or []) + (incoming.get("origin_countries") or [])
    ))
    for field in ("title", "original_title", "overview", "release_date", "poster_url"):
        if not target.get(field) and incoming.get(field):
            target[field] = incoming[field]
    return target


def _candidate_from_item(item, media_type, provider_name, region, channel):
    candidate = _base_candidate_from_item(item, media_type, channel)
    if provider_name:
        candidate["providers"] = [provider_name]
        candidate["provider_regions"] = {provider_name: [region]}
    return candidate


def _base_candidate_from_item(item, media_type, channel):
    return {
        "tmdb_id": item["id"],
        "title": item.get("title") or item.get("name") or "",
        "original_title": item.get("original_title") or item.get("original_name") or "",
        "type": media_type,
        "overview": item.get("overview") or "",
        "release_date": item.get("release_date") or item.get("first_air_date") or "",
        "poster_url": _poster_url(item.get("poster_path")),
        "imdb_rating": None,
        "rating_source": None,
        "rating_votes": None,
        "added_date": date.today().isoformat(),
        "providers": [],
        "provider_regions": {},
        "discovery_channels": [channel],
        "origin_countries": _normalize_country_codes(item.get("origin_country")),
    }


def _provider_availability(payload):
    """Collapse TMDB watch offers into six primary providers plus ``others``.

    同时保留原始展示名（provider_labels：内部 key -> 去重后的 TMDB
    provider_name 列表），供详情页展示“其他平台具体是哪个平台”。
    """
    provider_by_id = {provider_id: name for name, provider_id in PROVIDERS.items()}
    providers = []
    provider_regions = {}
    provider_labels = {}
    for region, offers in (payload.get("results") or {}).items():
        for field in MONETIZATION_FIELDS:
            for offer in offers.get(field) or []:
                provider_id = offer.get("provider_id")
                provider_name = provider_by_id.get(provider_id)
                display_name = str(offer.get("provider_name") or "").strip()
                if not provider_name:
                    provider_name = PRIMARY_PROVIDER_ALIASES.get(display_name.casefold(), "others")
                if provider_name not in providers:
                    providers.append(provider_name)
                regions = provider_regions.setdefault(provider_name, [])
                if region not in regions:
                    regions.append(region)
                if display_name:
                    labels = provider_labels.setdefault(provider_name, [])
                    if display_name not in labels:
                        labels.append(display_name)
    return providers, provider_regions, provider_labels


def _provider_offers(payload):
    """R02：逐地区结构化 offer（平台 ID/名称 × 地区 × 观看方式）。

    展示用 offer 覆盖订阅/租赁/购买等全部方式；可见性判断仍使用
    MONETIZATION_FIELDS 的配置，两者互不影响。
    """
    provider_by_id = {provider_id: name for name, provider_id in PROVIDERS.items()}
    offers = []
    seen = set()
    for region, region_offers in (payload.get("results") or {}).items():
        region = str(region or "").strip().upper()
        if len(region) != 2:
            continue
        for field in WATCH_PROVIDER_FIELDS:
            for offer in region_offers.get(field) or []:
                display_name = str(offer.get("provider_name") or "").strip()
                try:
                    provider_id = int(offer.get("provider_id"))
                except (TypeError, ValueError):
                    continue
                if not display_name:
                    continue
                group = provider_by_id.get(provider_id)
                if not group:
                    group = PRIMARY_PROVIDER_ALIASES.get(display_name.casefold(), "others")
                key = (provider_id, display_name, region, field)
                if key in seen:
                    continue
                seen.add(key)
                offers.append({
                    "provider_id": provider_id,
                    "provider_name": display_name,
                    "provider_group": group,
                    "region": region,
                    "monetization": field,
                })
    return offers


def _is_fresh(cached):
    if (
        not cached
        or cached.get("rating_source") not in TRUSTED_RATING_SOURCES
        or cached.get("imdb_rating") is None
        or float(cached["imdb_rating"]) < MIN_IMDB_RATING
        or (cached.get("rating_votes") or 0) < _min_votes_for(cached)
        or not _contains_cjk(cached.get("title"))
        or not _contains_cjk(cached.get("overview"))
        or not cached.get("countries_synced_at")
    ):
        return False
    value = cached.get("last_synced_at") if cached else None
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - parsed < timedelta(days=DETAIL_REFRESH_DAYS)
    except (TypeError, ValueError):
        return False


def _cached_title(candidate, cached):
    result = dict(cached)
    result["providers"] = candidate.get("providers") or []
    result["provider_regions"] = candidate.get("provider_regions") or {}
    result["provider_labels"] = candidate.get("provider_labels") or {}
    result["discovery_channels"] = candidate.get("discovery_channels") or []
    result["added_date"] = cached.get("added_date") or candidate.get("added_date")
    result["last_seen_at"] = datetime.now(timezone.utc).isoformat()
    return result


def empty_fetch_stats():
    return {
        "discovered": 0,
        "unique_discovered": 0,
        "cached": 0,
        "qualified": 0,
        "pending": 0,
        "no_rating": 0,
        "low_rating": 0,
        "request_failed": 0,
        "errors": [],
    }


def merge_fetch_stats(total, partial):
    for key in (
        "discovered", "unique_discovered", "cached", "qualified", "pending",
        "no_rating", "low_rating", "request_failed",
    ):
        total[key] = total.get(key, 0) + partial.get(key, 0)
    total.setdefault("errors", []).extend(partial.get("errors", []))


async def _notify_progress(callback, **payload):
    if not callback:
        return
    result = callback(payload)
    if asyncio.iscoroutine(result):
        await result
