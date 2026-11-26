"""相关推荐：优先使用 TMDB 协同过滤结果，匹配本地可见作品，不足时用本地相似度兜底。

- TMDB /recommendations 并发拉取 page 1 + page 2 并按原序去重，只取本地“可见 + 有中文”条目；
- 引入 SQLite L2 持久化缓存（默认 7 天；空结果 6 小时），容器/进程重启后无需重新冷启动；
- 首次冷访问给予短时等待窗口（0.85s），TMDB 响应快时首屏即出协同推荐；超时则不阻塞 UI，
  先返回本地相似度结果并保留后台预热任务供下次瞬间命中；
- 失败/无 key 负缓存 10 分钟，避免网络异常时持续打外网；
- L1 进程内缓存 24 小时（空结果 1 小时，硬上限 512 条）。
"""

import asyncio
import json
import logging
import sqlite3
import ssl
import threading
import time

import httpx

from app.config import TMDB_API_KEY
from app.db import get_db_connection
from app.db.connection import DEFAULT_VISIBILITY_CONDITION_T, TRUSTED_RATING_CONDITION_T
from app.db.queries import _fetch_provider_map, get_related_titles
from app.db.utils import _utc_now
from app.fetcher import fetch_tmdb

logger = logging.getLogger(__name__)

_CACHE_TTL = 24 * 3600
_EMPTY_CACHE_TTL = 3600
_NEGATIVE_TTL = 600
_DB_CACHE_TTL = 7 * 24 * 3600
_DB_EMPTY_CACHE_TTL = 6 * 3600
_MAX_CACHE_ENTRIES = 512
_FIRST_VISIT_WAIT_SECONDS = 1.0
# 复用系统 CA SSLContext，避免每次新建 AsyncClient 重新解析证书库消耗 ~100-600ms
_SSL_CONTEXT = ssl.create_default_context()
# UI 路径：单次 3 秒内必须出结果，失败走负缓存而不是重试
_TMDB_TIMEOUT = httpx.Timeout(3.0, connect=2.0)

_cache = {}
_cache_lock = threading.Lock()
# cache key -> in-flight 预热任务：同一 key 未完成时跳过重复调度（R4-07）
_warmup_tasks: dict[tuple, asyncio.Task] = {}


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
            overflow = len(_cache) - _MAX_CACHE_ENTRIES
            if overflow > 0:
                # 清过期后仍超容量：按到期时间淘汰最旧项，维持硬上限
                for stale_key, _ in sorted(
                    _cache.items(), key=lambda item: item[1][0]
                )[:overflow]:
                    _cache.pop(stale_key, None)


def _db_cache_get(media_type, tmdb_id):
    """读取 SQLite L2 持久化缓存；未建表/损坏/过期时安全返回 None。"""
    conn = None
    try:
        conn = get_db_connection()
        row = conn.execute(
            "SELECT rec_ids_json, expires_at FROM related_cache"
            " WHERE media_type = ? AND tmdb_id = ?",
            (media_type, int(tmdb_id)),
        ).fetchone()
        if not row or float(row["expires_at"] or 0) <= time.time():
            return None
        parsed = json.loads(row["rec_ids_json"] or "[]")
        if not isinstance(parsed, list):
            return None
        return [int(item) for item in parsed if item is not None]
    except (sqlite3.Error, ValueError, TypeError):
        return None
    finally:
        if conn is not None:
            conn.close()


