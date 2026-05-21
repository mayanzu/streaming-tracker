from datetime import datetime, timezone

from fastapi import APIRouter, Query, HTTPException, Request, Response, BackgroundTasks

from app.database import (
    check_database,
    get_providers,
    get_stats,
    get_title_detail,
    get_titles,
)
from app.config import MAIN_FILTER_PROVIDERS, TMDB_API_KEY
from app.scheduler import get_scheduler_status
from app.sync import get_sync_state, sync_new_titles

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, response: Response):
    issues = []
    stats = None
    latest_sync = None
    last_update = None
    try:
        scheduler = await get_scheduler_status(getattr(request.app.state, "scheduler", None))
    except Exception:
        scheduler = {"sync": {}}

    if check_database():
        try:
            stats = get_stats()
            latest_sync = stats.get("latest_sync")
            last_update = stats.get("last_update")
        except Exception:
            stats = None
            issues.append("database_unavailable")
    else:
        issues.append("database_unavailable")

    if not TMDB_API_KEY:
        issues.append("missing_tmdb_api_key")
    if scheduler.get("sync", {}).get("running"):
        issues.append("sync_running")
    if latest_sync and latest_sync.get("status") == "failed":
        issues.append("last_sync_failed")
    sync_running = scheduler.get("sync", {}).get("running")
    if stats and stats["total"] == 0 and not sync_running:
        issues.append("empty_database")

    if last_update:
        try:
            last_update_dt = datetime.fromisoformat(last_update)
            if last_update_dt.tzinfo is None:
                last_update_dt = last_update_dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - last_update_dt).days
            if age_days > 7:
                issues.append("stale_data")
        except ValueError:
            issues.append("invalid_last_update")

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
        "latest_sync": latest_sync,
    }


@router.get("/api/titles")
async def list_titles(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    provider: str = Query(None),
    sort_by: str = Query("rating", pattern="^(added_date|rating|release_date)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    title_type: str = Query(None, alias="type", pattern="^(movie|tv)?$"),
    search: str = Query(None),
    year: str = Query(None),
    min_rating: float = Query(None, ge=0, le=10),
):
    return get_titles(
        page=page, limit=limit, provider=provider,
        sort_by=sort_by, order=order, title_type=title_type,
        search=search, year=year, min_rating=min_rating,
    )


@router.get("/api/titles/{title_id}")
async def get_title(title_id: int):
    title = get_title_detail(title_id)
    if not title:
        raise HTTPException(status_code=404, detail="作品未找到")

    # GET 请求不应包含写操作；imdb_id 由同步流程预填充
    # 若缺失，可在下次同步时自动补全
    return title


@router.get("/api/providers")
async def list_providers():
    return {
        "providers": get_providers(),
        "available": list(MAIN_FILTER_PROVIDERS),
        "total": get_stats()["total"],
    }


@router.get("/api/stats")
async def stats():
    return get_stats()


@router.get("/api/sync/status")
async def sync_status(request: Request):
    return await get_scheduler_status(getattr(request.app.state, "scheduler", None))


@router.post("/api/sync")
async def trigger_sync(background_tasks: BackgroundTasks):
    state = await get_sync_state()
    if state.get("running"):
        raise HTTPException(status_code=400, detail="同步任务已经在运行中")

    background_tasks.add_task(
        sync_new_titles,
        reason="manual",
    )
    return {"status": "started"}
