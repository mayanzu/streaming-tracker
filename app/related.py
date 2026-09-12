"""相关推荐：优先使用 TMDB 协同过滤结果，匹配本地可见作品，不足时用本地相似度兜底。

- TMDB /recommendations 结果按 TMDB 顺序保留，只取本地"可见 + 有中文"的条目；
- TMDB 请求失败 / 结果不足时，回退到 app.db.queries.get_related_titles 的本地打分；
- TMDB 结果列表进程内缓存 24 小时（空结果缓存 1 小时），失败不缓存。
"""

import asyncio
import logging
import threading
import time

import httpx

from app.db import get_db_connection
from app.db.connection import DEFAULT_VISIBILITY_CONDITION_T, TRUSTED_RATING_CONDITION_T
from app.db.queries import _fetch_provider_map, get_related_titles
from app.fetcher import fetch_tmdb

logger = logging.getLogger(__name__)

_CACHE_TTL = 24 * 3600
_EMPTY_CACHE_TTL = 3600
_MAX_CACHE_ENTRIES = 512
_TMDB_TIMEOUT = httpx.Timeout(6.0, connect=4.0)

_cache = {}
_cache_lock = threading.Lock()


def _cache_get(key):
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        expires_at, value = entry
        if expires_at <= now:
            _cache.pop(key, None)
            return None
        return value


def _cache_set(key, value, ttl):
    now = time.monotonic()
    with _cache_lock:
        _cache[key] = (now + ttl, value)
        if len(_cache) > _MAX_CACHE_ENTRIES:
            for stale_key in [
                item for item, (expire, _) in _cache.items() if expire <= now
            ]:
                _cache.pop(stale_key, None)


async def _fetch_tmdb_recommendation_ids(tmdb_id, media_type):
    cache_key = ("tmdb", media_type, tmdb_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    endpoint = f"/{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}/recommendations"
    try:
        async with httpx.AsyncClient(timeout=_TMDB_TIMEOUT) as client:
            payload = await fetch_tmdb(endpoint, {"page": 1}, client=client)
        ids = [
            int(item["id"])
            for item in (payload.get("results") or [])
            if item.get("id") is not None
        ]
    except Exception as exc:
        # 失败不缓存，下次详情页再试
        logger.warning("TMDB recommendations failed tmdb_id=%s: %s", tmdb_id, exc)
        return []

    _cache_set(cache_key, ids, _CACHE_TTL if ids else _EMPTY_CACHE_TTL)
    return ids


def _load_base_sync(title_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT t.tmdb_id, t.type FROM titles t"
            f" WHERE t.id = ? AND {TRUSTED_RATING_CONDITION_T}",
            (title_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _load_local_sync(media_type, tmdb_ids, limit):
    """按 TMDB 推荐顺序取本地可见作品。"""
    if not tmdb_ids:
        return []
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        rows_by_tmdb_id = {}
        for offset in range(0, len(tmdb_ids), 200):
            chunk = tmdb_ids[offset:offset + 200]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(f"""
                SELECT t.id, t.tmdb_id, t.type, t.title, t.original_title, t.release_date,
                       t.poster_url, t.imdb_rating, t.rating_votes,
                       COALESCE(p.watch_status, '') AS watch_status
                FROM titles t
                LEFT JOIN title_preferences p ON t.tmdb_id = p.tmdb_id AND t.type = p.type
                WHERE t.type = ? AND t.tmdb_id IN ({placeholders})
                  AND {DEFAULT_VISIBILITY_CONDITION_T}
            """, [media_type, *chunk])
            for row in cursor.fetchall():
                rows_by_tmdb_id.setdefault(row["tmdb_id"], dict(row))

        ordered = [
            rows_by_tmdb_id[tmdb_id]
            for tmdb_id in tmdb_ids
            if tmdb_id in rows_by_tmdb_id
        ][:limit]
        provider_map = _fetch_provider_map(cursor, [item["id"] for item in ordered])
        for item in ordered:
            item["providers"] = provider_map.get(item["id"], [])
            item["reason"] = "TMDB 同看推荐"
        return ordered
    finally:
        conn.close()


async def get_related_titles_async(title_id, limit=12):
    base = await asyncio.to_thread(_load_base_sync, title_id)
    if not base:
        return []

    results = []
    if base.get("tmdb_id"):
        recommended_ids = await _fetch_tmdb_recommendation_ids(base["tmdb_id"], base["type"])
        if recommended_ids:
            results = await asyncio.to_thread(
                _load_local_sync, base["type"], recommended_ids, limit
            )

    if len(results) < limit:
        existing = {item["id"] for item in results}
        local = await asyncio.to_thread(get_related_titles, title_id, max(limit * 2, limit))
        for item in local:
            if item["id"] in existing:
                continue
            results.append(item)
            existing.add(item["id"])
            if len(results) >= limit:
                break
    return results[:limit]
