import asyncio
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import logging

from app.api import router
from app.config import STATIC_DIR
from app.database import init_db
from app.scheduler import start_scheduler, stop_scheduler
from app.sync import sync_if_empty

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    init_db()
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
        headers={
            "Cache-Control": "no-store",
            "Clear-Site-Data": '"cache"',
        },
    )
