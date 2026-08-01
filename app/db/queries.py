"""只读查询与列表/详情/统计：健康检查、作品列表、详情、平台、统计。"""

import math

from app.db.connection import (
    TRUSTED_RATING_CONDITION,
    TRUSTED_RATING_CONDITION_T,
    UNTRUSTED_RATING_CONDITION,
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
        cursor.execute(f"SELECT COUNT(*) FROM titles WHERE {TRUSTED_RATING_CONDITION}")
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
        filters.append("(t.title LIKE ? OR t.original_title LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
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
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT tp.provider_name, COUNT(DISTINCT tp.title_id) as count
            FROM title_provider_availability tp
            JOIN titles t ON t.id = tp.title_id
            WHERE tp.is_active=1 AND {TRUSTED_RATING_CONDITION_T}
            GROUP BY tp.provider_name
            ORDER BY count DESC
        """)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_stats():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        cursor.execute(f"SELECT COUNT(*) as total FROM titles t WHERE {TRUSTED_RATING_CONDITION_T}")
        total = cursor.fetchone()["total"]

        cursor.execute(f"""
            SELECT t.type, COUNT(*) as count
            FROM titles t
            WHERE {TRUSTED_RATING_CONDITION_T}
            GROUP BY t.type
        """)
        by_type = {row["type"]: row["count"] for row in cursor.fetchall()}

        cursor.execute(
            f"SELECT AVG(t.imdb_rating) as avg_rating FROM titles t WHERE {TRUSTED_RATING_CONDITION_T}"
        )
        avg = cursor.fetchone()["avg_rating"]

        cursor.execute(
            f"SELECT MAX(t.added_date) as last_update FROM titles t WHERE {TRUSTED_RATING_CONDITION_T}"
        )
        last_update = cursor.fetchone()["last_update"]

        cursor.execute(
            f"SELECT MAX(t.last_synced_at) as last_synced_at FROM titles t WHERE {TRUSTED_RATING_CONDITION_T}"
        )
        last_synced_at = cursor.fetchone()["last_synced_at"]

        cursor.execute("SELECT COUNT(*) AS count FROM pending_titles")
        pending_count = cursor.fetchone()["count"]

        cursor.execute(f"""
            SELECT tc.country_code, COUNT(DISTINCT tc.title_id) AS count
            FROM title_countries tc
            JOIN titles t ON t.id = tc.title_id
            WHERE {TRUSTED_RATING_CONDITION_T}
            GROUP BY tc.country_code
            ORDER BY count DESC, tc.country_code
        """)
        regions = [dict(row) for row in cursor.fetchall()]

        cursor.execute(f"""
            SELECT p.watch_status, COUNT(*) AS count
            FROM title_preferences p
            JOIN titles t ON t.tmdb_id = p.tmdb_id AND t.type = p.type
            WHERE {TRUSTED_RATING_CONDITION_T}
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

        return {
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
