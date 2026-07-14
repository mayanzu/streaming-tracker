import asyncio
from contextlib import asynccontextmanager, suppress
import re

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import logging

from app.api import router
from app.config import STATIC_DIR, SYNC_ENABLED, TMDB_API_KEY, TMDB_BASE_URL
from app.database import init_db

from app.scheduler import start_scheduler, stop_scheduler
from app.sync import sync_if_empty

class SecretRedactionFilter(logging.Filter):
    """Prevent credentials in URLs/headers from reaching any configured log sink."""

    _patterns = (
        re.compile(r"(?i)([?&](?:api_?key|apikey)=)[^&\s]+"),
        re.compile(r"(?i)(authorization[=:]\s*bearer\s+)[^,;\s]+"),
        re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+"),
    )

    def filter(self, record):
        message = record.getMessage()
        for pattern in self._patterns:
            message = pattern.sub(r"\1[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


logging.basicConfig(level=logging.INFO)
for handler in logging.getLogger().handlers:
    handler.addFilter(SecretRedactionFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def _validate_api_key():
    if not TMDB_API_KEY:
        logger.warning("TMDB_API_KEY is not configured")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if len(TMDB_API_KEY) == 32:
                response = await client.get(
                    f"{TMDB_BASE_URL}/configuration",
                    params={"api_key": TMDB_API_KEY},
                )
            else:
                response = await client.get(
                    f"{TMDB_BASE_URL}/authentication",
                    headers={"Authorization": f"Bearer {TMDB_API_KEY}"},
                )
            if response.status_code == 200:
                logger.info("TMDB API key validated successfully")
                return True
            logger.warning(
                "TMDB API key validation failed: HTTP %s %s",
                response.status_code,
                response.text[:200],
            )
            return False
    except Exception as exc:
        logger.warning("TMDB API key validation failed: %s", exc)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    init_db()
    # 路由器场景 SYNC_ENABLED=false 时跳过对 api.themoviedb.org 的启动校验，
    # 避免无外网 / 无 key 时浪费一次 10s HTTPS 超时
    if SYNC_ENABLED:
        asyncio.create_task(_validate_api_key())
    app.state.scheduler = start_scheduler()
    app.state.initial_sync_task = asyncio.create_task(sync_if_empty())
    yield
    initial_sync_task = getattr(app.state, "initial_sync_task", None)
    if initial_sync_task and not initial_sync_task.done():
        initial_sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await initial_sync_task
    stop_scheduler(getattr(app.state, "scheduler", None))
    logger.info("Shutting down...")


app = FastAPI(title="Streaming Tracker", description="海外流媒体新片追踪", lifespan=lifespan)

app.include_router(router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-cache"},
    )
