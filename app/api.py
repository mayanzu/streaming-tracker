import asyncio
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.config import APP_VERSION, BUILD_ID, SCHEMA_VERSION, SYNC_ENABLED, TMDB_API_KEY
from app.db import (
    check_database,
    export_watchlist,
    get_preference_detail,
    get_recent_releases,
    get_stats,
    get_title_detail,
    get_titles,
    import_watchlist,
    persist_sync_batch,
    update_preference_by_identity,
    update_status_by_identity,
    update_title_preference,
    update_title_status,
    update_titles_batch,
)
from app.related import get_related_titles_async
from app.scheduler import get_scheduler_status
from app.importer import TitleImportError, import_title_by_imdb
from app.sync import get_sync_state, sync_new_titles

router = APIRouter()


class WatchStatusUpdate(BaseModel):
    watch_status: Literal["", "watchlist", "watching", "watched"]


class PreferenceUpdate(BaseModel):
    """6.7 我的记录：优先级 / 备注 / 个人评分（均可选，只更新传入字段）。"""
    priority: int | None = Field(None, ge=0, le=2)
    note: str | None = Field(None, max_length=200)
    personal_rating: float | None = Field(None, ge=0, le=10)


class BatchUpdate(BaseModel):
    """6.7 批量操作：同一状态/优先级应用到多部作品。"""
    ids: list[int] = Field(..., min_length=1, max_length=200)
    watch_status: Literal["", "watchlist", "watching", "watched"] | None = None
    priority: int | None = Field(None, ge=0, le=2)


class TitleImportRequest(BaseModel):
    imdb: str = Field(min_length=1, max_length=200)


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
    try:
        from app.imdb_data import dataset_status
        if dataset_status().get("stale"):
            issues.append("ratings_dataset_stale")
    except Exception:
        issues.append("ratings_dataset_stale")
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
        "app_version": APP_VERSION,
        "build_id": BUILD_ID,
        "schema_version": SCHEMA_VERSION,
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
    sort_by: str = Query("release_date", pattern="^(rating|release_date|updated_at|priority)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    title_type: str = Query(None, alias="type", pattern="^(movie|tv)?$"),
    search: str = Query(None, max_length=100),
    region: str = Query(None, pattern="^[A-Za-z]{2}$"),
    min_rating: float = Query(None, ge=0, le=10),
    watch_status: str = Query(None, pattern="^(watchlist|watching|watched)?$"),
    genre: list[str] | None = Query(None, max_length=20),
    max_runtime: int = Query(None, ge=30, le=500),
    year_from: int | None = Query(None, ge=1900, le=2100),
    year_to: int | None = Query(None, ge=1900, le=2100),
    exclude_watched: bool = Query(False),
    released_after: str = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    return get_titles(
        page=page, limit=limit,
        sort_by=sort_by, order=order, title_type=title_type,
        search=search, region=region, min_rating=min_rating,
        watch_status=watch_status, genre=genre, max_runtime=max_runtime,
        year_from=year_from, year_to=year_to,
        exclude_watched=exclude_watched, released_after=released_after,
    )


@router.post("/api/titles/import")
async def import_title(payload: TitleImportRequest):
    if not TMDB_API_KEY:
        raise HTTPException(status_code=400, detail={
            "code": "missing_tmdb_api_key",
            "message": "未配置 TMDB_API_KEY，无法导入作品",
        })
    try:
        return await import_title_by_imdb(payload.imdb)
    except TitleImportError as exc:
        status_by_code = {
            "invalid_imdb_id": 422,
            "tmdb_not_found": 404,
            "missing_tmdb_api_key": 400,
            "external_request_failed": 502,
            "persistence_failed": 500,
        }
        raise HTTPException(
            status_code=status_by_code.get(exc.code, 500),
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.get("/api/titles/{title_id}")
def get_title(title_id: int):
    title = get_title_detail(title_id)
    if not title:
        raise HTTPException(status_code=404, detail="作品未找到")
    return title


@router.get("/api/titles/{title_id}/related")
async def get_related(title_id: int, limit: int = Query(12, ge=1, le=24)):
    return {"titles": await get_related_titles_async(title_id, limit=limit)}


@router.get("/api/watchlist/export")
def export_list():
    items = export_watchlist()
    return {"schema_version": 2, "exported_at": datetime.now(timezone.utc).isoformat(),
            "count": len(items), "items": items}


class WatchlistImportRequest(BaseModel):
    items: list = Field(default_factory=list, max_length=5000)
    schema_version: int = Field(1, ge=1, le=99)
    dry_run: bool = False
    force: bool = False


@router.post("/api/watchlist/import")
def import_list(payload: WatchlistImportRequest):
    try:
        result = import_watchlist(
            payload.items,
            schema_version=payload.schema_version,
            dry_run=payload.dry_run,
            force=payload.force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@router.get("/api/watchlist/{media_type}/{tmdb_id}")
def watchlist_entry(media_type: str, tmdb_id: int):
    """按身份读取个人条目：目录缺失时也能查看保存记录。"""
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=422, detail="type 必须是 movie 或 tv")
    entry = get_preference_detail(media_type, tmdb_id)
    if not entry:
        raise HTTPException(status_code=404, detail="条目不在片单中")
    return entry


@router.patch("/api/watchlist/{media_type}/{tmdb_id}/status")
def set_watchlist_entry_status(media_type: str, tmdb_id: int, payload: WatchStatusUpdate):
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=422, detail="type 必须是 movie 或 tv")
    entry = update_status_by_identity(media_type, tmdb_id, payload.watch_status)
    if entry is None and payload.watch_status:
        raise HTTPException(status_code=404, detail="条目不在片单中")
    # 移除成功时条目已不存在，返回明确的成功响应而不是 404
    return entry if entry is not None else {"removed": True, "tmdb_id": tmdb_id, "type": media_type}


@router.patch("/api/watchlist/{media_type}/{tmdb_id}/preference")
def set_watchlist_entry_preference(media_type: str, tmdb_id: int, payload: PreferenceUpdate):
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=422, detail="type 必须是 movie 或 tv")
    entry = update_preference_by_identity(
        media_type, tmdb_id,
        priority=payload.priority,
        note=payload.note,
        personal_rating=payload.personal_rating,
    )
    if not entry:
        raise HTTPException(status_code=404, detail="条目不在片单中")
    return entry


