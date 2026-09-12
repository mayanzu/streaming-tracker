"""只读查询与列表/详情/统计：健康检查、作品列表、详情、平台、统计。"""

import json
import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

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
from app.db.preferences import (
    touch_preference_snapshot,
    upsert_preference_snapshot,
    write_provider_offers,
)
from app.db.utils import _utc_now
from app.genres import genre_query_variants, normalize_genres
from app.scoring import weighted_rating_sql

IMDB_ID_QUERY_PATTERN = re.compile(r"(?<![A-Za-z0-9])(tt\d{7,12})(?!\d)", re.IGNORECASE)

# IMDb Top 250 同款贝叶斯加权：m = 先验票数，C = 全库均分。
# 只用于排序；详情与卡片继续展示原始 imdb_rating。表达式与表达式索引同源（app.scoring）。
WEIGHTED_RATING_SQL = weighted_rating_sql("t")
# 片单路径：目录缺失（t 为 NULL）沿用旧的 -1 兜底，保证 NULLS LAST 行为一致
WEIGHTED_RATING_LIBRARY_SQL = weighted_rating_sql("t", null_fallback=-1)


def normalize_imdb_query(value):
    """把纯 IMDb ID 或 IMDb 链接归一化为小写 ID；普通关键词返回 None。"""
    match = IMDB_ID_QUERY_PATTERN.search(str(value or "").strip())
    return match.group(1).lower() if match else None


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


def _normalized_genre_values(genre):
    """genre 兼容单值字符串与多值列表，返回去重后的规范题材列表。"""
    if genre is None:
        return []
    values = [genre] if isinstance(genre, str) else list(genre)
    return normalize_genres([str(value)[:40] for value in values if value])


