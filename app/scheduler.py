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
)
from app.sync import sync_new_titles

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
        kwargs={"days_back": SYNC_DAYS_BACK, "max_pages": SYNC_MAX_PAGES},
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


def get_scheduler_status(scheduler):
    job = scheduler.get_job(JOB_ID) if scheduler else None
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None

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
    }
