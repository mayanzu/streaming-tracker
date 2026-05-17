import sqlite3
import os
from datetime import datetime, timezone
from app.config import DATABASE_URL, MIN_IMDB_RATING

TRUSTED_RATING_SOURCES = ("imdb", "omdb")
TRUSTED_RATING_CONDITION = "imdb_rating IS NOT NULL AND rating_source IN ('imdb', 'omdb')"
TRUSTED_RATING_CONDITION_T = (
    "t.imdb_rating IS NOT NULL AND t.rating_source IN ('imdb', 'omdb')"
)
UNTRUSTED_RATING_CONDITION = (
    "imdb_rating IS NULL OR rating_source IS NULL OR rating_source NOT IN ('imdb', 'omdb')"
)


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

    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER NOT NULL,
            imdb_id TEXT,
            title TEXT NOT NULL,
            original_title TEXT,
            type TEXT CHECK(type IN ('movie', 'tv')),
            overview TEXT,
            release_date TEXT,
            poster_url TEXT,
            imdb_rating REAL,
            rating_source TEXT,
            rating_votes INTEGER,
            added_date TEXT,
            last_synced_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tmdb_id, type)
        )
    """)

    _ensure_columns(
        cursor,
        "titles",
        {
            "rating_source": "TEXT",
            "rating_votes": "INTEGER",
            "last_synced_at": "TEXT",
            "imdb_id": "TEXT",
        },
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS title_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER,
            provider_name TEXT,
            FOREIGN KEY (title_id) REFERENCES titles(id) ON DELETE CASCADE,
            UNIQUE(title_id, provider_name)
        )
    """)
    _ensure_title_identity_schema(cursor)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reason TEXT,
            status TEXT,
            days_back INTEGER,
            max_pages INTEGER,
            window_days INTEGER,
            started_at TEXT,
            finished_at TEXT,
            discovered INTEGER DEFAULT 0,
            qualified INTEGER DEFAULT 0,
            processed INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            no_rating INTEGER DEFAULT 0,
            low_rating INTEGER DEFAULT 0,
            current_provider TEXT,
            current_provider_index INTEGER DEFAULT 0,
            provider_total INTEGER DEFAULT 0,
            phase TEXT,
            error TEXT
        )
    """)
    _ensure_columns(
        cursor,
        "sync_runs",
        {
            "current_provider": "TEXT",
            "current_provider_index": "INTEGER DEFAULT 0",
            "provider_total": "INTEGER DEFAULT 0",
            "phase": "TEXT",
        },
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_run_id INTEGER,
            scope TEXT,
            message TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (sync_run_id) REFERENCES sync_runs(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_id_type ON titles(tmdb_id, type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_imdb_rating ON titles(imdb_rating)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_type ON titles(type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_added_date ON titles(added_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_release_date ON titles(release_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_runs_started ON sync_runs(started_at)")
    _drop_columns(cursor, "titles", ("tmdb_vote_average", "tmdb_vote_count"))

    conn.commit()
    conn.close()


def _ensure_title_identity_schema(cursor):
    cursor.execute("PRAGMA index_list(titles)")
    indexes = cursor.fetchall()
    has_legacy_unique = False
    for index in indexes:
        if not index["unique"]:
            continue
        cursor.execute(f"PRAGMA index_info({index['name']})")
        columns = [row["name"] for row in cursor.fetchall()]
        if columns == ["tmdb_id"]:
            has_legacy_unique = True
            break

    if not has_legacy_unique:
        return

    cursor.execute("PRAGMA foreign_keys = OFF")
    cursor.execute("ALTER TABLE title_providers RENAME TO title_providers_old")
    cursor.execute("ALTER TABLE titles RENAME TO titles_old")
    cursor.execute("""
        CREATE TABLE titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER NOT NULL,
            imdb_id TEXT,
            title TEXT NOT NULL,
            original_title TEXT,
            type TEXT CHECK(type IN ('movie', 'tv')),
            overview TEXT,
            release_date TEXT,
            poster_url TEXT,
            imdb_rating REAL,
            rating_source TEXT,
            rating_votes INTEGER,
            added_date TEXT,
            last_synced_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tmdb_id, type)
        )
    """)
    cursor.execute("""
        INSERT INTO titles (
            id, tmdb_id, imdb_id, title, original_title, type, overview, release_date,
            poster_url, imdb_rating, rating_source, rating_votes, added_date,
            last_synced_at, created_at
        )
        SELECT
            id, tmdb_id, imdb_id, title, original_title, type, overview, release_date,
            poster_url, imdb_rating, rating_source, rating_votes, added_date,
            last_synced_at, created_at
        FROM titles_old
    """)
    cursor.execute("""
        CREATE TABLE title_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER,
            provider_name TEXT,
            FOREIGN KEY (title_id) REFERENCES titles(id) ON DELETE CASCADE,
            UNIQUE(title_id, provider_name)
        )
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO title_providers (id, title_id, provider_name)
        SELECT id, title_id, provider_name
        FROM title_providers_old
        WHERE title_id IN (SELECT id FROM titles)
    """)
    cursor.execute("DROP TABLE title_providers_old")
    cursor.execute("DROP TABLE titles_old")
    cursor.execute("PRAGMA foreign_keys = ON")


def _ensure_columns(cursor, table, columns):
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in cursor.fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _drop_columns(cursor, table, columns):
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in cursor.fetchall()}
    for name in columns:
        if name in existing:
            try:
                cursor.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
            except sqlite3.OperationalError:
                cursor.execute(f"UPDATE {table} SET {name} = NULL")


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _normalize_rating_source(title_data):
    source = title_data.get("rating_source")
    if source not in TRUSTED_RATING_SOURCES:
        return None, None, None

    rating = title_data.get("imdb_rating")
    if rating is None:
        return None, None, None

    rating = float(rating)
    if rating < MIN_IMDB_RATING:
        return None, None, None

    return rating, source, title_data.get("rating_votes")


def update_title_imdb_id(title_id, imdb_id):
    if not title_id or not imdb_id:
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE titles SET imdb_id=?, last_synced_at=? WHERE id=?",
        (imdb_id, _utc_now(), title_id),
    )
    conn.commit()
    conn.close()


def insert_title(title_data):
    conn = get_db_connection()
    cursor = conn.cursor()
    rating, rating_source, rating_votes = _normalize_rating_source(title_data)
    if rating is None:
        raise ValueError("trusted IMDb rating is required")

    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT id FROM titles WHERE tmdb_id = ? AND type = ?",
            (title_data['tmdb_id'], title_data['type']),
        )
        existing = cursor.fetchone()

        if existing:
            cursor.execute("""
                UPDATE titles SET
                    imdb_id=?, title=?, original_title=?, type=?, overview=?,
                    release_date=?, poster_url=?, imdb_rating=?,
                    rating_source=?, rating_votes=?, added_date=?, last_synced_at=?
                WHERE tmdb_id=? AND type=?
            """, (
                title_data.get('imdb_id'),
                title_data['title'], title_data['original_title'],
                title_data['type'], title_data['overview'],
                title_data['release_date'], title_data['poster_url'],
                rating, rating_source,
                rating_votes, title_data['added_date'],
                title_data.get('last_synced_at') or _utc_now(),
                title_data['tmdb_id'], title_data['type']
            ))
            title_id = existing['id']
        else:
            cursor.execute("""
                INSERT INTO titles
                (tmdb_id, imdb_id, title, original_title, type, overview, release_date,
                 poster_url, imdb_rating, rating_source, rating_votes, added_date, last_synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                title_data['tmdb_id'], title_data.get('imdb_id'), title_data['title'],
                title_data['original_title'], title_data['type'],
                title_data['overview'], title_data['release_date'],
                title_data['poster_url'], rating,
                rating_source, rating_votes,
                title_data['added_date'], title_data.get('last_synced_at') or _utc_now()
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


def create_sync_run(reason, days_back, max_pages, window_days):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO sync_runs (reason, status, days_back, max_pages, window_days, started_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (reason, "running", days_back, max_pages, window_days, _utc_now()),
    )
    sync_run_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return sync_run_id


def finish_sync_run(sync_run_id, status, result):
    if not sync_run_id:
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE sync_runs
        SET status=?, finished_at=?, discovered=?, qualified=?, processed=?,
            skipped=?, no_rating=?, low_rating=?, error=?
        WHERE id=?
        """,
        (
            status,
            _utc_now(),
            result.get("discovered", 0),
            result.get("qualified", 0),
            result.get("processed", 0),
            result.get("skipped", 0),
            result.get("no_rating", 0),
            result.get("low_rating", 0),
            result.get("error"),
            sync_run_id,
        ),
    )
    conn.commit()
    conn.close()


def update_sync_run_progress(sync_run_id, progress):
    if not sync_run_id:
        return

    stats = progress.get("stats") or {}
    processed = progress.get("processed")
    skipped = progress.get("skipped")
    conn = get_db_connection()
    cursor = conn.cursor()
    if processed is not None and skipped is not None:
        cursor.execute(
            """
            UPDATE sync_runs
            SET discovered=?, qualified=?, processed=?, skipped=?,
                no_rating=?, low_rating=?, current_provider=?,
                current_provider_index=?, provider_total=?, phase=?
            WHERE id=?
            """,
            (
                stats.get("discovered", 0),
                stats.get("qualified", 0),
                processed,
                skipped,
                stats.get("no_rating", 0),
                stats.get("low_rating", 0),
                progress.get("provider"),
                progress.get("provider_index", 0),
                progress.get("provider_total", 0),
                progress.get("phase"),
                sync_run_id,
            ),
        )
        conn.commit()
        conn.close()
        return

    cursor.execute(
        """
        UPDATE sync_runs
        SET discovered=?, qualified=?, no_rating=?, low_rating=?,
            current_provider=?, current_provider_index=?, provider_total=?, phase=?
        WHERE id=?
        """,
        (
            stats.get("discovered", 0),
            stats.get("qualified", 0),
            stats.get("no_rating", 0),
            stats.get("low_rating", 0),
            progress.get("provider"),
            progress.get("provider_index", 0),
            progress.get("provider_total", 0),
            progress.get("phase"),
            sync_run_id,
        ),
    )
    conn.commit()
    conn.close()


def record_sync_error(sync_run_id, scope, message):
    if not sync_run_id:
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO sync_errors (sync_run_id, scope, message) VALUES (?, ?, ?)",
        (sync_run_id, scope, message),
    )
    conn.commit()
    conn.close()


def get_latest_sync_run():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT *
        FROM sync_runs
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def mark_sync_run_abandoned(sync_run_id, error):
    if not sync_run_id:
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE sync_runs
        SET status = ?, finished_at = ?, error = ?
        WHERE id = ? AND status = ?
        """,
        ("abandoned", _utc_now(), error, sync_run_id, "running"),
    )
    conn.commit()
    conn.close()


