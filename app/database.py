import sqlite3
import os
from app.config import DATABASE_URL


def get_db_connection():
    db_dir = os.path.dirname(DATABASE_URL)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DATABASE_URL)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER UNIQUE,
            title TEXT NOT NULL,
            original_title TEXT,
            type TEXT CHECK(type IN ('movie', 'tv')),
            overview TEXT,
            release_date TEXT,
            poster_url TEXT,
            imdb_rating REAL,
            added_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS title_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER,
            provider_name TEXT,
            FOREIGN KEY (title_id) REFERENCES titles(id) ON DELETE CASCADE,
            UNIQUE(title_id, provider_name)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_id ON titles(tmdb_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_imdb_rating ON titles(imdb_rating)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_type ON titles(type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_added_date ON titles(added_date)")

    conn.commit()
    conn.close()


def insert_title(title_data):
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT id FROM titles WHERE tmdb_id = ?", (title_data['tmdb_id'],))
        existing = cursor.fetchone()

        if existing:
            cursor.execute("""
                UPDATE titles SET
                    title=?, original_title=?, type=?, overview=?,
                    release_date=?, poster_url=?, imdb_rating=?, added_date=?
                WHERE tmdb_id=?
            """, (
                title_data['title'], title_data['original_title'],
                title_data['type'], title_data['overview'],
                title_data['release_date'], title_data['poster_url'],
                title_data['imdb_rating'], title_data['added_date'],
                title_data['tmdb_id']
            ))
            title_id = existing['id']
        else:
            cursor.execute("""
                INSERT INTO titles
                (tmdb_id, title, original_title, type, overview, release_date,
                 poster_url, imdb_rating, added_date)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                title_data['tmdb_id'], title_data['title'],
                title_data['original_title'], title_data['type'],
                title_data['overview'], title_data['release_date'],
                title_data['poster_url'], title_data['imdb_rating'],
                title_data['added_date']
            ))
            title_id = cursor.lastrowid

        for provider in title_data.get('providers') or []:
            cursor.execute(
                "INSERT OR IGNORE INTO title_providers (title_id, provider_name) VALUES (?,?)",
                (title_id, provider)
            )

        conn.commit()
        return title_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _build_title_filters(provider=None, title_type=None, search=None, year=None, min_rating=None):
    filters = []
    params = []

    if provider:
        filters.append("tp.provider_name = ?")
        params.append(provider)
    if title_type:
        filters.append("t.type = ?")
        params.append(title_type)
    if search:
        filters.append("(t.title LIKE ? OR t.original_title LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if year:
        filters.append("substr(t.release_date,1,4) = ?")
        params.append(str(year))
    if min_rating is not None:
        filters.append("t.imdb_rating >= ?")
        params.append(min_rating)

    where_sql = " WHERE " + " AND ".join(filters) if filters else ""
    return where_sql, params


def _fetch_provider_map(cursor, title_ids):
    if not title_ids:
        return {}

    placeholders = ",".join("?" for _ in title_ids)
    cursor.execute(
        f"""
        SELECT title_id, provider_name
        FROM title_providers
        WHERE title_id IN ({placeholders})
        ORDER BY provider_name
        """,
        title_ids,
    )

    provider_map = {title_id: [] for title_id in title_ids}
    for row in cursor.fetchall():
        provider_map[row["title_id"]].append(row["provider_name"])
    return provider_map


def get_titles(page=1, limit=20, provider=None, sort_by="rating", order="desc",
               title_type=None, search=None, year=None, min_rating=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    offset = (page - 1) * limit

    sort_map = {
        "added_date": "t.added_date",
        "rating": "t.imdb_rating",
        "release_date": "t.release_date",
    }
    sort_col = sort_map.get(sort_by, "t.added_date")
    direction = "DESC" if order == "desc" else "ASC"

    from_sql = """
        FROM titles t
        LEFT JOIN title_providers tp ON t.id = tp.title_id
    """
    where_sql, params = _build_title_filters(
        provider=provider,
        title_type=title_type,
        search=search,
        year=year,
        min_rating=min_rating,
    )

    query = f"""
        SELECT DISTINCT t.*
        {from_sql}
        {where_sql}
        ORDER BY {sort_col} {direction} NULLS LAST
        LIMIT ? OFFSET ?
    """

    cursor.execute(query, [*params, limit, offset])
    titles = [dict(row) for row in cursor.fetchall()]

    provider_map = _fetch_provider_map(cursor, [title["id"] for title in titles])
    for title in titles:
        title["providers"] = provider_map.get(title["id"], [])

    count_query = f"SELECT COUNT(DISTINCT t.id) {from_sql} {where_sql}"
    cursor.execute(count_query, params)
    total = cursor.fetchone()[0]
    conn.close()

    return {"titles": titles, "total": total, "page": page, "limit": limit}


def get_title_detail(title_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM titles WHERE id = ?", (title_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    title = dict(row)
    cursor.execute("SELECT provider_name FROM title_providers WHERE title_id = ?", (title_id,))
    title['providers'] = [r['provider_name'] for r in cursor.fetchall()]
    conn.close()
    return title


def get_providers():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT provider_name, COUNT(*) as count
        FROM title_providers GROUP BY provider_name ORDER BY count DESC
    """)
    providers = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return providers


def get_stats():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as total FROM titles")
    total = cursor.fetchone()["total"]

    cursor.execute("SELECT type, COUNT(*) as count FROM titles GROUP BY type")
    by_type = {row["type"]: row["count"] for row in cursor.fetchall()}

    cursor.execute("SELECT AVG(imdb_rating) as avg_rating FROM titles WHERE imdb_rating IS NOT NULL")
    avg = cursor.fetchone()["avg_rating"]

    cursor.execute("SELECT MAX(added_date) as last_update FROM titles")
    last_update = cursor.fetchone()["last_update"]

    cursor.execute("""
        SELECT DISTINCT substr(release_date,1,4) as y
        FROM titles
        WHERE release_date != ''
        ORDER BY y DESC
    """)
    years = [row["y"] for row in cursor.fetchall() if row["y"]]

    conn.close()
    return {
        "total": total,
        "by_type": by_type,
        "avg_rating": round(avg, 1) if avg else 0,
        "last_update": last_update,
        "years": years,
    }
