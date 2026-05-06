"""IMDb 官方数据集加载 —— 免 API Key、无限流、全量"""

import gzip
import os
import urllib.request
from datetime import datetime, timedelta

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CACHE_FILE = os.path.join(CACHE_DIR, "title.ratings.tsv")
CACHE_MAX_AGE_HOURS = 24

_ratings = None  # imdb_id -> (rating, votes)


def _ensure_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _download():
    gz_path = CACHE_FILE + ".gz"
    print(f"  Downloading IMDb dataset...")
    urllib.request.urlretrieve(DATASET_URL, gz_path)
    with gzip.open(gz_path, 'rb') as f_in:
        with open(CACHE_FILE, 'wb') as f_out:
            f_out.write(f_in.read())
    os.remove(gz_path)
    print(f"  Downloaded and extracted.")


def _cache_valid():
    if not os.path.exists(CACHE_FILE):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(CACHE_FILE))
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
    with open(CACHE_FILE, 'r', encoding='utf-8') as f:
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
