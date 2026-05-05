from fastapi import APIRouter, Query, HTTPException
import logging
from app.database import get_titles, get_title_detail, get_providers
from app.config import PROVIDERS

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/titles")
async def list_titles(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    provider: str = Query(None),
    sort_by: str = Query("rating", pattern="^(added_date|rating|release_date)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    type: str = Query(None, pattern="^(movie|tv)?$"),
    search: str = Query(None),
    year: str = Query(None),
    min_rating: float = Query(None, ge=0, le=10),
):
    return get_titles(
        page=page, limit=limit, provider=provider,
        sort_by=sort_by, order=order, title_type=type,
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
    return {"providers": get_providers(), "available": list(PROVIDERS.keys())}


@router.get("/api/stats")
async def get_stats():
    from app.database import get_db_connection
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as total FROM titles")
    total = cursor.fetchone()['total']

    cursor.execute("SELECT type, COUNT(*) as count FROM titles GROUP BY type")
    by_type = {row['type']: row['count'] for row in cursor.fetchall()}

    cursor.execute("SELECT AVG(imdb_rating) as avg_rating FROM titles WHERE imdb_rating IS NOT NULL")
    avg = cursor.fetchone()['avg_rating']

    cursor.execute("SELECT MAX(added_date) as last_update FROM titles")
    last_update = cursor.fetchone()['last_update']

    cursor.execute("SELECT DISTINCT substr(release_date,1,4) as y FROM titles WHERE release_date != '' ORDER BY y DESC")
    years = [row['y'] for row in cursor.fetchall() if row['y']]

    conn.close()
    return {
        "total": total,
        "by_type": by_type,
        "avg_rating": round(avg, 1) if avg else 0,
        "last_update": last_update,
        "years": years,
    }
