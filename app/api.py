import asyncio
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from app.database import (
    check_database,
    get_providers,
    get_stats,
    get_title_detail,
    get_titles,
    update_title_status,
)
from app.config import MAIN_FILTER_PROVIDERS, SYNC_ENABLED, TMDB_API_KEY
from app.scheduler import get_scheduler_status
from app.sync import get_sync_state, sync_new_titles

router = APIRouter()


class WatchStatusUpdate(BaseModel):
    watch_status: Literal["", "watchlist", "watching", "watched"]


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, response: Response):
    issues = []
    stats = None
    latest_sync = None
    last_update = None
    last_synced_at = None
    try:
        scheduler = await get_scheduler_status(getattr(request.app.state, "scheduler", None))
    except Exception:
        scheduler = {"sync": {}}

    db_ok = await asyncio.to_thread(check_database)
    if db_ok:
        try:
            stats = await asyncio.to_thread(get_stats)
            latest_sync = stats.get("latest_sync")
            last_update = stats.get("last_update")
            last_synced_at = stats.get("last_synced_at")
        except Exception:
            stats = None
            issues.append("database_unavailable")
    else:
        issues.append("database_unavailable")

    if SYNC_ENABLED and not TMDB_API_KEY:
        issues.append("missing_tmdb_api_key")
    if scheduler.get("sync", {}).get("running"):
        issues.append("sync_running")
    if latest_sync and latest_sync.get("status") == "failed":
        issues.append("last_sync_failed")
    sync_running = scheduler.get("sync", {}).get("running")
    if stats and stats["total"] == 0 and not sync_running:
        issues.append("empty_database")

    freshness_timestamp = last_synced_at or last_update
    if freshness_timestamp:
        try:
            last_update_dt = datetime.fromisoformat(freshness_timestamp)
            if last_update_dt.tzinfo is None:
                last_update_dt = last_update_dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - last_update_dt).days
            if age_days > 7:
                issues.append("stale_data")
        except ValueError:
            issues.append("invalid_last_synced_at")

    non_blocking = {"sync_running"}
    if sync_running:
        non_blocking.add("empty_database")
    blocking_issues = [issue for issue in issues if issue not in non_blocking]
    status = "ready" if not blocking_issues else "degraded"
    if status != "ready":
        response.status_code = 503

    return {
        "status": status,
        "issues": issues,
        "total": stats["total"] if stats else 0,
        "last_update": last_update,
        "last_synced_at": last_synced_at,
        "latest_sync": latest_sync,
    }


# 读路径用 def 让 FastAPI 自动包 threadpool，避免同步 SQLite 调用阻塞事件循环；
# 弱 ARM 单 worker 场景下这是最大并发瓶颈。

@router.get("/api/titles")
def list_titles(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    provider: str = Query(None),
    sort_by: str = Query("release_date", pattern="^(added_date|rating|release_date)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    title_type: str = Query(None, alias="type", pattern="^(movie|tv)?$"),
    search: str = Query(None),
    year: str = Query(None),
    min_rating: float = Query(None, ge=0, le=10),
    watch_status: str = Query(None, pattern="^(watchlist|watching|watched)?$"),
):
    return get_titles(
        page=page, limit=limit, provider=provider,
        sort_by=sort_by, order=order, title_type=title_type,
        search=search, year=year, min_rating=min_rating,
        watch_status=watch_status,
    )


@router.get("/api/titles/{title_id}")
def get_title(title_id: int):
    title = get_title_detail(title_id)
    if not title:
        raise HTTPException(status_code=404, detail="作品未找到")
    return title


@router.patch("/api/titles/{title_id}/status")
def set_title_status(title_id: int, payload: WatchStatusUpdate):
    title = update_title_status(title_id, payload.watch_status)
    if not title:
        raise HTTPException(status_code=404, detail="作品未找到")
    return title


@router.get("/api/providers")
def list_providers():
    return {
        "providers": get_providers(),
        "available": list(MAIN_FILTER_PROVIDERS),
        "total": get_stats()["total"],
    }


@router.get("/api/stats")
def stats():
    return get_stats()


@router.get("/api/sync/status")
async def sync_status(request: Request):
    return await get_scheduler_status(getattr(request.app.state, "scheduler", None))


@router.post("/api/sync", status_code=status.HTTP_202_ACCEPTED)
async def trigger_sync(background_tasks: BackgroundTasks):
    if not SYNC_ENABLED:
        raise HTTPException(status_code=403, detail="同步已在部署中禁用（SYNC_ENABLED=false）")
    if not TMDB_API_KEY:
        raise HTTPException(status_code=400, detail="未配置 TMDB_API_KEY，无法触发同步")

    state = await get_sync_state()
    if state.get("running"):
        raise HTTPException(status_code=400, detail="同步任务已经在运行中")

    background_tasks.add_task(
        sync_new_titles,
        reason="manual",
    )
    return {"status": "started"}