@router.post("/api/watchlist/{media_type}/{tmdb_id}/refresh")
async def refresh_watchlist_entry(media_type: str, tmdb_id: int):
    """目录缺失时显式重试补全：从 TMDB 拉取详情并尝试入目录，不改动个人数据。"""
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=422, detail="type 必须是 movie 或 tv")
    if not TMDB_API_KEY:
        raise HTTPException(status_code=400, detail={
            "code": "missing_tmdb_api_key",
            "message": "未配置 TMDB_API_KEY，无法补全资料",
        })
    entry = await asyncio.to_thread(get_preference_detail, media_type, tmdb_id)
    if not entry:
        raise HTTPException(status_code=404, detail="条目不在片单中")
    if entry.get("catalog_available"):
        entry["status"] = "ready"
        return entry

    from app.fetcher import enrich_titles

    candidate = {
        "tmdb_id": tmdb_id,
        "type": media_type,
        "title": entry.get("title") or "",
        "original_title": entry.get("original_title") or "",
        "overview": "",
        "release_date": entry.get("release_date") or "",
        "poster_url": None,
        "imdb_id": entry.get("imdb_id") or "",
        "providers": [],
        "provider_regions": {},
        "provider_labels": {},
        "origin_countries": [],
    }
    try:
        enriched = await enrich_titles([candidate])
    except Exception as exc:
        raise HTTPException(status_code=502, detail={
            "code": "external_request_failed",
            "message": f"TMDB 请求失败：{exc}",
        }) from exc
    qualified = enriched.get("titles") or []
    pending = enriched.get("pending") or []
    if qualified or pending:
        await asyncio.to_thread(persist_sync_batch, qualified, pending)
    entry = await asyncio.to_thread(get_preference_detail, media_type, tmdb_id)
    if entry.get("catalog_available"):
        entry["status"] = "completed"
        return entry
    entry["status"] = "pending"
    entry["pending_reason"] = (pending[0].get("pending_reason") if pending else None)
    return entry


@router.get("/api/releases")
def recent_releases(days: int = Query(14, ge=1, le=90), limit: int = Query(50, ge=1, le=100),
                    page: int = Query(1, ge=1)):
    return get_recent_releases(days=days, limit=limit, page=page)


@router.patch("/api/titles/{title_id}/status")
def set_title_status(title_id: int, payload: WatchStatusUpdate):
    title = update_title_status(title_id, payload.watch_status)
    if not title:
        raise HTTPException(status_code=404, detail="作品未找到")
    return title


@router.patch("/api/titles/{title_id}/preference")
def set_title_preference(title_id: int, payload: PreferenceUpdate):
    title = update_title_preference(
        title_id,
        priority=payload.priority,
        note=payload.note,
        personal_rating=payload.personal_rating,
    )
    if not title:
        raise HTTPException(status_code=404, detail="作品未加入片单，无法记录个人条目")
    return title


@router.patch("/api/titles/batch")
def batch_update_titles(payload: BatchUpdate):
    if payload.watch_status is None and payload.priority is None:
        raise HTTPException(status_code=422, detail="watch_status 与 priority 至少提供一个")
    try:
        return update_titles_batch(
            payload.ids,
            watch_status=payload.watch_status,
            priority=payload.priority,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/stats")
def stats():
    result = get_stats()
    result["app_version"] = APP_VERSION
    result["build_id"] = BUILD_ID
    result["schema_version"] = SCHEMA_VERSION
    try:
        from app.imdb_data import dataset_status
        result["ratings_dataset"] = dataset_status()
    except Exception:
        result["ratings_dataset"] = {"exists": False, "mtime": None, "age_hours": None, "stale": True}
    return result


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
