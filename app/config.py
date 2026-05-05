import os
from dotenv import load_dotenv

load_dotenv()

# 项目根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# TMDB API配置
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_BASE_URL = "https://api.themoviedb.org/3"

# OMDB API配置（用于获取IMDb评分）
OMDB_API_KEY = os.getenv("OMDB_API_KEY", "")
OMDB_BASE_URL = "https://www.omdbapi.com/"

# 数据库配置（使用绝对路径）
_db_url = os.getenv("DATABASE_URL", "")
if not _db_url:
    DATABASE_URL = os.path.join(BASE_DIR, "data", "tracker.db")
elif not os.path.isabs(_db_url):
    DATABASE_URL = os.path.join(BASE_DIR, _db_url)
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

# 定时任务配置
SCHEDULE_HOUR = 8  # 每天8点运行
