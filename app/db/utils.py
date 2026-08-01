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
    if reason == "low_rating":
        return max(PENDING_RETRY_DAYS[-1] if PENDING_RETRY_DAYS else 30, 30)
    schedule = PENDING_RETRY_DAYS or (1, 3, 7, 14, 30)
    return schedule[min(max(attempt_count - 1, 0), len(schedule) - 1)]


def stale_before(days):
    """计算 N 天前的 UTC 时间戳，用于过期判定。"""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