def get_latest_finished_sync_run():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT *
            FROM sync_runs
            WHERE finished_at IS NOT NULL
            ORDER BY finished_at DESC, id DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        if conn:
            conn.close()


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
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM titles WHERE {TRUSTED_RATING_CONDITION}")
    total = cursor.fetchone()[0]
    conn.close()
    return total


def count_untrusted_titles():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT COUNT(*)
        FROM titles
        WHERE {UNTRUSTED_RATING_CONDITION}
        """
    )
    total = cursor.fetchone()[0]
    conn.close()
    return total


def purge_untrusted_titles():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"""
        DELETE FROM titles
        WHERE id IN (
            SELECT id FROM titles WHERE {UNTRUSTED_RATING_CONDITION}
        )
        """
    )
    removed = cursor.rowcount
    cursor.execute("""
        DELETE FROM title_providers WHERE title_id NOT IN (SELECT id FROM titles)
    """)
    conn.commit()
    conn.close()
    return removed


def purge_all_titles():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM titles")
    total = cursor.fetchone()[0]
    cursor.execute("DELETE FROM titles")
    cursor.execute("DELETE FROM title_providers")
    conn.commit()
    conn.close()
    return total


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
    cursor.execute(
        f"SELECT * FROM titles t WHERE t.id = ? AND {TRUSTED_RATING_CONDITION_T}",
        (title_id,),
    )
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
    cursor.execute(f"""
        SELECT tp.provider_name, COUNT(*) as count
        FROM title_providers tp
        JOIN titles t ON t.id = tp.title_id
        WHERE {TRUSTED_RATING_CONDITION_T}
        GROUP BY tp.provider_name
        ORDER BY count DESC
    """)
    providers = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return providers


def get_stats():
    conn = get_db_connection()
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

    cursor.execute(f"""
        SELECT DISTINCT substr(t.release_date,1,4) as y
        FROM titles t
        WHERE t.release_date != '' AND {TRUSTED_RATING_CONDITION_T}
        ORDER BY y DESC
    """)
    years = [row["y"] for row in cursor.fetchall() if row["y"]]

    cursor.execute(
        """
        SELECT *
        FROM sync_runs
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """
    )
    latest_sync = cursor.fetchone()

    conn.close()
    return {
        "total": total,
        "by_type": by_type,
        "avg_rating": round(avg, 1) if avg else 0,
        "last_update": last_update,
        "last_synced_at": last_synced_at,
        "years": years,
        "latest_sync": dict(latest_sync) if latest_sync else None,
    }
