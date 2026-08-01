"""作品数据写入与缓存读取：插入/更新、IMDb ID 回填、缓存查询、国家回填。"""

from datetime import date

from app.db.connection import get_db_connection
from app.db.queries import _fetch_country_map
from app.db.utils import _normalize_country_codes, _normalize_rating_source, _utc_now


def update_title_imdb_id(title_id, imdb_id):
    if not title_id or not imdb_id:
        return

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE titles SET imdb_id=?, last_synced_at=? WHERE id=?",
            (imdb_id, _utc_now(), title_id),
        )
        conn.commit()
    finally:
        conn.close()


def insert_title(title_data, conn=None):
    """插入或更新作品。可传入外部连接以复用（如同步循环），不传则自建。"""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    cursor = conn.cursor()
    rating, rating_source, rating_votes = _normalize_rating_source(title_data)
    countries_supplied = "origin_countries" in title_data
    country_codes = _normalize_country_codes(title_data.get("origin_countries"))
    countries_synced_at = title_data.get("countries_synced_at")
    if countries_supplied and not countries_synced_at:
        countries_synced_at = title_data.get("last_synced_at") or _utc_now()
    if rating is None:
        if owns_conn:
            conn.close()
        raise ValueError("trusted IMDb rating is required")

    try:
        if owns_conn:
            cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT id FROM titles WHERE tmdb_id = ? AND type = ?",
            (title_data["tmdb_id"], title_data["type"]),
        )
        existing = cursor.fetchone()

        if existing:
            cursor.execute("""
                UPDATE titles SET
                    imdb_id=COALESCE(?, imdb_id),
                    title=COALESCE(NULLIF(?, ''), title),
                    original_title=COALESCE(NULLIF(?, ''), original_title),
                    type=?,
                    overview=COALESCE(NULLIF(?, ''), overview),
                    release_date=COALESCE(NULLIF(?, ''), release_date),
                    poster_url=COALESCE(?, poster_url), imdb_rating=?,
                    rating_source=?, rating_votes=?,
                    first_seen_at=COALESCE(first_seen_at, added_date, created_at, ?),
                    last_seen_at=?, last_synced_at=?,
                    countries_synced_at=COALESCE(?, countries_synced_at)
                WHERE tmdb_id=? AND type=?
            """, (
                title_data.get("imdb_id"),
                title_data["title"], title_data.get("original_title"),
                title_data["type"], title_data.get("overview"),
                title_data.get("release_date"), title_data.get("poster_url"),
                rating, rating_source, rating_votes,
                title_data.get("first_seen_at") or _utc_now(),
                title_data.get("last_seen_at") or _utc_now(),
                title_data.get("last_synced_at") or _utc_now(),
                countries_synced_at,
                title_data["tmdb_id"], title_data["type"],
            ))
            title_id = existing["id"]
        else:
            cursor.execute("""
                INSERT INTO titles
                (tmdb_id, imdb_id, title, original_title, type, overview, release_date,
                 poster_url, imdb_rating, rating_source, rating_votes, added_date,
                 first_seen_at, last_seen_at, last_synced_at, countries_synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                title_data["tmdb_id"], title_data.get("imdb_id"), title_data["title"],
                title_data.get("original_title"), title_data["type"],
                title_data.get("overview"), title_data.get("release_date"),
                title_data.get("poster_url"), rating, rating_source, rating_votes,
                title_data.get("added_date") or date.today().isoformat(),
                title_data.get("first_seen_at") or _utc_now(),
                title_data.get("last_seen_at") or _utc_now(),
                title_data.get("last_synced_at") or _utc_now(),
                countries_synced_at,
            ))
            title_id = cursor.lastrowid

        observed_at = title_data.get("last_seen_at") or _utc_now()
        provider_regions = title_data.get("provider_regions") or {}
        for provider in title_data.get("providers") or []:
            cursor.execute(
                "INSERT OR IGNORE INTO title_providers (title_id, provider_name) VALUES (?,?)",
                (title_id, provider),
            )
            regions = provider_regions.get(provider) or [""]
            for region in regions:
                cursor.execute("""
                    INSERT INTO title_provider_availability
                        (title_id, provider_name, region, monetization_type,
                         first_seen_at, last_seen_at, is_active)
                    VALUES (?, ?, ?, 'mixed', ?, ?, 1)
                    ON CONFLICT(title_id, provider_name, region, monetization_type)
                    DO UPDATE SET last_seen_at=excluded.last_seen_at, is_active=1
                """, (title_id, provider, region, observed_at, observed_at))

        if countries_supplied:
            cursor.execute("DELETE FROM title_countries WHERE title_id=?", (title_id,))
            cursor.executemany(
                "INSERT INTO title_countries (title_id, country_code) VALUES (?, ?)",
                [(title_id, code) for code in country_codes],
            )

        if owns_conn:
            conn.commit()
        return title_id
    except Exception:
        if owns_conn:
            conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()


def get_title_cache(identities):
    identities = list(dict.fromkeys(identities))
    if not identities:
        return {}
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cache = {}
        for offset in range(0, len(identities), 400):
            chunk = identities[offset:offset + 400]
            clauses = " OR ".join("(tmdb_id=? AND type=?)" for _ in chunk)
            params = [value for identity in chunk for value in identity]
            cursor.execute(f"SELECT * FROM titles WHERE {clauses}", params)
            for row in cursor.fetchall():
                item = dict(row)
                cache[(item["type"], item["tmdb_id"])] = item
        by_id = {item["id"]: item for item in cache.values()}
        country_map = _fetch_country_map(cursor, list(by_id))
        for title_id, item in by_id.items():
            item["origin_countries"] = country_map.get(title_id, [])
        return cache
    finally:
        conn.close()


def persist_title_countries(items):
    if not items:
        return 0
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        synced_at = _utc_now()
        for item in items:
            title_id = item["id"]
            codes = _normalize_country_codes(item.get("origin_countries"))
            cursor.execute("DELETE FROM title_countries WHERE title_id=?", (title_id,))
            cursor.executemany(
                "INSERT INTO title_countries (title_id, country_code) VALUES (?, ?)",
                [(title_id, code) for code in codes],
            )
            cursor.execute(
                "UPDATE titles SET countries_synced_at=? WHERE id=?",
                (item.get("countries_synced_at") or synced_at, title_id),
            )
        conn.commit()
        return len(items)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