def _build_title_filters(title_type=None, search=None, region=None, min_rating=None,
                         genre=None, max_runtime=None,
                         exclude_watched=None, released_after=None,
                         year_from=None, year_to=None):
    filters = []
    params = []

    if title_type:
        filters.append("t.type = ?")
        params.append(title_type)
    if search:
        imdb_id = normalize_imdb_query(search)
        if imdb_id:
            # R11：合法 IMDb ID/链接走精确检索，先于普通文本相关性搜索
            filters.append("t.imdb_id = ?")
            params.append(imdb_id)
        else:
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
    genre_values = _normalized_genre_values(genre)
    if genre_values:
        # WP-5 题材 facet：规范题材展开为全部别名，多选之间取并集（OR）
        or_filters = []
        for name in genre_values:
            variant_filters = []
            for variant in genre_query_variants(name):
                variant_filters.append("t.genres_json LIKE ?")
                params.append(f'%"{variant}"%')
            or_filters.append("(" + " OR ".join(variant_filters) + ")")
        filters.append("(" + " OR ".join(or_filters) + ")")
    if max_runtime:
        # runtime 缺失视为未知，保留（避免过滤掉大量资料不全作品）
        filters.append("(t.runtime IS NULL OR t.runtime <= ?)")
        params.append(int(max_runtime))
    if released_after:
        # 6.2 近期新片模式：release_date 为 ISO 文本，裸列比较可走 idx_release_date；
        # length 保护排除空值/异常格式，NULL 比较为假自动排除。
        filters.append("length(t.release_date) = 10 AND t.release_date >= ?")
        params.append(str(released_after))
    if year_from is not None:
        filters.append("length(t.release_date) = 10 AND t.release_date >= ?")
        params.append(f"{int(year_from):04d}-01-01")
    if year_to is not None:
        filters.append("length(t.release_date) = 10 AND t.release_date < ?")
        params.append(f"{int(year_to) + 1:04d}-01-01")
    if exclude_watched:
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
               genre=None, max_runtime=None, exclude_watched=False, released_after=None,
               year_from=None, year_to=None):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        offset = (page - 1) * limit
        direction = "DESC" if order == "desc" else "ASC"
        imdb_id = normalize_imdb_query(search) if search else None

        if watch_status:
            # R01：个人片单以 preferences 为主表；目录缺失时用快照展示，
            # 不再因为 JOIN titles 而整条消失。
            from_sql = """
                FROM title_preferences p
                LEFT JOIN titles t ON t.tmdb_id = p.tmdb_id AND t.type = p.type
                LEFT JOIN title_preference_snapshots s
                    ON s.tmdb_id = p.tmdb_id AND s.type = p.type
            """
            filters = ["p.watch_status = ?"]
            params = [watch_status]
            if title_type:
                filters.append("p.type = ?")
                params.append(title_type)
            if imdb_id:
                filters.append("COALESCE(t.imdb_id, s.imdb_id) = ?")
                params.append(imdb_id)
            elif search:
                like = f"%{search}%"
                filters.append(
                    "(COALESCE(t.title, s.title) LIKE ?"
                    " OR COALESCE(t.original_title, s.original_title) LIKE ?"
                    " OR t.overview LIKE ? OR t.director LIKE ? OR t.cast_json LIKE ?)"
                )
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
            genre_values = _normalized_genre_values(genre)
            if genre_values:
                or_filters = []
                for name in genre_values:
                    variant_filters = []
                    for variant in genre_query_variants(name):
                        variant_filters.append("t.genres_json LIKE ?")
                        params.append(f'%"{variant}"%')
                    or_filters.append("(" + " OR ".join(variant_filters) + ")")
                filters.append("(" + " OR ".join(or_filters) + ")")
            if max_runtime:
                filters.append("(t.runtime IS NULL OR t.runtime <= ?)")
                params.append(int(max_runtime))
            if released_after:
                filters.append("length(t.release_date) = 10 AND t.release_date >= ?")
                params.append(str(released_after))
            if year_from is not None:
                filters.append("length(t.release_date) = 10 AND t.release_date >= ?")
                params.append(f"{int(year_from):04d}-01-01")
            if year_to is not None:
                filters.append("length(t.release_date) = 10 AND t.release_date < ?")
                params.append(f"{int(year_to) + 1:04d}-01-01")
            where_sql = " WHERE " + " AND ".join(filters)

            library_sort = {
                "rating": WEIGHTED_RATING_LIBRARY_SQL,
                "release_date": "COALESCE(NULLIF(t.release_date, ''), s.release_date)",
                "updated_at": "p.updated_at",
                "priority": "p.priority",
            }
            sort_col = library_sort.get(sort_by, "p.updated_at")
            order_sql = f"ORDER BY {sort_col} {direction} NULLS LAST, p.updated_at DESC"
            order_params = []
            select_sql = """
                SELECT t.id AS id, p.tmdb_id AS tmdb_id, p.type AS type,
                       COALESCE(t.title, s.title) AS title,
                       COALESCE(t.original_title, s.original_title) AS original_title,
                       t.overview AS overview,
                       COALESCE(NULLIF(t.release_date, ''), s.release_date) AS release_date,
                       COALESCE(t.poster_url, s.poster_url) AS poster_url,
                       t.imdb_rating AS imdb_rating, t.rating_votes AS rating_votes,
                       t.genres_json AS genres_json, t.runtime AS runtime,
                       t.director AS director,
                       p.watch_status AS watch_status,
                       p.updated_at AS status_updated_at,
                       p.priority AS priority, p.note AS note,
                       p.personal_rating AS personal_rating,
                       p.watched_at AS watched_at,
                       (t.id IS NOT NULL) AS catalog_available
            """
            select_params = []
        else:
            sort_map = {
                "added_date": "t.added_date",
                # WP-5：评分排序使用 IMDb 式加权分，避免百票级条目占据榜首；展示仍用原始分
                "rating": WEIGHTED_RATING_SQL,
                "release_date": "t.release_date",
                # 6.7 我的片单排序：最近加入/更新、优先级
                "updated_at": "p.updated_at",
                "priority": "p.priority",
            }
            sort_col = sort_map.get(sort_by, "t.release_date")
            # F-search 相关性排序：精确片名/ID > 片名包含 > 原名 > 主创 > 简介；
            # 非搜索路径保持原排序并追加 id 作为稳定 secondary key。
            if search and not imdb_id:
                order_sql = f"""ORDER BY
                    CASE
                        WHEN t.title = ? THEN 0
                        WHEN t.title LIKE ? THEN 1
                        WHEN t.original_title LIKE ? THEN 2
                        WHEN t.director LIKE ? OR t.cast_json LIKE ? THEN 3
                        ELSE 4
                    END,
                    {sort_col} {direction} NULLS LAST, t.id DESC"""
                order_params = [search, f"{search}%", f"%{search}%", f"%{search}%", f"%{search}%"]
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
                genre=genre,
                max_runtime=max_runtime,
                exclude_watched=exclude_watched,
                released_after=released_after,
                year_from=year_from,
                year_to=year_to,
            )

            select_sql = """
                SELECT t.id, t.tmdb_id, t.type, t.title, t.original_title, t.overview,
                       t.release_date, t.poster_url, t.imdb_rating, t.rating_votes,
                       t.genres_json, t.runtime, t.director,
                       COALESCE(p.watch_status, '') AS watch_status,
                       p.updated_at AS status_updated_at,
                       COALESCE(p.priority, 0) AS priority,
                       1 AS catalog_available"""
            select_params = []
            if search and imdb_id:
                select_sql += """,
                       CASE WHEN t.imdb_id = ? THEN 'imdb' ELSE '' END AS match_reason"""
                select_params = [imdb_id]
            elif search:
                # 6.4 搜索匹配原因：前端据此显示“片名匹配/简介提及”等提示
                like = f"%{search}%"
                select_sql += """,
                       CASE
                           WHEN t.title LIKE ? THEN 'title'
                           WHEN t.original_title LIKE ? THEN 'original'
                           WHEN t.director LIKE ? THEN 'director'
                           WHEN t.cast_json LIKE ? THEN 'cast'
                           WHEN t.overview LIKE ? THEN 'overview'
                           ELSE ''
                       END AS match_reason"""
                select_params = [like, like, like, like, like]

        query = f"""
            {select_sql}
            {from_sql}
            {where_sql}
            {order_sql}
            LIMIT ? OFFSET ?
        """

        cursor.execute(query, [*select_params, *params, *order_params, limit, offset])
        titles = [dict(row) for row in cursor.fetchall()]

        title_ids = [title["id"] for title in titles if title.get("id") is not None]
        provider_map = _fetch_provider_map(cursor, title_ids)
        country_map = _fetch_country_map(cursor, title_ids)
        for title in titles:
            title["providers"] = provider_map.get(title["id"], [])
            title["origin_countries"] = country_map.get(title["id"], [])
            title["catalog_available"] = bool(title.get("catalog_available"))

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
        cursor.execute("""
            SELECT provider_id, provider_group, provider_name, region,
                   monetization, last_seen_at
            FROM title_watch_offers
            WHERE title_id = ? AND is_active = 1
            ORDER BY provider_name, region, monetization
        """, (title_id,))
        title['offers'] = [{
            'provider_id': r['provider_id'],
            'provider_group': r['provider_group'],
            'provider_name': r['provider_name'],
            'region': r['region'],
            'monetization': r['monetization'],
            'verified_at': r['last_seen_at'],
            'legacy': not bool(r['provider_id']),
        } for r in cursor.fetchall()]
        title['origin_countries'] = _fetch_country_map(cursor, [title_id]).get(title_id, [])
        return title
    finally:
        conn.close()


