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


def _build_title_filters(title_type=None, search=None, region=None, min_rating=None,
                         watch_status=None, genre=None, max_runtime=None,
                         exclude_watched=None, released_after=None):
    filters = []
    params = []

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
    if genre:
        # genres_json 为 JSON 数组字符串，用带引号的 LIKE 做精确元素匹配
        filters.append("t.genres_json LIKE ?")
        params.append(f'%"{genre}"%')
    if max_runtime:
        # runtime 缺失视为未知，保留（避免过滤掉大量资料不全作品）
        filters.append("(t.runtime IS NULL OR t.runtime <= ?)")
        params.append(int(max_runtime))
    if released_after:
        # 6.2 近期新片模式：date() 兼容空值/非法日期，NULL 比较为假自动排除
        filters.append("date(t.release_date) >= date(?)")
        params.append(released_after)
    if exclude_watched and not watch_status:
        # 6.5 “只看未看”：排除已加入“已看”的作品；已看的个人记录本身不受影响。
        filters.append("""
            NOT EXISTS (
                SELECT 1
                FROM title_preferences watched_filter
                WHERE watched_filter.tmdb_id = t.tmdb_id
                  AND watched_filter.type = t.type
                  AND watched_filter.watch_status = 'watched'
            )
        """)
    if watch_status:
        filters.append("p.watch_status = ?")
        params.append(watch_status)
        # F06/F07 个人片单与目录准入分离：已收藏作品即使失去目录资格
        #（无评分过宽限期、仅其他平台、无中文）仍必须可见、可移除。
        # 因此片单视图不套用可信评分与主平台可见性，只保留基础连接条件。
        where_sql = " WHERE " + " AND ".join(filters) if filters else ""
        return where_sql, params

    filters.append(TRUSTED_RATING_CONDITION_T)
    filters.append(ZH_CONDITION_T)
    # 目录默认隐藏“仅其他平台且非亚洲产地”的作品；平台只在详情展示，不再作为筛选项
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


