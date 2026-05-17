"""In-process scheduler for recurring TMDB synchronization."""

import logging

import pytz
from pytz import UnknownTimeZoneError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import (
    SYNC_DAYS_BACK,
    SYNC_ENABLED,
    SYNC_HOUR,
    SYNC_MAX_PAGES,
    SYNC_MINUTE,
    SYNC_TIMEZONE,
    SYNC_WINDOW_DAYS,
    SYNC_BOOTSTRAP_ON_EMPTY,
    SYNC_BOOTSTRAP_DAYS_BACK,
    SYNC_BOOTSTRAP_MAX_PAGES,
)
from app.database import get_latest_finished_sync_run, get_latest_sync_run
from app.sync import get_sync_state, sync_new_titles

logger = logging.getLogger(__name__)
JOB_ID = "daily_tmdb_sync"


def _timezone():
    try:
        return pytz.timezone(SYNC_TIMEZONE)
    except UnknownTimeZoneError:
        logger.warning("Unknown SYNC_TIMEZONE=%s; falling back to UTC", SYNC_TIMEZONE)
        return pytz.utc


def start_scheduler():
    if not SYNC_ENABLED:
        logger.info("Scheduled TMDB sync is disabled")
        return None

    timezone = _timezone()
    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        sync_new_titles,
        trigger=CronTrigger(hour=SYNC_HOUR, minute=SYNC_MINUTE, timezone=timezone),
        kwargs={
            "days_back": SYNC_DAYS_BACK,
            "max_pages": SYNC_MAX_PAGES,
            "window_days": SYNC_WINDOW_DAYS,
            "reason": "scheduled",
        },
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()

    job = scheduler.get_job(JOB_ID)
    logger.info(
        "Scheduled TMDB sync enabled: %02d:%02d %s; next run: %s",
        SYNC_HOUR,
        SYNC_MINUTE,
        SYNC_TIMEZONE,
        job.next_run_time if job else "unknown",
    )
    return scheduler


def stop_scheduler(scheduler):
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduled TMDB sync stopped")


async def get_scheduler_status(scheduler):
    job = None
    if scheduler and scheduler.running:
        job = scheduler.get_job(JOB_ID)
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    latest_finished_sync = get_latest_finished_sync_run()
    latest_sync = get_latest_sync_run()

    return {
        "enabled": SYNC_ENABLED,
        "running": bool(scheduler and scheduler.running),
        "job_id": JOB_ID if job else None,
        "next_run_time": next_run,
        "hour": SYNC_HOUR,
        "minute": SYNC_MINUTE,
        "timezone": SYNC_TIMEZONE,
        "days_back": SYNC_DAYS_BACK,
        "max_pages": SYNC_MAX_PAGES,
        "window_days": SYNC_WINDOW_DAYS,
        "bootstrap_on_empty": SYNC_BOOTSTRAP_ON_EMPTY,
        "bootstrap_days_back": SYNC_BOOTSTRAP_DAYS_BACK,
        "bootstrap_max_pages": SYNC_BOOTSTRAP_MAX_PAGES,
        "sync": await get_sync_state(),
        "latest_run": latest_sync,
        "latest_finished_sync": latest_finished_sync,
    }
