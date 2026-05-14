"""Shared TMDB synchronization workflow."""

import asyncio
import logging

from app.config import SYNC_DAYS_BACK, SYNC_MAX_PAGES, TMDB_API_KEY
from app.database import init_db, insert_title
from app.fetcher import fetch_all_providers

logger = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()


async def sync_new_titles(days_back=SYNC_DAYS_BACK, max_pages=SYNC_MAX_PAGES):
    """Fetch qualified TMDB titles and upsert them into the local database."""
    if not TMDB_API_KEY:
        logger.warning("TMDB_API_KEY is not configured; skipping scheduled sync")
        return {"processed": 0, "skipped": 0, "reason": "missing_tmdb_api_key"}

    if _sync_lock.locked():
        logger.info("TMDB sync is already running; skipping overlapping run")
        return {"processed": 0, "skipped": 0, "reason": "already_running"}

    async with _sync_lock:
        logger.info("Starting TMDB sync: days_back=%s max_pages=%s", days_back, max_pages)
        init_db()
        titles = await fetch_all_providers(days_back=days_back, max_pages=max_pages)

        processed = skipped = 0
        for title in titles:
            try:
                insert_title(title)
                processed += 1
            except Exception:
                skipped += 1
                logger.exception("Failed to upsert title %s", title.get("title", "?"))

        logger.info("TMDB sync finished: processed=%s skipped=%s", processed, skipped)
        return {"processed": processed, "skipped": skipped}
