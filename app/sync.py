"""Shared TMDB synchronization workflow."""

import asyncio
import logging
from datetime import datetime, timezone

from app.config import (
    SYNC_BOOTSTRAP_ON_EMPTY,
    SYNC_DAYS_BACK,
    SYNC_MAX_PAGES,
    TMDB_API_KEY,
)
from app.database import count_titles, init_db, insert_title
from app.fetcher import fetch_all_providers

logger = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()
_sync_state = {
    "running": False,
    "current_reason": None,
    "last_started_at": None,
    "last_finished_at": None,
    "last_result": None,
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_sync_state():
    return dict(_sync_state)


async def sync_new_titles(days_back=SYNC_DAYS_BACK, max_pages=SYNC_MAX_PAGES, reason="scheduled"):
    """Fetch qualified TMDB titles and upsert them into the local database."""
    if not TMDB_API_KEY:
        logger.warning("TMDB_API_KEY is not configured; skipping scheduled sync")
        result = {"processed": 0, "skipped": 0, "reason": "missing_tmdb_api_key"}
        _sync_state["last_result"] = result
        return result

    if _sync_lock.locked():
        logger.info("TMDB sync is already running; skipping overlapping run")
        return {"processed": 0, "skipped": 0, "reason": "already_running"}

    async with _sync_lock:
        _sync_state.update(
            {
                "running": True,
                "current_reason": reason,
                "last_started_at": _now_iso(),
                "last_finished_at": None,
                "last_result": None,
            }
        )
        try:
            logger.info(
                "Starting TMDB sync: reason=%s days_back=%s max_pages=%s",
                reason,
                days_back,
                max_pages,
            )
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

            result = {"processed": processed, "skipped": skipped, "reason": reason}
            logger.info("TMDB sync finished: processed=%s skipped=%s", processed, skipped)
            return result
        except Exception as exc:
            logger.exception("TMDB sync failed")
            result = {
                "processed": 0,
                "skipped": 0,
                "reason": "sync_failed",
                "error": str(exc),
            }
            return result
        finally:
            _sync_state.update(
                {
                    "running": False,
                    "current_reason": None,
                    "last_finished_at": _now_iso(),
                    "last_result": locals().get("result"),
                }
            )


async def sync_if_empty():
    if not SYNC_BOOTSTRAP_ON_EMPTY:
        logger.info("Startup bootstrap sync is disabled")
        return {"processed": 0, "skipped": 0, "reason": "bootstrap_disabled"}

    init_db()
    total = count_titles()
    if total > 0:
        logger.info("Database already has %s titles; skipping startup bootstrap sync", total)
        return {"processed": 0, "skipped": 0, "reason": "database_not_empty"}

    logger.info("Database is empty; starting bootstrap TMDB sync")
    return await sync_new_titles(reason="empty_database_bootstrap")
