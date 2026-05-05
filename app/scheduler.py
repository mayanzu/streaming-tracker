from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.database import init_db, insert_title
from app.fetcher import fetch_all_providers
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def run_fetch():
    logger.info("Starting scheduled fetch...")
    try:
        titles = await fetch_all_providers()
        for title in titles:
            insert_title(title)
        logger.info(f"Fetched and saved {len(titles)} titles")
    except Exception as e:
        logger.error(f"Error in scheduled fetch: {e}")


scheduler.add_job(
    run_fetch,
    CronTrigger(hour=8, minute=0),
    id='fetch_titles',
    name='Fetch new titles from streaming platforms'
)
