"""IMDb 官方数据集加载 —— 免 API Key、无限流、全量"""

import gzip
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from app.config import DATA_DIR

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
CACHE_DIR = DATA_DIR
CACHE_FILE = CACHE_DIR / "title.ratings.tsv"
CACHE_MAX_AGE_HOURS = 24

_ratings = None  # imdb_id -> (rating, votes)


def _ensure_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _download():
    gz_path = Path(f"{CACHE_FILE}.gz")
    print(f"  Downloading IMDb dataset...")
    urllib.request.urlretrieve(DATASET_URL, str(gz_path))
    with gzip.open(gz_path, "rb") as f_in:
        with open(CACHE_FILE, "wb") as f_out:
            f_out.write(f_in.read())
    gz_path.unlink(missing_ok=True)
    print(f"  Downloaded and extracted.")


def _cache_valid():
    if not CACHE_FILE.exists():
        return False
    mtime = datetime.fromtimestamp(CACHE_FILE.stat().st_mtime)
    return (datetime.now() - mtime) < timedelta(hours=CACHE_MAX_AGE_HOURS)


def load_ratings(force=False):
    global _ratings
    if _ratings is not None and not force:
        return _ratings

    _ensure_dir()

    if not _cache_valid() or force:
        _download()

    print("  Parsing IMDb ratings...")
    _ratings = {}
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        header = f.readline()  # skip header
        for line in f:
            parts = line.strip().split('\t')
            if len(parts) >= 3:
                imdb_id = parts[0]           # e.g. tt0111161
                rating = float(parts[1])      # e.g. 7.6
                votes = int(parts[2])          # e.g. 828114
                _ratings[imdb_id] = (rating, votes)

    print(f"  Loaded {len(_ratings):,} IMDb ratings.")
    return _ratings


def get_rating(imdb_id):
    """获取 IMDb 评分 (rating, votes)，无数据返回 (None, 0)"""
    ratings = load_ratings()
    return ratings.get(imdb_id, (None, 0))
