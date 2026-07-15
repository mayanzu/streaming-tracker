"""Reliable, idempotent TMDB synchronization workflow."""

import asyncio
import logging
from datetime import datetime, timezone

from app.config import (
    PROVIDERS,
    SYNC_BOOTSTRAP_DAYS_BACK,
    SYNC_BOOTSTRAP_MAX_PAGES,
    SYNC_BOOTSTRAP_ON_EMPTY,
    SYNC_CATALOG_SCAN_DAYS_BACK,
    SYNC_CATALOG_SCAN_ENABLED,
    SYNC_CATALOG_WINDOW_DAYS,
    SYNC_DAYS_BACK,
    SYNC_INCREMENTAL_OVERLAP_DAYS,
    SYNC_MAX_PAGES,
    SYNC_WINDOW_DAYS,
    TMDB_API_KEY,
)
from app.database import (
    claim_catalog_window,
    count_titles,
    count_untrusted_titles,
    create_sync_run,
    finish_sync_run,
    get_due_pending_titles,
    get_latest_finished_sync_run,
    get_latest_sync_run,
    get_title_cache,
    init_db,
    mark_sync_run_abandoned,
    persist_sync_batch,
    purge_all_titles,
    record_sync_error,
    update_sync_run_progress,
)
from app.fetcher import (
    discover_all_providers,
    empty_fetch_stats,
    enrich_titles,
    merge_fetch_stats,
)

logger = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()
_state_lock = asyncio.Lock()
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


async def get_sync_state():
    async with _state_lock:
        return dict(_sync_state)


def _is_incomplete_bootstrap(sync_run):
    return bool(
        sync_run
        and sync_run.get("status") == "running"
        and sync_run.get("reason") in BOOTSTRAP_REASONS
    )


def _merge_candidate(target, incoming):
    target["providers"] = list(dict.fromkeys(
        (target.get("providers") or []) + (incoming.get("providers") or [])
    ))
    regions = target.setdefault("provider_regions", {})
    for provider, values in (incoming.get("provider_regions") or {}).items():
        regions[provider] = list(dict.fromkeys((regions.get(provider) or []) + values))
    target["discovery_channels"] = list(dict.fromkeys(
        (target.get("discovery_channels") or []) + (incoming.get("discovery_channels") or [])
    ))
    target["origin_countries"] = list(dict.fromkeys(
        (target.get("origin_countries") or []) + (incoming.get("origin_countries") or [])
    ))
    for field in ("title", "original_title", "overview", "release_date", "poster_url", "imdb_id"):
        if not target.get(field) and incoming.get(field):
            target[field] = incoming[field]


def _result_from_stats(reason, processed, skipped, stats, extra=None):
    result = {
        "processed": processed,
        "skipped": skipped,
        "reason": reason,
        "discovered": stats.get("discovered", 0),
        "unique_discovered": stats.get("unique_discovered", 0),
        "qualified": stats.get("qualified", 0),
        "cached": stats.get("cached", 0),
        "pending": stats.get("pending", 0),
        "no_rating": stats.get("no_rating", 0),
        "low_rating": stats.get("low_rating", 0),
        "request_failed": stats.get("request_failed", 0),
        "inserted": stats.get("inserted", 0),
        "updated": stats.get("updated", 0),
        "unchanged": stats.get("unchanged", 0),
        "provider_expired": stats.get("provider_expired", 0),
    }
    if extra:
        result.update(extra)
    return result


def _incremental_days_back(reason, requested_days):
    if reason != "scheduled":
        return requested_days
    latest = get_latest_finished_sync_run()
    if not latest or latest.get("status") not in {"success", "partial"}:
        return requested_days
    try:
        finished = datetime.fromisoformat(latest["finished_at"])
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        elapsed_days = max(1, (datetime.now(timezone.utc) - finished).days + 1)
        return min(requested_days, elapsed_days + SYNC_INCREMENTAL_OVERLAP_DAYS)
    except (TypeError, ValueError):
        return requested_days


