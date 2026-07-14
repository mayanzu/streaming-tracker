"""Low-memory access to the official IMDb ratings dataset."""

import asyncio
import gzip
import logging
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app.config import DATA_DIR

logger = logging.getLogger(__name__)

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
CACHE_DIR = DATA_DIR
CACHE_FILE = CACHE_DIR / "title.ratings.tsv"
CACHE_MAX_AGE_HOURS = 24
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_DELAY = 5
_lock = threading.RLock()


def _cache_valid():
    if not CACHE_FILE.exists() or CACHE_FILE.stat().st_size == 0:
        return False
    mtime = datetime.fromtimestamp(CACHE_FILE.stat().st_mtime)
    return datetime.now() - mtime < timedelta(hours=CACHE_MAX_AGE_HOURS)


def _download():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    gz_path = Path(f"{CACHE_FILE}.gz")
    tmp_path = Path(f"{CACHE_FILE}.tmp")
    last_error = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            logger.info("Downloading IMDb ratings dataset (attempt %s/%s)", attempt, DOWNLOAD_RETRIES)
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                with client.stream("GET", DATASET_URL) as response:
                    response.raise_for_status()
                    with open(gz_path, "wb") as output:
                        for chunk in response.iter_bytes(chunk_size=64 * 1024):
                            output.write(chunk)
            with gzip.open(gz_path, "rb") as source, open(tmp_path, "wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            tmp_path.replace(CACHE_FILE)
            gz_path.unlink(missing_ok=True)
            logger.info("IMDb ratings dataset is ready")
            return
        except Exception as exc:
            last_error = exc
            gz_path.unlink(missing_ok=True)
            tmp_path.unlink(missing_ok=True)
            if attempt < DOWNLOAD_RETRIES:
                logger.warning("IMDb dataset download failed; retrying in %ss: %s", DOWNLOAD_RETRY_DELAY, exc)
                time.sleep(DOWNLOAD_RETRY_DELAY)

    raise RuntimeError("Failed to download IMDb ratings dataset") from last_error


def _ensure_dataset_sync(force=False):
    with _lock:
        if force or not _cache_valid():
            _download()
    return CACHE_FILE


def _get_ratings_sync(imdb_ids, force=False):
    """Scan once and retain only requested IDs instead of loading millions of rows."""
    wanted = {imdb_id for imdb_id in imdb_ids if imdb_id}
    if not wanted:
        return {}

    path = _ensure_dataset_sync(force=force)
    ratings = {}
    with open(path, "r", encoding="utf-8") as source:
        next(source, None)
        for line in source:
            imdb_id, separator, remainder = line.partition("\t")
            if not separator or imdb_id not in wanted:
                continue
            parts = remainder.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            try:
                ratings[imdb_id] = (float(parts[0]), int(parts[1]))
            except (TypeError, ValueError):
                continue
            if len(ratings) == len(wanted):
                break
    logger.info("Matched %s/%s requested IMDb ratings", len(ratings), len(wanted))
    return ratings


async def get_ratings(imdb_ids, force=False):
    return await asyncio.to_thread(_get_ratings_sync, set(imdb_ids), force)


async def get_rating(imdb_id):
    ratings = await get_ratings({imdb_id})
    return ratings.get(imdb_id, (None, 0))


async def load_ratings(force=False):
    """Backward-compatible helper; it no longer builds a full in-memory dictionary."""
    await asyncio.to_thread(_ensure_dataset_sync, force)
    return {}


async def preload_ratings():
    try:
        await load_ratings()
    except Exception:
        logger.exception("Failed to prepare IMDb ratings dataset")


def clear_ratings():
    """Kept for compatibility; lookups are already bounded-memory."""
    return None
