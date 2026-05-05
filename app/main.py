from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import logging

from app.api import router
from app.scheduler import scheduler, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    init_db()
    scheduler.start()
    yield
    logger.info("Shutting down...")
    scheduler.shutdown()


app = FastAPI(title="Streaming Tracker", description="海外流媒体新片追踪", lifespan=lifespan)

app.include_router(router)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")