_ALLOWED_STATUSES = {"watchlist", "watching", "watched"}


def _write_preference_status(cursor, tmdb_id, media_type, watch_status, now):
    """按身份写入/删除片单状态（调用方负责事务与缓存失效）。"""
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
        """, (tmdb_id, media_type, watch_status, now, watched_at))
        touch_preference_snapshot(cursor, tmdb_id, media_type, now=now)
    else:
        cursor.execute(
            "DELETE FROM title_preferences WHERE tmdb_id = ? AND type = ?",
            (tmdb_id, media_type),
        )
        cursor.execute(
            "DELETE FROM title_preference_snapshots WHERE tmdb_id = ? AND type = ?",
            (tmdb_id, media_type),
        )


def get_preference_detail(media_type, tmdb_id):
    """按身份读取个人条目（供目录缺失时展示/管理，不依赖内部 title.id）。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.tmdb_id, p.type, p.watch_status, p.updated_at, p.priority,
                   p.note, p.personal_rating, p.watched_at,
                   t.id AS catalog_id, t.title AS catalog_title,
                   t.original_title AS catalog_original_title,
                   t.imdb_id AS catalog_imdb_id, t.poster_url AS catalog_poster_url,
                   t.release_date AS catalog_release_date, t.overview AS catalog_overview,
                   t.imdb_rating AS catalog_rating, t.rating_votes AS catalog_votes,
                   t.genres_json AS catalog_genres, t.runtime AS catalog_runtime,
                   t.director AS catalog_director,
                   s.title AS snapshot_title, s.original_title AS snapshot_original_title,
                   s.imdb_id AS snapshot_imdb_id, s.poster_url AS snapshot_poster_url,
                   s.release_date AS snapshot_release_date
            FROM title_preferences p
            LEFT JOIN titles t ON t.tmdb_id = p.tmdb_id AND t.type = p.type
            LEFT JOIN title_preference_snapshots s
                ON s.tmdb_id = p.tmdb_id AND s.type = p.type
            WHERE p.tmdb_id = ? AND p.type = ?
        """, (tmdb_id, media_type))
        row = cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        catalog_id = data.pop("catalog_id")
        data.update({
            "title": data.pop("catalog_title") or data.pop("snapshot_title"),
            "original_title": data.pop("catalog_original_title") or data.pop("snapshot_original_title"),
            "imdb_id": data.pop("catalog_imdb_id") or data.pop("snapshot_imdb_id"),
            "poster_url": data.pop("catalog_poster_url") or data.pop("snapshot_poster_url"),
            "release_date": data.pop("catalog_release_date") or data.pop("snapshot_release_date"),
            "overview": data.pop("catalog_overview"),
            "imdb_rating": data.pop("catalog_rating"),
            "rating_votes": data.pop("catalog_votes"),
            "genres_json": data.pop("catalog_genres"),
            "runtime": data.pop("catalog_runtime"),
            "director": data.pop("catalog_director"),
            "id": catalog_id,
            "catalog_available": catalog_id is not None,
            "status_updated_at": data.pop("updated_at"),
        })
        data["providers"] = []
        data["origin_countries"] = []
        if catalog_id is not None:
            providers = _fetch_provider_map(cursor, [catalog_id]).get(catalog_id, [])
            data["providers"] = providers
            data["origin_countries"] = _fetch_country_map(cursor, [catalog_id]).get(catalog_id, [])
        return data
    finally:
        conn.close()


def update_preference_by_identity(media_type, tmdb_id, priority=None, note=None,
                                  personal_rating=None):
    """按身份更新个人记录；返回更新后的条目，未加入片单时返回 None。"""
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
            "SELECT 1 FROM title_preferences WHERE tmdb_id = ? AND type = ?",
            (tmdb_id, media_type),
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
            sets.append("personal_rating = NULL" if personal_rating == 0 else "personal_rating = ?")
            if personal_rating != 0:
                params.append(float(personal_rating))
        now = _utc_now()
        sets.append("updated_at = ?")
        params.append(now)
        params.extend((tmdb_id, media_type))
        cursor.execute(
            f"UPDATE title_preferences SET {', '.join(sets)} WHERE tmdb_id = ? AND type = ?",
            params,
        )
        touch_preference_snapshot(cursor, tmdb_id, media_type, now=now)

    invalidate_stats_cache()
    return get_preference_detail(media_type, tmdb_id)


def update_status_by_identity(media_type, tmdb_id, watch_status):
    """按身份更新片单状态；目录缺失的个人条目也能修改/移除。"""
    if watch_status and watch_status not in _ALLOWED_STATUSES:
        raise ValueError("invalid watch status")

    from app.db.connection import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        _write_preference_status(cursor, tmdb_id, media_type, watch_status, _utc_now())

    invalidate_stats_cache()
    return get_preference_detail(media_type, tmdb_id)


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

        _write_preference_status(
            cursor, title["tmdb_id"], title["type"], watch_status, _utc_now(),
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
                    touch_preference_snapshot(cursor, *identity, now=now)
                else:
                    cursor.execute(
                        "DELETE FROM title_preferences WHERE tmdb_id = ? AND type = ?",
                        identity,
                    )
                    cursor.execute(
                        "DELETE FROM title_preference_snapshots WHERE tmdb_id = ? AND type = ?",
                        identity,
                    )
                updated += 1
            elif priority is not None and exists:
                cursor.execute(
                    "UPDATE title_preferences SET priority = ?, updated_at = ? WHERE tmdb_id = ? AND type = ?",
                    (priority, now, *identity),
                )
                touch_preference_snapshot(cursor, *identity, now=now)
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
        now = _utc_now()
        params.append(now)
        params.extend(identity)
        cursor.execute(
            f"UPDATE title_preferences SET {', '.join(sets)} WHERE tmdb_id = ? AND type = ?",
            params,
        )
        touch_preference_snapshot(cursor, *identity, now=now)

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

        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        cursor.execute(
            f"""
            SELECT COUNT(*) AS count FROM titles t
            WHERE {DEFAULT_VISIBILITY_CONDITION_T}
              AND t.first_seen_at IS NOT NULL
              AND t.first_seen_at >= ?
            """,
            (week_ago,),
        )
        added_this_week = cursor.fetchone()["count"]

        # WP-5 题材 facet：json_each 展开 genres_json，归一化后按作品数排序；
        # 老数据（未回填）在此合并英文别名，前端只显示 count > 0 的题材。
        genres = []
        try:
            cursor.execute(f"""
                SELECT je.value AS name, COUNT(DISTINCT t.id) AS count
                FROM titles t, json_each(t.genres_json) je
                WHERE {DEFAULT_VISIBILITY_CONDITION_T}
                  AND t.genres_json IS NOT NULL
                  AND json_valid(t.genres_json)
                GROUP BY je.value
            """)
            merged = {}
            for row in cursor.fetchall():
                for name in normalize_genres([row["name"]]):
                    merged[name] = merged.get(name, 0) + int(row["count"] or 0)
            genres = [
                {"name": name, "count": count}
                for name, count in sorted(merged.items(), key=lambda item: (-item[1], item[0]))
            ]
        except Exception:
            genres = []

        cursor.execute(
            f"""
            SELECT COUNT(*) AS count FROM titles t
            WHERE {DEFAULT_VISIBILITY_CONDITION_T}
              AND t.providers_checked_at IS NOT NULL
            """
        )
        channels_done = cursor.fetchone()["count"]

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
            GROUP BY p.watch_status
        """)
        by_status = {row["watch_status"]: row["count"] for row in cursor.fetchall()}

        # R01：片单总览必须包含目录待补全的条目，单独提示而不是让它们消失
        cursor.execute("""
            SELECT COUNT(*) AS count
            FROM title_preferences p
            WHERE NOT EXISTS (
                SELECT 1 FROM titles t
                WHERE t.tmdb_id = p.tmdb_id AND t.type = p.type
            )
        """)
        library_pending = cursor.fetchone()["count"]

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
            "added_this_week": added_this_week,
            "genres": genres,
            "channels_verified": {"done": channels_done, "total": total},
            "last_update": last_update,
            "last_synced_at": last_synced_at,
            "pending": pending_count,
            "regions": regions,
            "by_status": by_status,
            "library_pending": library_pending,
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

    base_year = int(base.get("release_date", "")[:4]) if str(base.get("release_date") or "")[:4].isdigit() else 0

    scored = []
    fallback = []
    for candidate in candidates:
        candidate_id = candidate["id"]
        candidate_genres = set(_json_list(candidate.get("genres_json")))
        shared_genres = base_genres & candidate_genres
        union_genres = base_genres | candidate_genres
        jaccard = len(shared_genres) / len(union_genres) if union_genres else 0.0
        shared_cast = base_cast & set(_json_list(candidate.get("cast_json")))
        same_director = bool(base_director) and base_director == (candidate.get("director") or "").strip()
        same_country = bool(base_countries & set(country_map.get(candidate_id, [])))
        rating = candidate.get("imdb_rating") or 0
        release = candidate.get("release_date") or ""
        votes = candidate.get("rating_votes") or 0

        if not (shared_genres or shared_cast or same_director):
            continue
        # WP-5 5.7：Jaccard 而非交集计数；票数与年代做多样性惩罚，避免每部喜剧都推出同几部经典
        year = int(str(release)[:4]) if str(release)[:4].isdigit() else 0
        score = (
            4 * jaccard
            + (4 if same_director else 0)
            + min(len(shared_cast), 4) * 2
            + (1 if same_country else 0)
            - (0.5 * math.log10(votes / 1e4) if votes > 1e4 else 0)
            - (0.3 * abs(year - base_year) / 10 if year and base_year else 0)
        )
        if shared_genres:
            reason = "同题材"
        elif same_director:
            reason = "同导演"
        elif shared_cast:
            reason = "同主演"
        else:
            reason = "同产地"
        scored.append((
            score, rating, release,
            _related_payload(candidate, provider_map.get(candidate_id, []), reason),
            (candidate.get("director") or "").strip(), year,
        ))

    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    # 多样性上限：同一导演 ≤2、同年 ≤4，防止推荐列表被同一批作品占满
    results = []
    director_counts = {}
    year_counts = {}
    for item in scored:
        director, year = item[4], item[5]
        if director and director_counts.get(director, 0) >= 2:
            continue
        if year and year_counts.get(year, 0) >= 4:
            continue
        results.append(item[3])
        if director:
            director_counts[director] = director_counts.get(director, 0) + 1
        if year:
            year_counts[year] = year_counts.get(year, 0) + 1
        if len(results) >= limit:
            break

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


