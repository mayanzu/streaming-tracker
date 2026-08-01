"""fetcher 包共享工具：正则、翻译、海报、国家代码、日期窗口、候选合并、统计。"""

import asyncio
import logging
import random
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache

from deep_translator import GoogleTranslator

from app.config import DETAIL_REFRESH_DAYS, MIN_IMDB_RATING, PROVIDERS

logger = logging.getLogger(__name__)
TRUSTED_RATING_SOURCES = {"imdb", "omdb"}
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
IMDB_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])(tt\d{7,12})(?!\d)", re.IGNORECASE)
WATCH_PROVIDER_FIELDS = ("flatrate", "ads", "free", "rent", "buy")
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


@lru_cache(maxsize=5000)
def _translate_cached(text: str) -> str:
    try:
        result = GoogleTranslator(source="en", target="zh-CN").translate(text[:800])
        return result if result else text
    except Exception:
        return text


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
    return await asyncio.to_thread(_translate_cached, text[:800])


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
    """Collapse TMDB watch offers into six primary providers plus ``others``."""
    provider_by_id = {provider_id: name for name, provider_id in PROVIDERS.items()}
    providers = []
    provider_regions = {}
    for region, offers in (payload.get("results") or {}).items():
        for field in WATCH_PROVIDER_FIELDS:
            for offer in offers.get(field) or []:
                provider_id = offer.get("provider_id")
                provider_name = provider_by_id.get(provider_id)
                if not provider_name:
                    display_name = str(offer.get("provider_name") or "").strip().casefold()
                    provider_name = PRIMARY_PROVIDER_ALIASES.get(display_name, "others")
                if provider_name not in providers:
                    providers.append(provider_name)
                regions = provider_regions.setdefault(provider_name, [])
                if region not in regions:
                    regions.append(region)
    return providers, provider_regions


def _is_fresh(cached):
    if (
        not cached
        or cached.get("rating_source") not in TRUSTED_RATING_SOURCES
        or cached.get("imdb_rating") is None
        or float(cached["imdb_rating"]) < MIN_IMDB_RATING
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