def get_titles(page=1, limit=20, sort_by="release_date", order="desc",
               title_type=None, search=None, region=None, min_rating=None, watch_status=None,
               genre=None, max_runtime=None, exclude_watched=False, released_after=None):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        offset = (page - 1) * limit

        sort_map = {
            "added_date": "t.added_date",
            "rating": "t.imdb_rating",
            "release_date": "t.release_date",
            # 6.7 我的片单排序：最近加入/更新、优先级
            "updated_at": "p.updated_at",
            "priority": "p.priority",
        }
        sort_col = sort_map.get(sort_by, "t.release_date")
        direction = "DESC" if order == "desc" else "ASC"
        # F-search 相关性排序：精确片名/ID > 片名包含 > 原名 > 主创 > 简介；
        # 非搜索路径保持原排序并追加 id 作为稳定 secondary key。
        if search and not watch_status:
            order_sql = f"""ORDER BY
                CASE
                    WHEN t.title = ? THEN 0
                    WHEN t.imdb_id = ? THEN 0
                    WHEN t.title LIKE ? THEN 1
                    WHEN t.original_title LIKE ? THEN 2
                    WHEN t.director LIKE ? OR t.cast_json LIKE ? THEN 3
                    ELSE 4
                END,
                {sort_col} {direction} NULLS LAST, t.id DESC"""
            order_params = [search, search, f"{search}%", f"%{search}%", f"%{search}%", f"%{search}%"]
        else:
            order_sql = f"ORDER BY {sort_col} {direction} NULLS LAST, t.id DESC"
            order_params = []

        from_sql = """
            FROM titles t
            LEFT JOIN title_preferences p ON t.tmdb_id = p.tmdb_id AND t.type = p.type
        """
        where_sql, params = _build_title_filters(
            title_type=title_type,
            search=search,
            region=region,
            min_rating=min_rating,
            watch_status=watch_status,
            genre=genre,
            max_runtime=max_runtime,
            exclude_watched=exclude_watched,
            released_after=released_after,
        )

        select_sql = """
            SELECT t.id, t.tmdb_id, t.type, t.title, t.original_title, t.overview,
                   t.release_date, t.poster_url, t.imdb_rating, t.rating_votes,
                   t.genres_json, t.runtime, t.director,
                   COALESCE(p.watch_status, '') AS watch_status,
                   p.updated_at AS status_updated_at,
                   COALESCE(p.priority, 0) AS priority"""
        select_params = []
        if search and not watch_status:
            # 6.4 搜索匹配原因：前端据此显示“片名匹配/简介提及”等提示
            like = f"%{search}%"
            select_sql += """,
                   CASE
                       WHEN t.title LIKE ? THEN 'title'
                       WHEN t.original_title LIKE ? THEN 'original'
                       WHEN t.director LIKE ? THEN 'director'
                       WHEN t.cast_json LIKE ? THEN 'cast'
                       WHEN t.overview LIKE ? THEN 'overview'
                       WHEN t.imdb_id = ? THEN 'imdb'
                       ELSE ''
                   END AS match_reason"""
            select_params = [like, like, like, like, like, search]

        query = f"""
            {select_sql}
            {from_sql}
            {where_sql}
            {order_sql}
            LIMIT ? OFFSET ?
        """

        cursor.execute(query, [*select_params, *params, *order_params, limit, offset])
        titles = [dict(row) for row in cursor.fetchall()]

        provider_map = _fetch_provider_map(cursor, [title["id"] for title in titles])
        country_map = _fetch_country_map(cursor, [title["id"] for title in titles])
        for title in titles:
            title["providers"] = provider_map.get(title["id"], [])
            title["origin_countries"] = country_map.get(title["id"], [])

        count_query = f"SELECT COUNT(*) {from_sql} {where_sql}"
        # 过滤结果总数缓存：同一筛选条件翻页时 COUNT 占主要耗时（可见性条件需全表评估）。
        # 60 秒 TTL + 写入失效，保证正确性优先。
        count_key = (where_sql, tuple(params))
        total = _count_cache_get(count_key)
        if total is None:
            cursor.execute(count_query, params)
            total = cursor.fetchone()[0]
            _count_cache_set(count_key, total)
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
                   p.updated_at AS status_updated_at,
                   COALESCE(p.priority, 0) AS priority,
                   p.note AS note,
                   p.personal_rating AS personal_rating,
                   p.watched_at AS watched_at
            FROM titles t
            LEFT JOIN title_preferences p ON t.tmdb_id = p.tmdb_id AND t.type = p.type
            WHERE t.id = ? AND {TRUSTED_RATING_CONDITION_T}
        """, (title_id,))
        row = cursor.fetchone()
        if not row:
            # F07 回退：已收藏作品即使失去目录资格仍可打开/移除。
            cursor.execute("""
                SELECT t.*, COALESCE(p.watch_status, '') AS watch_status,
                       p.updated_at AS status_updated_at,
                       COALESCE(p.priority, 0) AS priority,
                       p.note AS note,
                       p.personal_rating AS personal_rating,
                       p.watched_at AS watched_at
                FROM titles t
                LEFT JOIN title_preferences p ON t.tmdb_id = p.tmdb_id AND t.type = p.type
                WHERE t.id = ?
                  AND p.watch_status IS NOT NULL
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
        cursor.execute("""
            SELECT provider_name, region, monetization_type, last_seen_at, provider_label
            FROM title_provider_availability
            WHERE title_id = ? AND is_active=1
            ORDER BY provider_name, region
        """, (title_id,))
        details = {}
        for r in cursor.fetchall():
            name = r['provider_name']
            item = details.setdefault(name, {
                'provider': name, 'regions': [], 'labels': [],
                'monetization': r['monetization_type'], 'last_seen_at': r['last_seen_at'],
            })
            if r['region'] and r['region'] not in item['regions']:
                item['regions'].append(r['region'])
            for label in (r['provider_label'] or '').split(' / '):
                label = label.strip()
                if label and label not in item['labels']:
                    item['labels'].append(label)
            if r['last_seen_at'] and (not item['last_seen_at'] or r['last_seen_at'] > item['last_seen_at']):
                item['last_seen_at'] = r['last_seen_at']
        title['provider_details'] = list(details.values())
        title['origin_countries'] = _fetch_country_map(cursor, [title_id]).get(title_id, [])
        return title
    finally:
        conn.close()