_IMPORT_MAX_ITEMS = 5000
_IMPORT_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)


def _valid_import_timestamp(value):
    """日期/时间戳格式不可信时视为缺失，不用它判断新旧。"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _IMPORT_TIMESTAMP_RE.match(value) else None


def _parse_restore_item(item, schema_version):
    """校验单条备份；返回 (parsed, error)。

    schema_version>=2：显式 null/空值表示清空，字段缺失表示保留原值；
    旧版（<2）兼容为 null/缺失都保留原值。
    """
    if not isinstance(item, dict):
        return None, "not_object"
    try:
        tmdb_id = int(item.get("tmdb_id"))
    except (TypeError, ValueError):
        return None, "invalid_tmdb_id"
    if tmdb_id <= 0:
        return None, "invalid_tmdb_id"
    media_type = item.get("type")
    if media_type not in ("movie", "tv"):
        return None, "invalid_type"
    status = item.get("watch_status")
    if status not in _ALLOWED_STATUSES:
        return None, "invalid_status"
    explicit_clear = schema_version >= 2
    fields = {"watch_status": status}

    if "priority" in item and item.get("priority") is not None:
        priority = item.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, (int, float)):
            return None, "invalid_priority"
        if int(priority) != priority or not 0 <= priority <= 2:
            return None, "invalid_priority"
        fields["priority"] = int(priority)
    elif explicit_clear:
        fields["priority"] = 0

    if "note" in item:
        note = item.get("note")
        if note in (None, ""):
            if explicit_clear:
                fields["note"] = None
        elif isinstance(note, str):
            if len(note) > 200:
                return None, "invalid_note"
            fields["note"] = note
        else:
            return None, "invalid_note"

    if "personal_rating" in item:
        rating = item.get("personal_rating")
        if rating in (None, ""):
            if explicit_clear:
                fields["personal_rating"] = None
        elif isinstance(rating, bool) or not isinstance(rating, (int, float)):
            return None, "invalid_rating"
        elif not math.isfinite(float(rating)) or not 0 < float(rating) <= 10:
            return None, "invalid_rating"
        else:
            fields["personal_rating"] = float(rating)

    if "watched_at" in item:
        watched = item.get("watched_at")
        if watched in (None, ""):
            if explicit_clear:
                fields["watched_at"] = None
        else:
            watched = _valid_import_timestamp(watched)
            if watched is None:
                return None, "invalid_watched_at"
            fields["watched_at"] = watched

    snapshot = {}
    title_value = item.get("title")
    if isinstance(title_value, str) and title_value.strip():
        snapshot["title"] = title_value.strip()[:300]
    original_value = item.get("original_title")
    if isinstance(original_value, str) and original_value.strip():
        snapshot["original_title"] = original_value.strip()[:300]
    imdb_value = item.get("imdb_id")
    if isinstance(imdb_value, str) and re.match(r"^tt\d{7,12}$", imdb_value.strip().lower()):
        snapshot["imdb_id"] = imdb_value.strip().lower()
    poster_value = item.get("poster_url")
    if isinstance(poster_value, str) and poster_value.strip().startswith(("http://", "https://")):
        snapshot["poster_url"] = poster_value.strip()[:500]
    release_value = item.get("release_date")
    if isinstance(release_value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", release_value.strip()):
        snapshot["release_date"] = release_value.strip()
    return {
        "tmdb_id": tmdb_id,
        "type": media_type,
        "fields": fields,
        "snapshot": snapshot,
        "backup_updated_at": _valid_import_timestamp(item.get("updated_at")),
    }, None


def _classify_restore(cursor, parsed, force):
    """返回 (action, changed_fields)；action ∈ added/updated/unchanged/protected。"""
    cursor.execute(
        "SELECT watch_status, priority, note, personal_rating, watched_at, updated_at"
        " FROM title_preferences WHERE tmdb_id = ? AND type = ?",
        (parsed["tmdb_id"], parsed["type"]),
    )
    existing = cursor.fetchone()
    if existing is None:
        return "added", list(parsed["fields"].keys())

    changed = []
    for key, value in parsed["fields"].items():
        if key == "personal_rating":
            old = existing["personal_rating"]
            same = (old is None and value is None) or (
                old is not None and value is not None and abs(float(old) - float(value)) < 1e-9
            )
        else:
            same = existing[key] == value
        if not same:
            changed.append(key)
    if not changed:
        return "unchanged", []
    backup_updated = parsed["backup_updated_at"]
    local_updated = existing["updated_at"]
    if not force and backup_updated and local_updated and backup_updated < local_updated:
        return "protected", changed
    return "updated", changed


def import_watchlist(items, schema_version=1, dry_run=False, force=False):
    """从备份恢复片单：统一校验、预览与执行，计数覆盖每一条输入。

    返回 added/updated/unchanged/protected/invalid/duplicate，六者之和等于
    total（即输入条数）；catalog_pending 为目录待补全的已处理条数。
    """
    from app.db.connection import get_db

    if not isinstance(items, list):
        raise ValueError("items must be a list")
    if len(items) > _IMPORT_MAX_ITEMS:
        raise ValueError("too many items")
    schema_version = int(schema_version or 1)
    now = _utc_now()
    counts = {"added": 0, "updated": 0, "unchanged": 0, "protected": 0,
              "invalid": 0, "duplicate": 0}
    field_changes = {key: 0 for key in
                     ("watch_status", "priority", "note", "personal_rating", "watched_at")}
    catalog_pending = 0
    seen = set()

    with get_db() as conn:
        cursor = conn.cursor()
        for item in items:
            parsed, _error = _parse_restore_item(item, schema_version)
            if parsed is None:
                counts["invalid"] += 1
                continue
            identity = (parsed["tmdb_id"], parsed["type"])
            if identity in seen:
                counts["duplicate"] += 1
                continue
            seen.add(identity)

            cursor.execute(
                "SELECT 1 FROM titles WHERE tmdb_id = ? AND type = ?", identity,
            )
            has_catalog = cursor.fetchone() is not None
            if not has_catalog:
                catalog_pending += 1

            action, changed = _classify_restore(cursor, parsed, force)
            counts[action] += 1
            for key in changed:
                field_changes[key] += 1

            if dry_run or action in ("unchanged", "protected"):
                # 偏好字段没变化时仍可补齐缺失的作品快照（例如旧备份导入到已清目录的库）
                if not dry_run and any(parsed["snapshot"].values()):
                    upsert_preference_snapshot(
                        cursor, parsed["tmdb_id"], parsed["type"],
                        fields=parsed["snapshot"], now=now,
                    )
                continue

            fields = parsed["fields"]
            if action == "added":
                cursor.execute("""
                    INSERT INTO title_preferences
                        (tmdb_id, type, watch_status, updated_at, priority, note,
                         personal_rating, watched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    parsed["tmdb_id"], parsed["type"], fields["watch_status"],
                    parsed["backup_updated_at"] or now,
                    fields.get("priority", 0), fields.get("note"),
                    fields.get("personal_rating"), fields.get("watched_at"),
                ))
            else:
                sets, params = [], []
                for key, value in fields.items():
                    sets.append(f"{key} = ?")
                    params.append(value)
                sets.append("updated_at = ?")
                params.append(parsed["backup_updated_at"] or now)
                params.extend(identity)
                cursor.execute(
                    f"UPDATE title_preferences SET {', '.join(sets)}"
                    " WHERE tmdb_id = ? AND type = ?",
                    params,
                )

            if has_catalog or any(parsed["snapshot"].values()):
                upsert_preference_snapshot(
                    cursor, parsed["tmdb_id"], parsed["type"],
                    fields=parsed["snapshot"], now=now,
                )

    if not dry_run:
        invalidate_stats_cache()
    return {
        "schema_version": schema_version,
        "dry_run": bool(dry_run),
        **counts,
        "skipped": counts["invalid"] + counts["duplicate"],
        "total": len(items),
        "processed": counts["added"] + counts["updated"]
                     + counts["unchanged"] + counts["protected"],
        "catalog_pending": catalog_pending,
        "field_changes": field_changes,
    }


