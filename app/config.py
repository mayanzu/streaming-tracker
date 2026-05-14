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

# 评分和抓取策略
MIN_IMDB_RATING = float(os.getenv("MIN_IMDB_RATING", "7.0"))
MIN_IMDB_VOTES = int(os.getenv("MIN_IMDB_VOTES", "50"))
OMDB_MIN_VOTES = int(os.getenv("OMDB_MIN_VOTES", "100"))
TMDB_FALLBACK_MIN_VOTES = int(os.getenv("TMDB_FALLBACK_MIN_VOTES", "5"))
ENRICH_CONCURRENCY = int(os.getenv("ENRICH_CONCURRENCY", "30"))

# 本地同步上传目标。示例：root@192.168.1.2:/app/data/tracker.db
ROUTER_TARGET = os.getenv("ROUTER_TARGET", "")

# 网站进程内自动同步配置
SYNC_ENABLED = os.getenv("SYNC_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SYNC_HOUR = int(os.getenv("SYNC_HOUR", "6"))
SYNC_MINUTE = int(os.getenv("SYNC_MINUTE", "0"))
SYNC_TIMEZONE = os.getenv("SYNC_TIMEZONE", "Asia/Shanghai")
SYNC_DAYS_BACK = int(os.getenv("SYNC_DAYS_BACK", "30"))
SYNC_MAX_PAGES = int(os.getenv("SYNC_MAX_PAGES", "5"))
