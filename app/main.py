import asyncio
from contextlib import asynccontextmanager, suppress
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
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

# GZip 压缩：作用于 API JSON 响应（图片已是压缩格式不重复压缩）
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(router)


# 静态文件：带版本号(?v=) → immutable 一年缓存；其余 → no-cache
# 自定义路由而非 mount，确保 GZip 中间件与缓存头都生效
_STATIC_ALLOWED = {".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"}
_STATIC_MIME = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


@app.get("/static/{file_path:path}")
async def serve_static(file_path: str, request: Request):
    # 防路径穿越：解析后必须在 STATIC_DIR 内
    full = (STATIC_DIR / file_path).resolve()
    try:
        full.relative_to(STATIC_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=404)
    if not full.is_file() or full.suffix.lower() not in _STATIC_ALLOWED:
        raise HTTPException(status_code=404)
    has_version = "v=" in request.url.query
    cache_control = "public, max-age=31536000, immutable" if has_version else "no-cache"
    mime = _STATIC_MIME.get(full.suffix.lower(), "application/octet-stream")
    data = await asyncio.to_thread(full.read_bytes)
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": cache_control, "ETag": f'W/"{full.stat().st_size:x}-{int(full.stat().st_mtime):x}"'},
    )


@app.get("/")
async def root():
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-cache"},
    )
