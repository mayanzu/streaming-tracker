import sqlite3
import os
from app.config import DATABASE_URL


def get_db_connection():
    os.makedirs(os.path.dirname(DATABASE_URL), exist_ok=True)
    conn = sqlite3.connect(DATABASE_URL)
    conn.row_factory = sqlite3.Row
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

        for provider in title_data.get('providers', []):
            cursor.execute(
                "INSERT OR IGNORE INTO title_providers (title_id, provider_name) VALUES (?,?)",
                (title_id, provider)
            )

        conn.commit()
        return title_id
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_titles(page=1, limit=20, provider=None, sort_by="rating", order="desc",
               title_type=None, search=None, year=None, min_rating=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    offset = (page - 1) * limit

    query = """
        SELECT DISTINCT t.* FROM titles t
        LEFT JOIN title_providers tp ON t.id = tp.title_id
        WHERE 1=1
    """
    params = []

    if provider:
        query += " AND tp.provider_name = ?"
        params.append(provider)
    if title_type:
        query += " AND t.type = ?"
        params.append(title_type)
    if search:
        query += " AND (t.title LIKE ? OR t.original_title LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if year:
        query += " AND substr(t.release_date,1,4) = ?"
        params.append(str(year))
    if min_rating:
        query += " AND t.imdb_rating >= ?"
        params.append(min_rating)

    sort_map = {
        "added_date": "t.added_date",
        "rating": "t.imdb_rating",
        "release_date": "t.release_date",
    }
    sort_col = sort_map.get(sort_by, "t.added_date")
    direction = "DESC" if order == "desc" else "ASC"
    query += f" ORDER BY {sort_col} {direction} NULLS LAST LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor.execute(query, params)
    titles = [dict(row) for row in cursor.fetchall()]

    for title in titles:
        cursor.execute("SELECT provider_name FROM title_providers WHERE title_id = ?", (title['id'],))
        title['providers'] = [r['provider_name'] for r in cursor.fetchall()]

    count_query = """
        SELECT COUNT(DISTINCT t.id) FROM titles t
        LEFT JOIN title_providers tp ON t.id = tp.title_id WHERE 1=1
    """
    count_params = []
    if provider:
        count_query += " AND tp.provider_name = ?"
        count_params.append(provider)
    if title_type:
        count_query += " AND t.type = ?"
        count_params.append(title_type)
    if search:
        count_query += " AND (t.title LIKE ? OR t.original_title LIKE ?)"
        count_params.extend([f"%{search}%", f"%{search}%"])
    if year:
        count_query += " AND substr(t.release_date,1,4) = ?"
        count_params.append(str(year))
    if min_rating:
        count_query += " AND t.imdb_rating >= ?"
        count_params.append(min_rating)

    cursor.execute(count_query, count_params)
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
