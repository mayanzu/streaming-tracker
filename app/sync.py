"""Shared TMDB synchronization workflow."""

import asyncio
import logging
from datetime import datetime, timezone

from app.config import (
    SYNC_BOOTSTRAP_ON_EMPTY,
    SYNC_BOOTSTRAP_DAYS_BACK,
    SYNC_BOOTSTRAP_MAX_PAGES,
    SYNC_DAYS_BACK,
    SYNC_MAX_PAGES,
    SYNC_WINDOW_DAYS,
    TMDB_API_KEY,
    PROVIDERS,
)
from app.database import (
    count_titles,
    count_untrusted_titles,
    create_sync_run,
    finish_sync_run,
    get_latest_sync_run,
    init_db,
    insert_title,
    mark_sync_run_abandoned,
    purge_all_titles,
    purge_untrusted_titles,
    record_sync_error,
    update_sync_run_progress,
)
from app.fetcher import empty_fetch_stats, fetch_provider_titles, merge_fetch_stats

logger = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()
_sync_state = {
    "running": False,
    "current_reason": None,
    "last_started_at": None,
    "last_finished_at": None,
    "last_result": None,
    "current_run_id": None,
}
BOOTSTRAP_REASONS = {
    "empty_database_bootstrap",
    "untrusted_rating_rebuild",
    "incomplete_bootstrap_rebuild",
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_sync_state():
    return dict(_sync_state)


def _is_incomplete_bootstrap(sync_run):
    return bool(
        sync_run
        and sync_run.get("status") == "running"
        and sync_run.get("reason") in BOOTSTRAP_REASONS
    )


def _result_from_stats(reason, processed, skipped, fetch_stats, extra=None):
    result = {
        "processed": processed,
        "skipped": skipped,
        "reason": reason,
        "discovered": fetch_stats.get("discovered", 0),
        "qualified": fetch_stats.get("qualified", 0),
        "no_rating": fetch_stats.get("no_rating", 0),
        "low_rating": fetch_stats.get("low_rating", 0),
    }
    if extra:
        result.update(extra)
    return result


async def sync_new_titles(
    days_back=SYNC_DAYS_BACK,
    max_pages=SYNC_MAX_PAGES,
    window_days=SYNC_WINDOW_DAYS,
    reason="scheduled",
):
    """Fetch qualified TMDB titles and upsert them into the local database."""
    if _sync_lock.locked():
        logger.info("TMDB sync is already running; skipping overlapping run")
        return {"processed": 0, "skipped": 0, "reason": "already_running"}

    async with _sync_lock:
        init_db()
        sync_run_id = create_sync_run(reason, days_back, max_pages, window_days)
        _sync_state.update(
            {
                "running": True,
                "current_run_id": sync_run_id,
                "current_reason": reason,
                "last_started_at": _now_iso(),
                "last_finished_at": None,
                "last_result": None,
            }
        )
        try:
            if not TMDB_API_KEY:
                logger.warning("TMDB_API_KEY is not configured; skipping sync")
                result = {
                    "processed": 0,
                    "skipped": 0,
                    "reason": "missing_tmdb_api_key",
                    "error": "TMDB_API_KEY is not configured",
                }
                finish_sync_run(sync_run_id, "failed", result)
                return result

            logger.info(
                "Starting TMDB sync: reason=%s days_back=%s max_pages=%s window_days=%s",
                reason,
                days_back,
                max_pages,
                window_days,
            )
            processed = skipped = 0
            fetch_stats = empty_fetch_stats()

            def report_progress(progress):
                current = progress.get("provider")
                progress_stats = dict(fetch_stats)
                partial_stats = progress.get("stats") or {}
                for key in ("discovered", "qualified", "no_rating", "low_rating"):
                    progress_stats[key] += partial_stats.get(key, 0)
                db_progress = dict(progress)
                db_progress["stats"] = progress_stats
                db_progress["processed"] = processed
                db_progress["skipped"] = skipped
                update_sync_run_progress(sync_run_id, db_progress)
                _sync_state["last_result"] = {
                    "processed": processed,
                    "skipped": skipped,
                    "reason": reason,
                    "phase": progress.get("phase"),
                    "current_provider": current,
                    "current_provider_index": progress.get("provider_index", 0),
                    "provider_total": progress.get("provider_total", 0),
                    "current_provider_discovered": progress.get("provider_discovered", 0),
                    "current_provider_qualified": progress.get("provider_qualified", 0),
                    "discovered": progress_stats.get("discovered", 0),
                    "qualified": progress_stats.get("qualified", 0),
                    "no_rating": progress_stats.get("no_rating", 0),
                    "low_rating": progress_stats.get("low_rating", 0),
                }

            provider_total = len(PROVIDERS)
            for provider_index, provider_name in enumerate(PROVIDERS, start=1):
                provider_result = await fetch_provider_titles(
                    provider_name,
                    days_back=days_back,
                    max_pages=max_pages,
                    window_days=window_days,
                    provider_index=provider_index,
                    provider_total=provider_total,
                    progress_callback=report_progress,
                )
                merge_fetch_stats(fetch_stats, provider_result["stats"])

                for title in provider_result["titles"]:
                    try:
                        insert_title(title)
                        processed += 1
                    except Exception:
                        skipped += 1
                        logger.exception("Failed to upsert title %s", title.get("title", "?"))
                        record_sync_error(
                            sync_run_id,
                            title.get("title", "?"),
                            "failed_to_upsert",
                        )

                result = _result_from_stats(
                    reason,
                    processed,
                    skipped,
                    fetch_stats,
                    {
                        "phase": "persisted",
                        "current_provider": provider_name,
                        "current_provider_index": provider_index,
                        "provider_total": provider_total,
                    },
                )
                _sync_state["last_result"] = result
                update_sync_run_progress(
                    sync_run_id,
                    {
                        "phase": "persisted",
                        "provider": provider_name,
                        "provider_index": provider_index,
                        "provider_total": provider_total,
                        "processed": processed,
                        "skipped": skipped,
                        "stats": fetch_stats,
                    },
                )
                logger.info(
                    "TMDB provider persisted: provider=%s processed=%s discovered=%s",
                    provider_name,
                    processed,
                    result["discovered"],
                )

            for error in fetch_stats.get("errors", []):
                record_sync_error(sync_run_id, "fetch", error)

            result = _result_from_stats(reason, processed, skipped, fetch_stats)
            status = "partial" if fetch_stats.get("errors") or skipped else "success"
            finish_sync_run(sync_run_id, status, result)
            logger.info(
                "TMDB sync finished: processed=%s skipped=%s discovered=%s",
                processed,
                skipped,
                result["discovered"],
            )
            return result
        except Exception as exc:
            logger.exception("TMDB sync failed")
            result = {
                "processed": 0,
                "skipped": 0,
                "reason": "sync_failed",
                "error": str(exc),
            }
            finish_sync_run(sync_run_id, "failed", result)
            return result
        finally:
            _sync_state.update(
                {
                    "running": False,
                    "current_run_id": None,
                    "current_reason": None,
                    "last_finished_at": _now_iso(),
                    "last_result": locals().get("result"),
                }
            )


async def sync_if_empty():
    init_db()
    latest_sync = get_latest_sync_run()

    if _is_incomplete_bootstrap(latest_sync):
        removed = purge_all_titles()
        mark_sync_run_abandoned(
            latest_sync.get("id"),
            "Discarded incomplete bootstrap catalog before retry",
        )
        logger.warning(
            "Discarded %s titles from incomplete bootstrap sync id=%s",
            removed,
            latest_sync.get("id"),
        )
        if not SYNC_BOOTSTRAP_ON_EMPTY:
            logger.info("Startup bootstrap sync is disabled after incomplete cleanup")
            return {
                "processed": 0,
                "skipped": removed,
                "reason": "incomplete_bootstrap_removed_bootstrap_disabled",
            }
        return await sync_new_titles(
            days_back=SYNC_BOOTSTRAP_DAYS_BACK,
            max_pages=SYNC_BOOTSTRAP_MAX_PAGES,
            window_days=SYNC_WINDOW_DAYS,
            reason="incomplete_bootstrap_rebuild",
        )

    trusted_total = count_titles()
    untrusted_total = count_untrusted_titles()
    if untrusted_total:
        removed = purge_untrusted_titles()
        logger.warning(
            "Removed %s titles without trusted IMDb ratings before bootstrap check",
            removed,
        )
        if not SYNC_BOOTSTRAP_ON_EMPTY:
            logger.info("Startup bootstrap sync is disabled after untrusted title cleanup")
            return {
                "processed": 0,
                "skipped": removed,
                "reason": "untrusted_removed_bootstrap_disabled",
            }
        logger.info(
            "Rebuilding catalog from strict IMDb ratings after removing untrusted rows"
        )
        return await sync_new_titles(
            days_back=SYNC_BOOTSTRAP_DAYS_BACK,
            max_pages=SYNC_BOOTSTRAP_MAX_PAGES,
            window_days=SYNC_WINDOW_DAYS,
            reason="untrusted_rating_rebuild",
        )

    if not SYNC_BOOTSTRAP_ON_EMPTY:
        logger.info("Startup bootstrap sync is disabled")
        return {"processed": 0, "skipped": 0, "reason": "bootstrap_disabled"}

    if trusted_total > 0:
        logger.info(
            "Database already has %s trusted IMDb-rated titles; skipping startup bootstrap sync",
            trusted_total,
        )
        return {"processed": 0, "skipped": 0, "reason": "database_not_empty"}

    logger.info(
        "Database has no trusted IMDb-rated titles; starting bootstrap TMDB sync: days_back=%s max_pages=%s",
        SYNC_BOOTSTRAP_DAYS_BACK,
        SYNC_BOOTSTRAP_MAX_PAGES,
    )
    return await sync_new_titles(
        days_back=SYNC_BOOTSTRAP_DAYS_BACK,
        max_pages=SYNC_BOOTSTRAP_MAX_PAGES,
        window_days=SYNC_WINDOW_DAYS,
        reason="empty_database_bootstrap",
    )
