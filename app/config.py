import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"


def _release_metadata():
    """发布脚本写入的 data/version.json；不存在时返回空值走默认。"""
    try:
        payload = json.loads((DATA_DIR / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    return (
        str(payload.get("app_version") or "").strip(),
        str(payload.get("build_id") or "").strip(),
    )


_released_version, _released_build = _release_metadata()

# 发布版本：/ready 与 /api/stats 暴露，便于确认部署版本与数据 schema 是否同版
APP_VERSION = os.getenv("APP_VERSION", "").strip() or _released_version or "1.1.0"
BUILD_ID = os.getenv("BUILD_ID", "").strip() or _released_build or APP_VERSION
SCHEMA_VERSION = 3

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
# 全平台补抓只覆盖华语观看地区，避免把日韩美本地小平台的目录整库拉进来
_all_provider_regions = os.getenv("ALL_PROVIDER_WATCH_REGIONS", "")
ALL_PROVIDER_WATCH_REGIONS = tuple(
    region.strip().upper()
    for region in _all_provider_regions.split(",")
    if region.strip()
) or CHINESE_FOCUSED_REGIONS

# 加权评分（IMDb Top 250 同款贝叶斯先验）：只影响排序，不影响展示的原始分
RATING_PRIOR_VOTES = max(0, int(os.getenv("RATING_PRIOR_VOTES", "3000")))
RATING_PRIOR_MEAN = float(os.getenv("RATING_PRIOR_MEAN", "7.5"))

# 翻译源：google（默认）| deepl | none；none 时不翻译并在前端展示原文标签
TRANSLATE_PROVIDER = os.getenv("TRANSLATE_PROVIDER", "google").strip().lower() or "google"
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "").strip()
DEEPL_API_URL = os.getenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate").strip()

# 评分和抓取策略
MIN_IMDB_RATING = float(os.getenv("MIN_IMDB_RATING", "7.0"))
MIN_IMDB_VOTES = int(os.getenv("MIN_IMDB_VOTES", "1000"))
OMDB_MIN_VOTES = int(os.getenv("OMDB_MIN_VOTES", "100"))
# 新剧宽限期：首播 ≤N 天内放宽 votes 门槛，避免新剧因票数不足被 pending
NEW_TITLE_GRACE_DAYS = int(os.getenv("NEW_TITLE_GRACE_DAYS", "30"))
MIN_IMDB_VOTES_GRACE = int(os.getenv("MIN_IMDB_VOTES_GRACE", "20"))
# 历史窗口（超过宽限期）的平台发现预过滤：TMDB 票数低于该值的冷门目录不再抓取，0 关闭
DISCOVER_MIN_VOTE_COUNT = int(os.getenv("DISCOVER_MIN_VOTE_COUNT", "100"))
# 亚洲产地（中/台/港/韩/日/泰/越）内容票数门槛放宽，避免漏掉低票数亚洲剧集；且默认不受"仅其他平台"限制
_asian_origins = os.getenv("ASIAN_ORIGIN_COUNTRIES", "CN,TW,HK,KR,JP,TH,VN")
ASIAN_ORIGIN_COUNTRIES = tuple(
    code.strip().upper() for code in _asian_origins.split(",") if code.strip()
) or ("CN", "TW", "HK", "KR", "JP", "TH", "VN")
ASIAN_MIN_IMDB_VOTES = int(os.getenv("ASIAN_MIN_IMDB_VOTES", "100"))
ENRICH_CONCURRENCY = int(os.getenv("ENRICH_CONCURRENCY", "30"))
ENRICH_BATCH_SIZE = max(1, int(os.getenv("ENRICH_BATCH_SIZE", "200")))
DISCOVER_CONCURRENCY = int(os.getenv("DISCOVER_CONCURRENCY", "10"))
# 只统计真正的可看渠道；默认仅订阅制 flatrate，避免 rent/buy 把"可租可买"的冷门目录算成流媒体
WATCH_MONETIZATION_TYPES = os.getenv(
    "WATCH_MONETIZATION_TYPES", "flatrate"
).replace(",", "|")
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
DETAIL_REFRESH_DAYS = int(os.getenv("DETAIL_REFRESH_DAYS", "7"))
PENDING_RETRY_DAYS = tuple(
    int(value)
    for value in os.getenv("PENDING_RETRY_DAYS", "1,3,7,14,30").split(",")
    if value.strip()
)
PROVIDER_STALE_DAYS = int(os.getenv("PROVIDER_STALE_DAYS", "45"))
# 说明（7.5）：渠道失效不再按时钟全局过期（会误停用未被本轮同步覆盖的老片）。
# 该配置仅为兼容旧 .env 保留；否定结论由 backfill_channels 单片核验成功后发布。

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
# 历史补偿扫描：默认只回看 1 年，冷门老片不再无限回填
SYNC_CATALOG_SCAN_DAYS_BACK = int(os.getenv("SYNC_CATALOG_SCAN_DAYS_BACK", "365"))
SYNC_CATALOG_WINDOW_DAYS = int(os.getenv("SYNC_CATALOG_WINDOW_DAYS", "365"))
SYNC_BOOTSTRAP_ON_EMPTY = os.getenv("SYNC_BOOTSTRAP_ON_EMPTY", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
SYNC_BOOTSTRAP_DAYS_BACK = int(os.getenv("SYNC_BOOTSTRAP_DAYS_BACK", "365"))
SYNC_BOOTSTRAP_MAX_PAGES = int(os.getenv("SYNC_BOOTSTRAP_MAX_PAGES", "10"))
