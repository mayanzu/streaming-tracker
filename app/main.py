from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import logging

from app.api import router
from app.config import STATIC_DIR
from app.database import init_db
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    init_db()
    app.state.scheduler = start_scheduler()
    yield
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