def _db_cache_set(media_type, tmdb_id, ids, ttl):
    """写入 SQLite L2 持久化缓存；写库异常只记录 debug 不影响主流程。"""
    conn = None
    try:
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO related_cache (media_type, tmdb_id, rec_ids_json, fetched_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(media_type, tmdb_id) DO UPDATE SET
                rec_ids_json = excluded.rec_ids_json,
                fetched_at = excluded.fetched_at,
                expires_at = excluded.expires_at
            """,
            (
                media_type,
                int(tmdb_id),
                json.dumps([int(item) for item in ids]),
                _utc_now(),
                time.time() + ttl,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.debug("related_cache write skipped: %s", exc)
    finally:
        if conn is not None:
            conn.close()


async def _fetch_tmdb_recommendation_ids(tmdb_id, media_type, check_db_cache=True):
    cache_key = ("tmdb", media_type, tmdb_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not TMDB_API_KEY:
        # 无 key 直接负缓存，不发起任何外网请求
        _cache_set(cache_key, [], _NEGATIVE_TTL)
        return []

    if check_db_cache:
        db_ids = await asyncio.to_thread(_db_cache_get, media_type, tmdb_id)
        if db_ids is not None:
            _cache_set(cache_key, db_ids, _CACHE_TTL if db_ids else _EMPTY_CACHE_TTL)
            return db_ids

    endpoint = f"/{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}/recommendations"
    try:
        async with httpx.AsyncClient(timeout=_TMDB_TIMEOUT, verify=_SSL_CONTEXT) as client:
            p1, p2 = await asyncio.gather(
                fetch_tmdb(endpoint, {"page": 1}, client=client, retries=0),
                fetch_tmdb(endpoint, {"page": 2}, client=client, retries=0),
                return_exceptions=True,
            )
    except Exception as exc:
        logger.info("TMDB recommendations unavailable tmdb_id=%s: %s", tmdb_id, exc)
        _cache_set(cache_key, [], _NEGATIVE_TTL)
        return []

    if isinstance(p1, Exception) and isinstance(p2, Exception):
        # P01：远端完全不可达同样写入负缓存，10 分钟内不再打外网
        logger.info("TMDB recommendations unavailable tmdb_id=%s: %s", tmdb_id, p1)
        _cache_set(cache_key, [], _NEGATIVE_TTL)
        return []

    ids = []
    seen = set()
    for page_payload in (p1, p2):
        if isinstance(page_payload, Exception) or not isinstance(page_payload, dict):
            continue
        for item in page_payload.get("results") or []:
            raw_id = item.get("id")
            if raw_id is None:
                continue
            tid = int(raw_id)
            if tid in seen:
                continue
            seen.add(tid)
            ids.append(tid)

    _cache_set(cache_key, ids, _CACHE_TTL if ids else _EMPTY_CACHE_TTL)
    await asyncio.to_thread(
        _db_cache_set,
        media_type,
        tmdb_id,
        ids,
        _DB_CACHE_TTL if ids else _DB_EMPTY_CACHE_TTL,
    )
    return ids


def _on_warmup_done(task, cache_key):
    # 仅清理仍属于该 key 的任务，避免被同 key 的新任务覆盖后误删
    if _warmup_tasks.get(cache_key) is task:
        _warmup_tasks.pop(cache_key, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.debug("TMDB recommendation warmup failed: %s", exc)


def _schedule_warmup(tmdb_id, media_type, check_db_cache=True):
    """后台预热远端推荐缓存；失败不影响当前请求。

    R4-07：同一 cache key 已有未完成的预热任务时跳过重复调度，
    避免高频浏览同一作品时放大远端请求；任务结束即从映射中清理。
    """
    if not TMDB_API_KEY:
        return None
    cache_key = ("tmdb", media_type, tmdb_id)
    pending = _warmup_tasks.get(cache_key)
    if pending is not None and not pending.done():
        return pending
    try:
        coro = (
            _fetch_tmdb_recommendation_ids(tmdb_id, media_type)
            if check_db_cache
            else _fetch_tmdb_recommendation_ids(tmdb_id, media_type, check_db_cache=False)
        )
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        return None
    _warmup_tasks[cache_key] = task
    task.add_done_callback(lambda done: _on_warmup_done(done, cache_key))
    return task


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
            item["reason"] = "TMDB 推荐"
        return ordered
    finally:
        conn.close()


async def get_related_titles_async(title_id, limit=12):
    """P01：L1/L2 缓存优先，冷访问短等待 TMDB 预热，超时或不足由本地相似度无缝补全。"""
    base = await asyncio.to_thread(_load_base_sync, title_id)
    if not base:
        return []

    results = []

    if base.get("tmdb_id"):
        cache_key = ("tmdb", base["type"], base["tmdb_id"])
        cached_ids = _cache_get(cache_key)
        if cached_ids is None and TMDB_API_KEY:
            db_ids = await asyncio.to_thread(_db_cache_get, base["type"], base["tmdb_id"])
            if db_ids is not None:
                _cache_set(cache_key, db_ids, _CACHE_TTL if db_ids else _EMPTY_CACHE_TTL)
                cached_ids = db_ids
        if cached_ids is None and TMDB_API_KEY:
            task = _schedule_warmup(base["tmdb_id"], base["type"], check_db_cache=False)
            if task is not None and _FIRST_VISIT_WAIT_SECONDS > 0:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=_FIRST_VISIT_WAIT_SECONDS
                    )
                except (asyncio.TimeoutError, Exception):
                    pass
                cached_ids = _cache_get(cache_key)
        if cached_ids:
            # 远端缓存命中：排除自身后按 TMDB 顺序优先展示
            filtered_ids = [tid for tid in cached_ids if tid != base["tmdb_id"]]
            if filtered_ids:
                results = await asyncio.to_thread(
                    _load_local_sync, base["type"], filtered_ids, limit
                )

    if len(results) < limit:
        # R4-07：只有需要补足时才创建并等待本地查询，避免 coroutine 未 await
        local = await asyncio.to_thread(
            get_related_titles, title_id, max(limit * 2, limit)
        )
        existing = {item["id"] for item in results}
        for item in local:
            if item["id"] in existing:
                continue
            results.append(item)
            existing.add(item["id"])
            if len(results) >= limit:
                break
    return results[:limit]
