from fastapi import APIRouter, Query, HTTPException, Request
from app.database import get_titles, get_title_detail, get_providers, get_stats
from app.config import PROVIDERS
from app.scheduler import get_scheduler_status

router = APIRouter()


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
    return title


@router.get("/api/providers")
async def list_providers():
    return {
        "providers": get_providers(),
        "available": list(PROVIDERS.keys()),
        "total": get_stats()["total"],
    }


@router.get("/api/stats")
async def stats():
    return get_stats()


@router.get("/api/sync/status")
async def sync_status(request: Request):
    return get_scheduler_status(getattr(request.app.state, "scheduler", None))
