"""只读查询与列表/详情/统计：健康检查、作品列表、详情、平台、统计。"""

import json
import math
import threading
import time

from app.db.connection import (
    ASIAN_ORIGIN_CONDITION_T,
    DEFAULT_VISIBILITY_CONDITION,
    DEFAULT_VISIBILITY_CONDITION_T,
    PRIMARY_PROVIDER_CONDITION_T,
    TRUSTED_RATING_CONDITION,
    TRUSTED_RATING_CONDITION_T,
    UNTRUSTED_RATING_CONDITION,
    ZH_CONDITION_T,
    get_db_connection,
)
from app.db.utils import _utc_now


def check_database():
    conn = None
    try:
        conn = get_db_connection()
        conn.execute("SELECT COUNT(*) FROM titles")
        return True
    except Exception:
        return False
    finally:
        if conn:
            conn.close()


def count_titles():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM titles WHERE {DEFAULT_VISIBILITY_CONDITION}")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def count_untrusted_titles():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT COUNT(*)
            FROM titles
            WHERE {UNTRUSTED_RATING_CONDITION}
        """)
        return cursor.fetchone()[0]
    finally:
        conn.close()


def purge_untrusted_titles():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            DELETE FROM titles
            WHERE id IN (
                SELECT id FROM titles WHERE {UNTRUSTED_RATING_CONDITION}
            )
        """)
        removed = cursor.rowcount
        cursor.execute("""
            DELETE FROM title_providers WHERE title_id NOT IN (SELECT id FROM titles)
        """)
        conn.commit()
        return removed
    finally:
        conn.close()


def purge_all_titles():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM titles")
        total = cursor.fetchone()[0]
        cursor.execute("DELETE FROM titles")
        cursor.execute("DELETE FROM title_providers")
        conn.commit()
        return total
    finally:
        conn.close()


