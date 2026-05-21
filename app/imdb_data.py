"""IMDb 官方数据集加载 —— 免 API Key、无限流、全量"""

import asyncio
import gzip
import logging
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from app.config import DATA_DIR

logger = logging.getLogger(__name__)

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
CACHE_DIR = DATA_DIR
CACHE_FILE = CACHE_DIR / "title.ratings.tsv"
CACHE_MAX_AGE_HOURS = 24
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_DELAY = 5  # seconds

_ratings = None  # imdb_id -> (rating, votes)
_lock = threading.RLock()  # 使用可重入锁，支持双重检查


def _ensure_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _download():
    gz_path = Path(f"{CACHE_FILE}.gz")
    last_error = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            logger.info("Downloading IMDb dataset (attempt %s/%s)...", attempt, DOWNLOAD_RETRIES)
            import httpx
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                with client.stream("GET", DATASET_URL) as r:
                    r.raise_for_status()
                    with open(gz_path, "wb") as f:
                        for chunk in r.iter_bytes(chunk_size=8192):
                            f.write(chunk)
            with gzip.open(gz_path, "rb") as f_in:
                with open(CACHE_FILE, "wb") as f_out:
                    f_out.write(f_in.read())
            gz_path.unlink(missing_ok=True)
            logger.info("IMDb dataset downloaded and extracted")
            return
        except Exception as exc:
            last_error = exc
            gz_path.unlink(missing_ok=True)
            if attempt < DOWNLOAD_RETRIES:
                logger.warning(
                    "IMDb download failed (attempt %s/%s), retrying in %ss: %s",
                    attempt,
                    DOWNLOAD_RETRIES,
                    DOWNLOAD_RETRY_DELAY,
                    exc,
                )
                time.sleep(DOWNLOAD_RETRY_DELAY)
            else:
                logger.error("IMDb download failed after %s attempts: %s", DOWNLOAD_RETRIES, exc)

    raise RuntimeError(
        f"Failed to download IMDb dataset after {DOWNLOAD_RETRIES} attempts"
    ) from last_error


def _cache_valid():
    if not CACHE_FILE.exists():
        return False
    mtime = datetime.fromtimestamp(CACHE_FILE.stat().st_mtime)
    return (datetime.now() - mtime) < timedelta(hours=CACHE_MAX_AGE_HOURS)


def _load_ratings_sync(force=False):
    """同步加载 IMDb 评分到内存（内部使用，通过 asyncio.to_thread 调用）。"""
    global _ratings

    with _lock:
        if _ratings is not None and not force:
            return _ratings

        _ensure_dir()

        if not _cache_valid() or force:
            _download()

        logger.info("Parsing IMDb ratings...")
        ratings = {}
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            f.readline()  # skip header
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) >= 3:
                    imdb_id = parts[0]
                    try:
                        rating = float(parts[1])
                        votes = int(parts[2])
                    except (ValueError, TypeError):
                        continue
                    ratings[imdb_id] = (rating, votes)

        _ratings = ratings
        logger.info("Loaded %s IMDb ratings", f"{len(_ratings):,}")

    return _ratings


async def load_ratings(force=False):
    """异步加载 IMDb 评分，使用双重检查锁避免竞态条件。"""
    with _lock:
        if _ratings is not None and not force:
            return _ratings

    # 释放锁后在线程中加载，避免阻塞事件循环
    await asyncio.to_thread(_load_ratings_sync, force)

    with _lock:
        return _ratings


async def get_rating(imdb_id):
    """获取 IMDb 评分 (rating, votes)，无数据返回 (None, 0)。"""
    ratings = await load_ratings()
    return ratings.get(imdb_id, (None, 0))


async def preload_ratings():
    """启动时预加载 IMDb 评分数据，避免首次请求时阻塞。"""
    logger.info("Preloading IMDb ratings at startup...")
    try:
        await load_ratings()
    except Exception:
        logger.exception("Failed to preload IMDb ratings, will retry on first request")


def clear_ratings():
    """清除内存中的 IMDb 评分数据，释放内存。"""
    global _ratings
    with _lock:
        if _ratings is not None:
            _ratings = None
            logger.info("IMDb ratings cache cleared from memory")
            import gc
            gc.collect()

