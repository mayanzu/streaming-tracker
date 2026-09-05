"""与业务无关的通用工具：时间戳、评分/国家代码归一化、重试间隔。"""

from datetime import datetime, timedelta, timezone

from app.config import MIN_IMDB_RATING, PENDING_RETRY_DAYS
from app.db.connection import TRUSTED_RATING_SOURCES


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _normalize_rating_source(title_data):
    source = title_data.get("rating_source")
    if source not in TRUSTED_RATING_SOURCES:
        return None, None, None

    rating = title_data.get("imdb_rating")
    if rating is None:
        return None, None, None

    rating = float(rating)
    if rating < MIN_IMDB_RATING:
        return None, None, None

    return rating, source, title_data.get("rating_votes")


def _normalize_country_codes(values):
    codes = []
    for value in values or []:
        code = value.get("iso_3166_1") if isinstance(value, dict) else value
        code = str(code or "").strip().upper()
        if len(code) == 2 and code.isalpha() and code not in codes:
            codes.append(code)
    return codes


def _retry_delay_days(reason, attempt_count):
    """返回下次重试间隔（天）；超过上限返回 None 表示归档（一年后再看）。

    low_rating/missing_imdb_id 这类极少翻盘的，6 次后归档，避免 4 万积压每
    30 天空转一次最贵的 TMDB 详情调用。手动 import 与达标入库仍会清掉 pending。
    """
    if reason == "low_rating":
        if attempt_count > 6:
            return None
        return max(PENDING_RETRY_DAYS[-1] if PENDING_RETRY_DAYS else 30, 30)
    if reason == "awaiting_rating":
        schedule = (7, 14, 30)
        if attempt_count > 8:
            return None
        return schedule[min(max(attempt_count - 1, 0), len(schedule) - 1)]
    schedule = PENDING_RETRY_DAYS or (1, 3, 7, 14, 30)
    if attempt_count > 8:
        return None
    return schedule[min(max(attempt_count - 1, 0), len(schedule) - 1)]


ARCHIVE_RETRY_DAYS = 365


def stale_before(days):
    """计算 N 天前的 UTC 时间戳，用于过期判定。"""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