def _build_title_filters(provider=None, title_type=None, search=None, region=None, min_rating=None,
                         watch_status=None):
    filters = []
    params = []

    if provider:
        filters.append("""
            EXISTS (
                SELECT 1
                FROM title_provider_availability provider_filter
                WHERE provider_filter.title_id = t.id
                  AND provider_filter.is_active = 1
                  AND provider_filter.provider_name = ?
            )
        """)
        params.append(provider)
    if title_type:
        filters.append("t.type = ?")
        params.append(title_type)
    if search:
        filters.append("(t.title LIKE ? OR t.original_title LIKE ? OR t.overview LIKE ? OR t.director LIKE ? OR t.cast_json LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like, like, like])
    if region:
        filters.append("""
            EXISTS (
                SELECT 1
                FROM title_countries country_filter
                WHERE country_filter.title_id = t.id
                  AND country_filter.country_code = ?
            )
        """)
        params.append(region.upper())
    if min_rating is not None:
        filters.append("t.imdb_rating >= ?")
        params.append(min_rating)
    if watch_status:
        filters.append("p.watch_status = ?")
        params.append(watch_status)

    filters.append(TRUSTED_RATING_CONDITION_T)
    filters.append(ZH_CONDITION_T)
    if provider != "others" and not watch_status:
        # 默认列表隐藏仅"其他平台"的作品，但亚洲产地内容例外；
        # 显式筛选其他平台或查看个人片单时不过滤
        filters.append(f"({PRIMARY_PROVIDER_CONDITION_T} OR {ASIAN_ORIGIN_CONDITION_T})")

    where_sql = " WHERE " + " AND ".join(filters) if filters else ""
    return where_sql, params


def _fetch_provider_map(cursor, title_ids):
    if not title_ids:
        return {}

    placeholders = ",".join("?" for _ in title_ids)
    cursor.execute(
        f"""
        SELECT title_id, provider_name
        FROM title_provider_availability
        WHERE is_active=1 AND title_id IN ({placeholders})
        GROUP BY title_id, provider_name
        ORDER BY provider_name
        """,
        title_ids,
    )

    provider_map = {title_id: [] for title_id in title_ids}
    for row in cursor.fetchall():
        provider_map[row["title_id"]].append(row["provider_name"])
    return provider_map


def _fetch_country_map(cursor, title_ids):
    if not title_ids:
        return {}
    placeholders = ",".join("?" for _ in title_ids)
    cursor.execute(
        f"""
        SELECT title_id, country_code
        FROM title_countries
        WHERE title_id IN ({placeholders})
        ORDER BY country_code
        """,
        title_ids,
    )
    country_map = {title_id: [] for title_id in title_ids}
    for row in cursor.fetchall():
        country_map[row["title_id"]].append(row["country_code"])
    return country_map


def get_titles(page=1, limit=20, provider=None, sort_by="release_date", order="desc",
               title_type=None, search=None, region=None, min_rating=None, watch_status=None):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        offset = (page - 1) * limit

        sort_map = {
            "added_date": "t.added_date",
            "rating": "t.imdb_rating",
            "release_date": "t.release_date",
        }
        sort_col = sort_map.get(sort_by, "t.release_date")
        direction = "DESC" if order == "desc" else "ASC"

        from_sql = """
            FROM titles t
            LEFT JOIN title_preferences p ON t.tmdb_id = p.tmdb_id AND t.type = p.type
        """
        where_sql, params = _build_title_filters(
            provider=provider,
            title_type=title_type,
            search=search,
            region=region,
            min_rating=min_rating,
            watch_status=watch_status,
        )

        query = f"""
            SELECT t.*, COALESCE(p.watch_status, '') AS watch_status,
                   p.updated_at AS status_updated_at
            {from_sql}
            {where_sql}
            ORDER BY {sort_col} {direction} NULLS LAST
            LIMIT ? OFFSET ?
        """

        cursor.execute(query, [*params, limit, offset])
        titles = [dict(row) for row in cursor.fetchall()]

        provider_map = _fetch_provider_map(cursor, [title["id"] for title in titles])
        country_map = _fetch_country_map(cursor, [title["id"] for title in titles])
        for title in titles:
            title["providers"] = provider_map.get(title["id"], [])
            title["origin_countries"] = country_map.get(title["id"], [])

        count_query = f"SELECT COUNT(*) {from_sql} {where_sql}"
        cursor.execute(count_query, params)
        total = cursor.fetchone()[0]
    finally:
        conn.close()

    total_pages = math.ceil(total / limit) if limit > 0 else 0
    return {
        "titles": titles,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "has_next": page < total_pages,
    }


def get_title_detail(title_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT t.*, COALESCE(p.watch_status, '') AS watch_status,
                   p.updated_at AS status_updated_at
            FROM titles t
            LEFT JOIN title_preferences p ON t.tmdb_id = p.tmdb_id AND t.type = p.type
            WHERE t.id = ? AND {TRUSTED_RATING_CONDITION_T}
        """, (title_id,))
        row = cursor.fetchone()
        if not row:
            return None
        title = dict(row)
        cursor.execute("""
            SELECT provider_name
            FROM title_provider_availability
            WHERE title_id = ? AND is_active=1
            GROUP BY provider_name
            ORDER BY provider_name
        """, (title_id,))
        title['providers'] = [r['provider_name'] for r in cursor.fetchall()]
        title['origin_countries'] = _fetch_country_map(cursor, [title_id]).get(title_id, [])
        return title
    finally:
        conn.close()


def update_title_status(title_id, watch_status):
    """更新个人片单状态；空字符串表示移出片单。"""
    allowed = {"watchlist", "watching", "watched"}
    if watch_status and watch_status not in allowed:
        raise ValueError("invalid watch status")

    from app.db.connection import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT tmdb_id, type FROM titles t WHERE id = ? AND {TRUSTED_RATING_CONDITION_T}",
            (title_id,),
        )
        title = cursor.fetchone()
        if not title:
            return None

        identity = (title["tmdb_id"], title["type"])
        if watch_status:
            cursor.execute("""
                INSERT INTO title_preferences (tmdb_id, type, watch_status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tmdb_id, type) DO UPDATE SET
                    watch_status = excluded.watch_status,
                    updated_at = excluded.updated_at
            """, (*identity, watch_status, _utc_now()))
        else:
            cursor.execute(
                "DELETE FROM title_preferences WHERE tmdb_id = ? AND type = ?",
                identity,
            )

    return get_title_detail(title_id)


def get_providers():
    cached = _cache_read(_providers_cache)
    if cached is not None:
        return cached
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # 先算出可见作品集合再统计可用性，避免对每一行 availability 重复计算可见性条件
        cursor.execute(f"""
            SELECT tp.provider_name, COUNT(DISTINCT tp.title_id) as count
            FROM title_provider_availability tp
            WHERE tp.is_active = 1
              AND tp.title_id IN (
                  SELECT t.id FROM titles t
                  WHERE {TRUSTED_RATING_CONDITION_T} AND {ZH_CONDITION_T}
              )
            GROUP BY tp.provider_name
            ORDER BY count DESC
        """)
        result = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
    _cache_write(_providers_cache, result, 60)
    return result


def get_stats():
    cached = _cache_read(_stats_cache)
    if cached is not None:
        return cached
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        cursor.execute(f"SELECT COUNT(*) as total FROM titles t WHERE {DEFAULT_VISIBILITY_CONDITION_T}")
        total = cursor.fetchone()["total"]

        cursor.execute(f"""
            SELECT t.type, COUNT(*) as count
            FROM titles t
            WHERE {DEFAULT_VISIBILITY_CONDITION_T}
            GROUP BY t.type
        """)
        by_type = {row["type"]: row["count"] for row in cursor.fetchall()}

        cursor.execute(
            f"SELECT AVG(t.imdb_rating) as avg_rating FROM titles t WHERE {DEFAULT_VISIBILITY_CONDITION_T}"
        )
        avg = cursor.fetchone()["avg_rating"]

        cursor.execute(
            f"SELECT MAX(t.added_date) as last_update FROM titles t WHERE {DEFAULT_VISIBILITY_CONDITION_T}"
        )
        last_update = cursor.fetchone()["last_update"]

        cursor.execute(
            f"SELECT MAX(t.last_synced_at) as last_synced_at FROM titles t WHERE {DEFAULT_VISIBILITY_CONDITION_T}"
        )
        last_synced_at = cursor.fetchone()["last_synced_at"]

        cursor.execute("SELECT COUNT(*) AS count FROM pending_titles")
        pending_count = cursor.fetchone()["count"]

        cursor.execute(f"""
            SELECT tc.country_code, COUNT(DISTINCT tc.title_id) AS count
            FROM title_countries tc
            JOIN titles t ON t.id = tc.title_id
            WHERE {DEFAULT_VISIBILITY_CONDITION_T}
            GROUP BY tc.country_code
            ORDER BY count DESC, tc.country_code
        """)
        regions = [dict(row) for row in cursor.fetchall()]

        cursor.execute(f"""
            SELECT p.watch_status, COUNT(*) AS count
            FROM title_preferences p
            JOIN titles t ON t.tmdb_id = p.tmdb_id AND t.type = p.type
            WHERE {DEFAULT_VISIBILITY_CONDITION_T}
            GROUP BY p.watch_status
        """)
        by_status = {row["watch_status"]: row["count"] for row in cursor.fetchall()}

        cursor.execute(
            """
            SELECT *
            FROM sync_runs
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """
        )
        latest_sync = cursor.fetchone()

        result = {
            "total": total,
            "by_type": by_type,
            "avg_rating": round(avg, 1) if avg else 0,
            "last_update": last_update,
            "last_synced_at": last_synced_at,
            "pending": pending_count,
            "regions": regions,
            "by_status": by_status,
            "latest_sync": dict(latest_sync) if latest_sync else None,
        }
    finally:
        conn.close()
    _cache_write(_stats_cache, result, 30)
    return result


def get_titles_missing_countries(limit=0):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        query = """
            SELECT id, tmdb_id, type, title
            FROM titles
            WHERE countries_synced_at IS NULL
            ORDER BY id
        """
        params = []
        if limit and limit > 0:
            query += " LIMIT ?"
            params.append(limit)
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _json_list(value):
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


_RELATED_CACHE_TTL = 300
_related_cache = {}
_related_cache_lock = threading.Lock()

_CACHE_LOCK = threading.Lock()
_stats_cache = {"expires": 0.0, "value": None}
_providers_cache = {"expires": 0.0, "value": None}


def _cache_read(cache):
    with _CACHE_LOCK:
        if cache["value"] is not None and cache["expires"] > time.monotonic():
            return cache["value"]
    return None


def _cache_write(cache, value, ttl):
    with _CACHE_LOCK:
        cache["value"] = value
        cache["expires"] = time.monotonic() + ttl


def _related_payload(candidate, providers):
    return {
        "id": candidate["id"],
        "tmdb_id": candidate["tmdb_id"],
        "type": candidate["type"],
        "title": candidate["title"],
        "original_title": candidate.get("original_title"),
        "release_date": candidate.get("release_date"),
        "poster_url": candidate.get("poster_url"),
        "imdb_rating": candidate.get("imdb_rating"),
        "rating_votes": candidate.get("rating_votes"),
        "watch_status": candidate.get("watch_status") or "",
        "providers": providers,
    }


def get_related_titles(title_id, limit=12):
    """基于题材/演员/导演/产地/平台交集的本地相似度推荐（5 分钟进程内缓存）。

    打分：共享题材×4、同导演×4、共享演员×2、同产地×1、同平台×1；
    没有任何题材/演员/导演交集的候选直接排除，数量不足时用同题材/同产地按评分兜底。
    """
    now = time.monotonic()
    cache_key = (title_id, limit)
    with _related_cache_lock:
        cached = _related_cache.get(cache_key)
        if cached and now - cached[0] < _RELATED_CACHE_TTL:
            return cached[1]

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT t.* FROM titles t
            WHERE t.id = ? AND {TRUSTED_RATING_CONDITION_T}
        """, (title_id,))
        base_row = cursor.fetchone()
        if not base_row:
            return []
        base = dict(base_row)
        base_genres = set(_json_list(base.get("genres_json")))
        base_cast = set(_json_list(base.get("cast_json")))
        base_director = (base.get("director") or "").strip()
        base_countries = set(_fetch_country_map(cursor, [title_id]).get(title_id, []))
        base_providers = set(_fetch_provider_map(cursor, [title_id]).get(title_id, []))

        cursor.execute(f"""
            SELECT t.id, t.tmdb_id, t.type, t.title, t.original_title, t.release_date,
                   t.poster_url, t.imdb_rating, t.rating_votes, t.genres_json,
                   t.cast_json, t.director, COALESCE(p.watch_status, '') AS watch_status
            FROM titles t
            LEFT JOIN title_preferences p ON t.tmdb_id = p.tmdb_id AND t.type = p.type
            WHERE t.id != ?
              AND t.type = ?
              AND {DEFAULT_VISIBILITY_CONDITION_T}
        """, (title_id, base["type"]))
        candidates = [dict(row) for row in cursor.fetchall()]
        candidate_ids = [item["id"] for item in candidates]
        country_map = _fetch_country_map(cursor, candidate_ids)
        provider_map = _fetch_provider_map(cursor, candidate_ids)
    finally:
        conn.close()

    scored = []
    fallback = []
    for candidate in candidates:
        candidate_id = candidate["id"]
        shared_genres = base_genres & set(_json_list(candidate.get("genres_json")))
        shared_cast = base_cast & set(_json_list(candidate.get("cast_json")))
        same_director = bool(base_director) and base_director == (candidate.get("director") or "").strip()
        same_country = bool(base_countries & set(country_map.get(candidate_id, [])))
        same_provider = bool(base_providers & set(provider_map.get(candidate_id, [])))
        rating = candidate.get("imdb_rating") or 0
        release = candidate.get("release_date") or ""

        if not (shared_genres or shared_cast or same_director):
            continue
        score = (
            len(shared_genres) * 4
            + (4 if same_director else 0)
            + len(shared_cast) * 2
            + (1 if same_country else 0)
            + (1 if same_provider else 0)
        )
        scored.append((score, rating, release, _related_payload(candidate, provider_map.get(candidate_id, []))))

    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    results = [item[3] for item in scored[:limit]]

    if len(results) < limit and not base_genres:
        # 仅当基础作品没有题材数据（老数据）时，才用同产地按评分兜底，避免塞入无关作品
        picked = {item["id"] for item in results}
        for candidate in candidates:
            if candidate["id"] in picked:
                continue
            same_country = bool(base_countries & set(country_map.get(candidate["id"], [])))
            if not same_country:
                continue
            fallback.append((
                candidate.get("imdb_rating") or 0,
                candidate.get("release_date") or "",
                _related_payload(candidate, provider_map.get(candidate["id"], [])),
            ))
        fallback.sort(key=lambda item: (item[0], item[1]), reverse=True)
        for item in fallback:
            results.append(item[2])
            if len(results) >= limit:
                break

    with _related_cache_lock:
        _related_cache[cache_key] = (now, results)
        if len(_related_cache) > 256:
            for key in [
                key for key, value in _related_cache.items()
                if now - value[0] >= _RELATED_CACHE_TTL
            ]:
                _related_cache.pop(key, None)
    return results


def export_watchlist():
    """导出全部片单偏好（含作品快照），用于备份/迁移。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.tmdb_id, p.type, p.watch_status, p.updated_at,
                   t.title, t.original_title, t.imdb_id, t.imdb_rating, t.poster_url
            FROM title_preferences p
            LEFT JOIN titles t ON t.tmdb_id = p.tmdb_id AND t.type = p.type
            ORDER BY p.updated_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_recent_releases(days=14, limit=50):
    """近 N 天上映/首播的高分作品，用于新片提醒/RSS。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT t.*, COALESCE(p.watch_status, '') AS watch_status
            FROM titles t
            LEFT JOIN title_preferences p ON t.tmdb_id = p.tmdb_id AND t.type = p.type
            WHERE {DEFAULT_VISIBILITY_CONDITION_T}
              AND date(t.release_date) >= date('now', ?)
            ORDER BY t.release_date DESC, t.imdb_rating DESC
            LIMIT ?
        """, (f"-{int(days)} days", int(limit)))
        titles = [dict(row) for row in cursor.fetchall()]
        provider_map = _fetch_provider_map(cursor, [t["id"] for t in titles])
        for title in titles:
            title["providers"] = provider_map.get(title["id"], [])
        return titles
    finally:
        conn.close()
