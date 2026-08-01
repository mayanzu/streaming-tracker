"""Schema 初始化与迁移：建表、补列、索引、历史数据整理。"""

import sqlite3
from datetime import datetime, timedelta, timezone

from app.config import PROVIDERS
from app.db.connection import get_db_connection
from app.db.utils import _utc_now


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
            first_seen_at TEXT,
            last_seen_at TEXT,
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
            "first_seen_at": "TEXT",
            "last_seen_at": "TEXT",
            "countries_synced_at": "TEXT",
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
    _ensure_title_identity_schema(conn, cursor)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS title_provider_availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER NOT NULL,
            provider_name TEXT NOT NULL,
            region TEXT NOT NULL DEFAULT '',
            monetization_type TEXT NOT NULL DEFAULT 'mixed',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (title_id) REFERENCES titles(id) ON DELETE CASCADE,
            UNIQUE(title_id, provider_name, region, monetization_type)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS title_countries (
            title_id INTEGER NOT NULL,
            country_code TEXT NOT NULL CHECK(length(country_code) = 2),
            FOREIGN KEY (title_id) REFERENCES titles(id) ON DELETE CASCADE,
            PRIMARY KEY (title_id, country_code)
        )
    """)
    now = _utc_now()
    cursor.execute("""
        INSERT OR IGNORE INTO title_provider_availability
            (title_id, provider_name, region, monetization_type, first_seen_at, last_seen_at, is_active)
        SELECT tp.title_id, tp.provider_name, '', 'mixed', ?, ?, 1
        FROM title_providers tp
        JOIN titles t ON t.id = tp.title_id
    """, (now, now))
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('movie', 'tv')),
            title TEXT,
            imdb_id TEXT,
            reason TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT NOT NULL,
            last_error TEXT,
            data_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(tmdb_id, type)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute(
        "SELECT value FROM sync_state WHERE key='provider_categories_v1'"
    )
    if not cursor.fetchone():
        _collapse_provider_categories(cursor)
        cursor.execute("""
            INSERT INTO sync_state(key, value, updated_at)
            VALUES ('provider_categories_v1', 'complete', ?)
        """, (now,))

    # 用户片单与抓取数据分离：即使作品表在评分重建时被清空，个人状态也不会丢失。
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS title_preferences (
            tmdb_id INTEGER NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('movie', 'tv')),
            watch_status TEXT NOT NULL CHECK(watch_status IN ('watchlist', 'watching', 'watched')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tmdb_id, type)
        )
    """)

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
            heartbeat_at TEXT,
            pending INTEGER DEFAULT 0,
            request_failed INTEGER DEFAULT 0,
            inserted INTEGER DEFAULT 0,
            updated INTEGER DEFAULT 0,
            unchanged INTEGER DEFAULT 0,
            provider_expired INTEGER DEFAULT 0,
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
            "heartbeat_at": "TEXT",
            "pending": "INTEGER DEFAULT 0",
            "request_failed": "INTEGER DEFAULT 0",
            "inserted": "INTEGER DEFAULT 0",
            "updated": "INTEGER DEFAULT 0",
            "unchanged": "INTEGER DEFAULT 0",
            "provider_expired": "INTEGER DEFAULT 0",
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_preferences_status ON title_preferences(watch_status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_runs_started ON sync_runs(started_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_retry ON pending_titles(next_retry_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_availability_active_provider ON title_provider_availability(is_active, provider_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_availability_title_active_provider ON title_provider_availability(title_id, is_active, provider_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_availability_last_seen ON title_provider_availability(last_seen_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_title_countries_code_title ON title_countries(country_code, title_id)")
    stale_before = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    cursor.execute("""
        UPDATE sync_runs
        SET status='abandoned', finished_at=?, error=COALESCE(error, 'Stale running sync recovered at startup')
        WHERE status='running' AND started_at < ?
    """, (now, stale_before))
    _drop_columns(cursor, "titles", ("tmdb_vote_average", "tmdb_vote_count"))

    conn.commit()
    conn.close()


def _ensure_title_identity_schema(conn, cursor):
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

    # SQLite PRAGMA foreign_keys 必须在事务外切换；先 commit 已积累的隐式事务，
    # 再开显式 IMMEDIATE 事务确保整个迁移原子化
    conn.commit()
    cursor.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
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
                first_seen_at TEXT,
                last_seen_at TEXT,
                last_synced_at TEXT,
                countries_synced_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tmdb_id, type)
            )
        """)
        cursor.execute("""
            INSERT INTO titles (
                id, tmdb_id, imdb_id, title, original_title, type, overview, release_date,
                poster_url, imdb_rating, rating_source, rating_votes, added_date,
                first_seen_at, last_seen_at, last_synced_at, countries_synced_at, created_at
            )
            SELECT
                id, tmdb_id, imdb_id, title, original_title, type, overview, release_date,
                poster_url, imdb_rating, rating_source, rating_votes, added_date,
                first_seen_at, last_seen_at, last_synced_at, countries_synced_at, created_at
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
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
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


def _merge_provider_group(cursor, target, where_sql, params):
    """Merge matching provider rows into one key without losing region history."""
    cursor.execute("DROP TABLE IF EXISTS temp.provider_availability_merge")
    cursor.execute(f"""
        CREATE TEMP TABLE provider_availability_merge AS
        SELECT title_id, region, monetization_type,
               MIN(first_seen_at) AS first_seen_at,
               MAX(last_seen_at) AS last_seen_at,
               MAX(is_active) AS is_active
        FROM title_provider_availability
        WHERE {where_sql}
        GROUP BY title_id, region, monetization_type
    """, params)
    cursor.execute(f"DELETE FROM title_provider_availability WHERE {where_sql}", params)
    cursor.execute("""
        INSERT INTO title_provider_availability
            (title_id, provider_name, region, monetization_type,
             first_seen_at, last_seen_at, is_active)
        SELECT title_id, ?, region, monetization_type,
               first_seen_at, last_seen_at, is_active
        FROM provider_availability_merge
    """, (target,))
    cursor.execute("DROP TABLE provider_availability_merge")

    cursor.execute("DROP TABLE IF EXISTS temp.provider_title_merge")
    cursor.execute(f"""
        CREATE TEMP TABLE provider_title_merge AS
        SELECT DISTINCT title_id FROM title_providers WHERE {where_sql}
    """, params)
    cursor.execute(f"DELETE FROM title_providers WHERE {where_sql}", params)
    cursor.execute("""
        INSERT OR IGNORE INTO title_providers(title_id, provider_name)
        SELECT title_id, ? FROM provider_title_merge
    """, (target,))
    cursor.execute("DROP TABLE provider_title_merge")


def _collapse_provider_categories(cursor):
    alias_groups = {
        "netflix": ("netflix",),
        "disney": ("disney", "disney plus", "disney+"),
        "max": ("max", "hbo max"),
        "amazon": ("amazon", "amazon prime video", "prime video"),
        "apple": ("apple", "apple tv plus", "apple tv+"),
        "hulu": ("hulu",),
    }
    for target, aliases in alias_groups.items():
        placeholders = ",".join("?" for _ in aliases)
        _merge_provider_group(
            cursor,
            target,
            f"LOWER(provider_name) IN ({placeholders})",
            aliases,
        )

    primary_names = tuple(PROVIDERS)
    placeholders = ",".join("?" for _ in primary_names)
    _merge_provider_group(
        cursor,
        "others",
        f"provider_name IS NULL OR LOWER(provider_name) NOT IN ({placeholders})",
        primary_names,
    )
