import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"

# TMDB API配置
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_BASE_URL = "https://api.themoviedb.org/3"

# OMDB API配置（用于获取IMDb评分）
OMDB_API_KEY = os.getenv("OMDB_API_KEY", "")
OMDB_BASE_URL = "https://www.omdbapi.com/"

# 数据库配置（使用绝对路径）
_db_url = os.getenv("DATABASE_URL", "")
if not _db_url:
    DATABASE_URL = str(DATA_DIR / "tracker.db")
elif not os.path.isabs(_db_url):
    DATABASE_URL = str(BASE_DIR / _db_url)
else:
    DATABASE_URL = _db_url

# 平台ID映射 (TMDB Provider IDs)
PROVIDERS = {
    "netflix": 8,
    "disney": 337,
    "max": 1899,
    "amazon": 9,
    "apple": 350,
    "hulu": 15,
}

MAIN_FILTER_PROVIDERS = (*PROVIDERS, "others")

CHINESE_FOCUSED_REGIONS = ("TW", "HK", "SG", "MY")
GLOBAL_DISCOVERY_REGIONS = (*CHINESE_FOCUSED_REGIONS, "JP", "KR", "US", "GB", "CA", "AU")

DEFAULT_PROVIDER_REGIONS = {
    "netflix": GLOBAL_DISCOVERY_REGIONS,
    "disney": GLOBAL_DISCOVERY_REGIONS,
    "max": GLOBAL_DISCOVERY_REGIONS,
    "amazon": GLOBAL_DISCOVERY_REGIONS,
    "apple": GLOBAL_DISCOVERY_REGIONS,
    "hulu": GLOBAL_DISCOVERY_REGIONS,
}


def _provider_regions(provider_name):
    value = os.getenv(f"{provider_name.upper()}_WATCH_REGIONS", "")
    if not value:
        return DEFAULT_PROVIDER_REGIONS[provider_name]
    regions = tuple(region.strip().upper() for region in value.split(",") if region.strip())
    return regions or DEFAULT_PROVIDER_REGIONS[provider_name]


PROVIDER_REGIONS = {
    provider_name: _provider_regions(provider_name)
    for provider_name in PROVIDERS
}

DISCOVER_ALL_PROVIDERS = os.getenv("DISCOVER_ALL_PROVIDERS", "true").lower() in (
    "1", "true", "yes", "on",
)
_all_provider_regions = os.getenv("ALL_PROVIDER_WATCH_REGIONS", "")
ALL_PROVIDER_WATCH_REGIONS = tuple(
    region.strip().upper()
    for region in _all_provider_regions.split(",")
    if region.strip()
) or GLOBAL_DISCOVERY_REGIONS

# 评分和抓取策略
MIN_IMDB_RATING = float(os.getenv("MIN_IMDB_RATING", "7.0"))
MIN_IMDB_VOTES = int(os.getenv("MIN_IMDB_VOTES", "50"))
OMDB_MIN_VOTES = int(os.getenv("OMDB_MIN_VOTES", "100"))
# 新剧宽限期：首播 ≤N 天内放宽 votes 门槛，避免新剧因票数不足被 pending
NEW_TITLE_GRACE_DAYS = int(os.getenv("NEW_TITLE_GRACE_DAYS", "30"))
MIN_IMDB_VOTES_GRACE = int(os.getenv("MIN_IMDB_VOTES_GRACE", "5"))
ENRICH_CONCURRENCY = int(os.getenv("ENRICH_CONCURRENCY", "30"))
ENRICH_BATCH_SIZE = max(1, int(os.getenv("ENRICH_BATCH_SIZE", "200")))
DISCOVER_CONCURRENCY = int(os.getenv("DISCOVER_CONCURRENCY", "10"))
WATCH_MONETIZATION_TYPES = os.getenv(
    "WATCH_MONETIZATION_TYPES", "flatrate,ads,free,rent,buy"
).replace(",", "|")
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
DETAIL_REFRESH_DAYS = int(os.getenv("DETAIL_REFRESH_DAYS", "7"))
PENDING_RETRY_DAYS = tuple(
    int(value)
    for value in os.getenv("PENDING_RETRY_DAYS", "1,3,7,14,30").split(",")
    if value.strip()
)
PROVIDER_STALE_DAYS = int(os.getenv("PROVIDER_STALE_DAYS", "45"))

# 网站进程内自动同步配置
SYNC_ENABLED = os.getenv("SYNC_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SYNC_HOUR = int(os.getenv("SYNC_HOUR", "6"))
SYNC_MINUTE = int(os.getenv("SYNC_MINUTE", "0"))
SYNC_TIMEZONE = os.getenv("SYNC_TIMEZONE", "Asia/Shanghai")
SYNC_DAYS_BACK = int(os.getenv("SYNC_DAYS_BACK", "30"))
SYNC_MAX_PAGES = int(os.getenv("SYNC_MAX_PAGES", "5"))
SYNC_WINDOW_DAYS = int(os.getenv("SYNC_WINDOW_DAYS", "0"))
SYNC_INCREMENTAL_OVERLAP_DAYS = int(os.getenv("SYNC_INCREMENTAL_OVERLAP_DAYS", "3"))
SYNC_CATALOG_SCAN_ENABLED = os.getenv("SYNC_CATALOG_SCAN_ENABLED", "true").lower() in (
    "1", "true", "yes", "on",
)
SYNC_CATALOG_SCAN_DAYS_BACK = int(os.getenv("SYNC_CATALOG_SCAN_DAYS_BACK", "3650"))
SYNC_CATALOG_WINDOW_DAYS = int(os.getenv("SYNC_CATALOG_WINDOW_DAYS", "365"))
SYNC_BOOTSTRAP_ON_EMPTY = os.getenv("SYNC_BOOTSTRAP_ON_EMPTY", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
SYNC_BOOTSTRAP_DAYS_BACK = int(os.getenv("SYNC_BOOTSTRAP_DAYS_BACK", "1825"))
SYNC_BOOTSTRAP_MAX_PAGES = int(os.getenv("SYNC_BOOTSTRAP_MAX_PAGES", "29"))