def update_title_status(title_id, watch_status):
    """更新个人片单状态；空字符串表示移出片单。6.7：标记已看时记录观看日期。"""
    allowed = {"watchlist", "watching", "watched"}
    if watch_status and watch_status not in allowed:
        raise ValueError("invalid watch status")

    from app.db.connection import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        # F07：前置查询不套可信评分过滤，否则过期收藏无法移除。
        cursor.execute(
            "SELECT tmdb_id, type FROM titles t WHERE id = ?",
            (title_id,),
        )
        title = cursor.fetchone()
        if not title:
            return None

        identity = (title["tmdb_id"], title["type"])
        now = _utc_now()
        if watch_status:
            watched_at = now if watch_status == "watched" else None
            cursor.execute("""
                INSERT INTO title_preferences (tmdb_id, type, watch_status, updated_at, watched_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tmdb_id, type) DO UPDATE SET
                    watch_status = excluded.watch_status,
                    updated_at = excluded.updated_at,
                    watched_at = CASE
                        WHEN excluded.watch_status = 'watched'
                            THEN COALESCE(title_preferences.watched_at, excluded.watched_at)
                        ELSE title_preferences.watched_at
                    END
            """, (*identity, watch_status, now, watched_at))
        else:
            cursor.execute(
                "DELETE FROM title_preferences WHERE tmdb_id = ? AND type = ?",
                identity,
            )

    # F05：片单写入后立即失效统计缓存，否则 loadStats 会被旧值覆盖。
    invalidate_stats_cache()
    return get_title_detail(title_id)


def update_titles_batch(ids, watch_status=None, priority=None):
    """6.7 批量操作：把同一状态/优先级应用到多部作品。watch_status='' 表示批量移出片单。"""
    if watch_status is not None and watch_status not in ("", "watchlist", "watching", "watched"):
        raise ValueError("invalid watch status")
    if priority is not None and priority not in (0, 1, 2):
        raise ValueError("invalid priority")
    if watch_status is None and priority is None:
        raise ValueError("nothing to update")

    cleaned = []
    for value in ids or []:
        try:
            num = int(value)
        except (TypeError, ValueError):
            continue
        if num > 0 and num not in cleaned:
            cleaned.append(num)
    if not cleaned:
        return {"updated": 0, "skipped": 0}
    if len(cleaned) > 200:
        raise ValueError("too many ids")

    from app.db.connection import get_db

    updated = 0
    skipped = 0
    now = _utc_now()
    with get_db() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in cleaned)
        cursor.execute(
            f"SELECT id, tmdb_id, type FROM titles WHERE id IN ({placeholders})",
            cleaned,
        )
        rows = cursor.fetchall()
        for row in rows:
            identity = (row["tmdb_id"], row["type"])
            cursor.execute(
                "SELECT 1 FROM title_preferences WHERE tmdb_id = ? AND type = ?",
                identity,
            )
            exists = cursor.fetchone() is not None
            if watch_status is not None:
                if watch_status:
                    watched_at = now if watch_status == "watched" else None
                    priority_set = (
                        "priority = excluded.priority," if priority is not None else ""
                    )
                    cursor.execute(f"""
                        INSERT INTO title_preferences
                            (tmdb_id, type, watch_status, updated_at, priority, watched_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(tmdb_id, type) DO UPDATE SET
                            watch_status = excluded.watch_status,
                            updated_at = excluded.updated_at,
                            {priority_set}
                            watched_at = CASE
                                WHEN excluded.watch_status = 'watched'
                                    THEN COALESCE(title_preferences.watched_at, excluded.watched_at)
                                ELSE title_preferences.watched_at
                            END
                    """, (*identity, watch_status, now,
                          priority if priority is not None else 0, watched_at))
                else:
                    cursor.execute(
                        "DELETE FROM title_preferences WHERE tmdb_id = ? AND type = ?",
                        identity,
                    )
                updated += 1
            elif priority is not None and exists:
                cursor.execute(
                    "UPDATE title_preferences SET priority = ?, updated_at = ? WHERE tmdb_id = ? AND type = ?",
                    (priority, now, *identity),
                )
                updated += 1
            else:
                # 仅设置优先级但不在片单中：不创建记录
                skipped += 1
        skipped += len(cleaned) - len(rows)

    invalidate_stats_cache()
    return {"updated": updated, "skipped": skipped}