async def sync_new_titles(
    days_back=SYNC_DAYS_BACK,
    max_pages=SYNC_MAX_PAGES,
    window_days=SYNC_WINDOW_DAYS,
    reason="scheduled",
):
    if _sync_lock.locked():
        logger.info("TMDB sync is already running; skipping overlapping run")
        return {"processed": 0, "skipped": 0, "reason": "already_running"}

    async with _sync_lock:
        await asyncio.to_thread(init_db)
        effective_days_back = await asyncio.to_thread(_incremental_days_back, reason, days_back)
        sync_run_id = await asyncio.to_thread(
            create_sync_run, reason, effective_days_back, max_pages, window_days,
        )
        async with _state_lock:
            _sync_state.update({
                "running": True,
                "current_run_id": sync_run_id,
                "current_reason": reason,
                "last_started_at": _now_iso(),
                "last_finished_at": None,
                "last_result": None,
            })

        result = None
        try:
            if not TMDB_API_KEY:
                result = {
                    "processed": 0, "skipped": 0, "reason": "missing_tmdb_api_key",
                    "error": "TMDB_API_KEY is not configured",
                }
                await asyncio.to_thread(finish_sync_run, sync_run_id, "failed", result)
                return result

            catalog_range = None
            if SYNC_CATALOG_SCAN_ENABLED:
                catalog_range = await asyncio.to_thread(
                    claim_catalog_window,
                    SYNC_CATALOG_SCAN_DAYS_BACK,
                    SYNC_CATALOG_WINDOW_DAYS,
                    effective_days_back,
                )
            logger.info(
                "Starting sync reason=%s recent_days=%s catalog_range=%s max_pages=%s",
                reason, effective_days_back, catalog_range, max_pages,
            )

            fetch_stats = empty_fetch_stats()
            processed = skipped = 0

            async def report_progress(progress):
                stats = progress.get("stats") or fetch_stats
                db_progress = dict(progress)
                db_progress.update({"stats": stats, "processed": processed, "skipped": skipped})
                await asyncio.to_thread(update_sync_run_progress, sync_run_id, db_progress)
                snapshot = _result_from_stats(
                    reason, processed, skipped, stats,
                    {
                        "phase": progress.get("phase"),
                        "current_provider": progress.get("provider"),
                        "current_provider_index": progress.get("provider_index", 0),
                        "provider_total": progress.get("provider_total", len(PROVIDERS)),
                    },
                )
                async with _state_lock:
                    _sync_state["last_result"] = snapshot

            discovered = await discover_all_providers(
                days_back=effective_days_back,
                max_pages=max_pages,
                window_days=window_days,
                catalog_range=catalog_range,
                progress_callback=report_progress,
            )
            fetch_stats = discovered["stats"]

            candidates_by_key = {
                (item["type"], item["tmdb_id"]): item for item in discovered["titles"]
            }
            due_pending = await asyncio.to_thread(get_due_pending_titles)
            for pending_item in due_pending:
                key = (pending_item["type"], pending_item["tmdb_id"])
                if key in candidates_by_key:
                    _merge_candidate(candidates_by_key[key], pending_item)
                else:
                    candidates_by_key[key] = pending_item
            fetch_stats["unique_discovered"] = len(candidates_by_key)

            identities = list(candidates_by_key)
            cached_titles = await asyncio.to_thread(get_title_cache, identities)
            enriched = await enrich_titles(
                list(candidates_by_key.values()),
                cached_titles=cached_titles,
                progress_callback=report_progress,
            )
            merge_fetch_stats(fetch_stats, enriched["stats"])
            fetch_stats["qualified"] = len(enriched["titles"])
            fetch_stats["pending"] = len(enriched["pending"])

            persistence = await asyncio.to_thread(
                persist_sync_batch, enriched["titles"], enriched["pending"],
            )
            processed = persistence["processed"]
            skipped = persistence["skipped"]
            for key in ("inserted", "updated", "unchanged", "provider_expired"):
                fetch_stats[key] = persistence[key]
            fetch_stats["errors"].extend(persistence["errors"])

            await asyncio.to_thread(
                update_sync_run_progress,
                sync_run_id,
                {
                    "phase": "persisted",
                    "provider": None,
                    "provider_index": len(PROVIDERS),
                    "provider_total": len(PROVIDERS),
                    "processed": processed,
                    "skipped": skipped,
                    "inserted": fetch_stats["inserted"],
                    "updated": fetch_stats["updated"],
                    "unchanged": fetch_stats["unchanged"],
                    "provider_expired": fetch_stats["provider_expired"],
                    "stats": fetch_stats,
                },
            )

            for error in fetch_stats.get("errors", []):
                await asyncio.to_thread(record_sync_error, sync_run_id, "sync", error)

            result = _result_from_stats(
                reason, processed, skipped, fetch_stats,
                {
                    "catalog_range": [value.isoformat() for value in catalog_range]
                    if catalog_range else None,
                },
            )
            status = "partial" if fetch_stats.get("errors") or skipped else "success"
            await asyncio.to_thread(finish_sync_run, sync_run_id, status, result)
            logger.info(
                "Sync finished status=%s unique=%s inserted=%s updated=%s unchanged=%s pending=%s",
                status, result["unique_discovered"], result["inserted"], result["updated"],
                result["unchanged"], result["pending"],
            )
            return result
        except Exception as exc:
            logger.exception("TMDB sync failed")
            result = {
                "processed": 0,
                "skipped": 0,
                "reason": "sync_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            await asyncio.to_thread(finish_sync_run, sync_run_id, "failed", result)
            return result
        finally:
            async with _state_lock:
                _sync_state.update({
                    "running": False,
                    "current_run_id": None,
                    "current_reason": None,
                    "last_finished_at": _now_iso(),
                    "last_result": result,
                })


async def sync_if_empty():
    await asyncio.to_thread(init_db)
    latest_sync = await asyncio.to_thread(get_latest_sync_run)

    if _is_incomplete_bootstrap(latest_sync):
        removed = await asyncio.to_thread(purge_all_titles)
        await asyncio.to_thread(
            mark_sync_run_abandoned,
            latest_sync.get("id"),
            "Discarded incomplete bootstrap catalog before retry",
        )
        if not SYNC_BOOTSTRAP_ON_EMPTY:
            return {
                "processed": 0, "skipped": removed,
                "reason": "incomplete_bootstrap_removed_bootstrap_disabled",
            }
        return await sync_new_titles(
            days_back=SYNC_BOOTSTRAP_DAYS_BACK,
            max_pages=SYNC_BOOTSTRAP_MAX_PAGES,
            window_days=SYNC_WINDOW_DAYS,
            reason="incomplete_bootstrap_rebuild",
        )

    trusted_total = await asyncio.to_thread(count_titles)
    untrusted_total = await asyncio.to_thread(count_untrusted_titles)
    if untrusted_total:
        logger.warning(
            "Found %s legacy untrusted titles; preserving them for in-place refresh",
            untrusted_total,
        )

    if not SYNC_BOOTSTRAP_ON_EMPTY:
        return {"processed": 0, "skipped": 0, "reason": "bootstrap_disabled"}
    if trusted_total > 0:
        return {"processed": 0, "skipped": 0, "reason": "database_not_empty"}

    return await sync_new_titles(
        days_back=SYNC_BOOTSTRAP_DAYS_BACK,
        max_pages=SYNC_BOOTSTRAP_MAX_PAGES,
        window_days=SYNC_WINDOW_DAYS,
        reason="empty_database_bootstrap",
    )