def export_watchlist():
    """导出全部片单偏好（含作品快照与个人记录），用于备份/迁移。

    目录缺失时回退偏好快照，保证导出→清目录→导入→再导出的身份与展示字段不丢。
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.tmdb_id, p.type, p.watch_status, p.updated_at,
                   p.priority, p.note, p.personal_rating, p.watched_at,
                   COALESCE(t.title, s.title) AS title,
                   COALESCE(t.original_title, s.original_title) AS original_title,
                   COALESCE(t.imdb_id, s.imdb_id) AS imdb_id,
                   t.imdb_rating,
                   COALESCE(t.poster_url, s.poster_url) AS poster_url,
                   COALESCE(t.release_date, s.release_date) AS release_date
            FROM title_preferences p
            LEFT JOIN titles t ON t.tmdb_id = p.tmdb_id AND t.type = p.type
            LEFT JOIN title_preference_snapshots s
                ON s.tmdb_id = p.tmdb_id AND s.type = p.type
            ORDER BY p.updated_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_recent_releases(days=14, limit=50, page=1):
    """[今天-N 天, 今天] 内上映/首播的可见作品；未来日期不再计入。

    时间边界用服务端本地日期统一计算，返回 total/has_next 支持分页。
    """
    days = max(int(days), 0)
    limit = max(int(limit), 1)
    page = max(int(page), 1)
    today = date.today()
    window_start = (today - timedelta(days=days)).isoformat()
    window_end = today.isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # P02：release_date 为 ISO 文本，裸列范围比较可走 idx_release_date；
        # length 保护排除空值/异常格式（date() 包裹会让索引失效）。
        base_from = f"""
            FROM titles t
            LEFT JOIN title_preferences p ON t.tmdb_id = p.tmdb_id AND t.type = p.type
            WHERE {DEFAULT_VISIBILITY_CONDITION_T}
              AND length(t.release_date) = 10
              AND t.release_date BETWEEN ? AND ?
        """
        cursor.execute(
            f"""
            SELECT t.*, COALESCE(p.watch_status, '') AS watch_status
            {base_from}
            ORDER BY t.release_date DESC, t.imdb_rating DESC NULLS LAST, t.id DESC
            LIMIT ? OFFSET ?
            """,
            (window_start, window_end, limit, (page - 1) * limit),
        )
        titles = [dict(row) for row in cursor.fetchall()]
        provider_map = _fetch_provider_map(cursor, [t["id"] for t in titles])
        for title in titles:
            title["providers"] = provider_map.get(title["id"], [])
        count_key = ("releases", window_start, window_end)
        total = _count_cache_get(count_key)
        if total is None:
            cursor.execute(f"SELECT COUNT(*) {base_from}", (window_start, window_end))
            total = cursor.fetchone()[0]
            _count_cache_set(count_key, total)
        total_pages = math.ceil(total / limit) if limit > 0 else 0
        return {
            "titles": titles,
            "days": days,
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "window_start": window_start,
            "window_end": window_end,
        }
    finally:
        conn.close()