def update_title_preference(title_id, priority=None, note=None, personal_rating=None):
    """6.7：更新个人记录（优先级/备注/个人评分）。作品必须在片单中，否则返回 None。"""
    if priority is not None and priority not in (0, 1, 2):
        raise ValueError("invalid priority")
    if personal_rating is not None and not 0 <= personal_rating <= 10:
        raise ValueError("invalid personal rating")
    if note is not None:
        note = str(note)[:200]

    from app.db.connection import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tmdb_id, type FROM titles t WHERE id = ?",
            (title_id,),
        )
        title = cursor.fetchone()
        if not title:
            return None
        identity = (title["tmdb_id"], title["type"])
        cursor.execute(
            "SELECT 1 FROM title_preferences WHERE tmdb_id = ? AND type = ?",
            identity,
        )
        if not cursor.fetchone():
            return None

        sets, params = [], []
        if priority is not None:
            sets.append("priority = ?")
            params.append(int(priority))
        if note is not None:
            sets.append("note = ?")
            params.append(note)
        if personal_rating is not None:
            if personal_rating == 0:
                # 0 表示清除个人评分（前端“未评分”选项发送 0）
                sets.append("personal_rating = NULL")
            else:
                sets.append("personal_rating = ?")
                params.append(float(personal_rating))
        sets.append("updated_at = ?")
        params.append(_utc_now())
        params.extend(identity)
        cursor.execute(
            f"UPDATE title_preferences SET {', '.join(sets)} WHERE tmdb_id = ? AND type = ?",
            params,
        )

    invalidate_stats_cache()
    return get_title_detail(title_id)


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

        cursor.execute("""
            SELECT p.watch_status, COUNT(*) AS count
            FROM title_preferences p
            JOIN titles t ON t.tmdb_id = p.tmdb_id AND t.type = p.type
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
# 列表过滤总数缓存：key=(where_sql, params)
_COUNT_CACHE_TTL = 60
_COUNT_CACHE_MAX = 64
_COUNT_LOCK = threading.Lock()
_count_cache = {}


def _count_cache_get(key):
    now = time.monotonic()
    with _COUNT_LOCK:
        entry = _count_cache.get(key)
        if entry is None:
            return None
        if entry[0] <= now:
            _count_cache.pop(key, None)
            return None
        return entry[1]


def _count_cache_set(key, value):
    now = time.monotonic()
    with _COUNT_LOCK:
        _count_cache[key] = (now + _COUNT_CACHE_TTL, value)
        if len(_count_cache) > _COUNT_CACHE_MAX:
            for stale_key in [k for k, v in _count_cache.items() if v[0] <= now]:
                _count_cache.pop(stale_key, None)
            while len(_count_cache) > _COUNT_CACHE_MAX:
                _count_cache.pop(next(iter(_count_cache)), None)


def _count_cache_clear():
    with _COUNT_LOCK:
        _count_cache.clear()


def _cache_read(cache):
    with _CACHE_LOCK:
        if cache["value"] is not None and cache["expires"] > time.monotonic():
            return cache["value"]
    return None


def _cache_write(cache, value, ttl):
    with _CACHE_LOCK:
        cache["value"] = value
        cache["expires"] = time.monotonic() + ttl


def _cache_invalidate(cache):
    with _CACHE_LOCK:
        cache["value"] = None
        cache["expires"] = 0.0


def invalidate_stats_cache():
    """F05：片单/同步/导入写入后失效统计与过滤计数缓存，避免旧计数覆盖乐观更新。"""
    _cache_invalidate(_stats_cache)
    _count_cache_clear()


def invalidate_catalog_caches():
    _cache_invalidate(_stats_cache)
    _count_cache_clear()


def _related_payload(candidate, providers, reason=""):
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
        "reason": reason,
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
        reason_bits = []
        if shared_genres:
            reason_bits.append(f"同题材·{sorted(shared_genres)[0]}")
        if same_director:
            reason_bits.append("同导演")
        if shared_cast:
            reason_bits.append(f"同主演·{sorted(shared_cast)[0]}")
        if same_provider:
            reason_bits.append("同平台")
        reason = " / ".join(reason_bits[:2])
        scored.append((score, rating, release, _related_payload(candidate, provider_map.get(candidate_id, []), reason)))

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
        # 硬上限：优先清过期，仍超限则按插入序淘汰最旧
        if len(_related_cache) > 256:
            for key in [
                key for key, value in _related_cache.items()
                if now - value[0] >= _RELATED_CACHE_TTL
            ]:
                _related_cache.pop(key, None)
        while len(_related_cache) > 256:
            _related_cache.pop(next(iter(_related_cache)), None)
    return results


def import_watchlist(items):
    """从备份恢复片单：按 (tmdb_id, type) 幂等合并，目录缺失也不丢偏好。"""
    from app.db.connection import get_db

    allowed = {"watchlist", "watching", "watched"}
    added = updated = skipped = 0
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    if len(items) > 5000:
        raise ValueError("too many items")
    now = _utc_now()
    with get_db() as conn:
        cursor = conn.cursor()
        for item in items:
            try:
                tmdb_id = int(item.get("tmdb_id"))
                media_type = item.get("type")
                status = item.get("watch_status")
            except (TypeError, ValueError, AttributeError):
                skipped += 1
                continue
            if media_type not in ("movie", "tv") or status not in allowed:
                skipped += 1
                continue
            cursor.execute(
                "SELECT watch_status FROM title_preferences WHERE tmdb_id=? AND type=?",
                (tmdb_id, media_type),
            )
            existing = cursor.fetchone()
            # 6.7：备份中的个人记录字段（缺失或非法则保留现有值）
            priority = item.get("priority")
            priority = int(priority) if isinstance(priority, (int, float)) and 0 <= priority <= 2 else None
            personal_rating = item.get("personal_rating")
            personal_rating = float(personal_rating) if isinstance(personal_rating, (int, float)) and 0 < personal_rating <= 10 else None
            note = item.get("note")
            note = str(note)[:200] if note else None
            watched_at = item.get("watched_at") or None
            updated_at = item.get("updated_at") or now
            if existing is None:
                cursor.execute("""
                    INSERT INTO title_preferences
                        (tmdb_id, type, watch_status, updated_at, priority, note,
                         personal_rating, watched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (tmdb_id, media_type, status, updated_at,
                      priority if priority is not None else 0,
                      note, personal_rating, watched_at))
                added += 1
            else:
                sets = ["watch_status = ?", "updated_at = ?"]
                params = [status, updated_at]
                if priority is not None:
                    sets.append("priority = ?"); params.append(priority)
                if note is not None:
                    sets.append("note = ?"); params.append(note)
                if personal_rating is not None:
                    sets.append("personal_rating = ?"); params.append(personal_rating)
                if watched_at:
                    sets.append("watched_at = ?"); params.append(watched_at)
                params.extend((tmdb_id, media_type))
                cursor.execute(
                    f"UPDATE title_preferences SET {', '.join(sets)} WHERE tmdb_id=? AND type=?",
                    params,
                )
                if existing["watch_status"] != status:
                    updated += 1
    invalidate_stats_cache()
    return {"added": added, "updated": updated, "skipped": skipped,
            "total": added + updated + skipped}


def export_watchlist():
    """导出全部片单偏好（含作品快照与个人记录），用于备份/迁移。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.tmdb_id, p.type, p.watch_status, p.updated_at,
                   p.priority, p.note, p.personal_rating, p.watched_at,
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
