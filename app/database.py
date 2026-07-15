import os
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import logging

from app.config import DATABASE_URL, MIN_IMDB_RATING, PENDING_RETRY_DAYS, PROVIDER_STALE_DAYS

logger = logging.getLogger(__name__)

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
    conn.execute("PRAGMA busy_timeout = 5000")
    # 弱 ARM + 慢 USB 优化：扩 cache、开 mmap、临时表入内存
    conn.execute("PRAGMA cache_size = -4000")    # 4MB page cache（默认 2MB）
    conn.execute("PRAGMA mmap_size = 10485760")  # 10MB mmap，减少 read() 系统调用
    conn.execute("PRAGMA temp_store = MEMORY")
    # WAL 模式下 synchronous=NORMAL 是 SQLite 官方推荐（不丢已 commit 数据，
    # 仅 OS 崩溃时可能丢最后一个 WAL 段；exFAT 上 FULL 反而引入大量 fsync 浪费 I/O）
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def get_db():
    """上下文管理器：自动 commit/rollback/close，推荐用于简单读写场景。"""
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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


def _normalize_country_codes(values):
    codes = []
    for value in values or []:
        code = value.get("iso_3166_1") if isinstance(value, dict) else value
        code = str(code or "").strip().upper()
        if len(code) == 2 and code.isalpha() and code not in codes:
            codes.append(code)
    return codes


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


def get_due_pending_titles(limit=500):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM pending_titles
            WHERE next_retry_at <= ?
            ORDER BY next_retry_at, id
            LIMIT ?
        """, (_utc_now(), limit))
        results = []
        for row in cursor.fetchall():
            stored = dict(row)
            try:
                payload = json.loads(stored["data_json"])
            except (TypeError, ValueError):
                payload = {}
            payload.update({
                "tmdb_id": stored["tmdb_id"],
                "type": stored["type"],
                "title": payload.get("title") or stored["title"] or "",
                "imdb_id": payload.get("imdb_id") or stored["imdb_id"],
                "pending_attempt_count": stored["attempt_count"],
            })
            results.append(payload)
        return results
    finally:
        conn.close()


def claim_catalog_window(total_days, window_days, recent_days):
    if total_days <= recent_days or window_days <= 0:
        return None
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT value FROM sync_state WHERE key='catalog_window_index'")
        row = cursor.fetchone()
        index = int(row["value"]) if row else 0
        available_days = total_days - recent_days
        window_count = max(1, math.ceil(available_days / window_days))
        index %= window_count
        range_end = date.today() - timedelta(days=recent_days + index * window_days + 1)
        oldest = date.today() - timedelta(days=total_days)
        range_start = max(oldest, range_end - timedelta(days=window_days - 1))
        next_index = (index + 1) % window_count
        cursor.execute("""
            INSERT INTO sync_state(key, value, updated_at)
            VALUES ('catalog_window_index', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (str(next_index), _utc_now()))
        conn.commit()
        return range_start, range_end
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _retry_delay_days(reason, attempt_count):
    if reason == "low_rating":
        return max(PENDING_RETRY_DAYS[-1] if PENDING_RETRY_DAYS else 30, 30)
    schedule = PENDING_RETRY_DAYS or (1, 3, 7, 14, 30)
    return schedule[min(max(attempt_count - 1, 0), len(schedule) - 1)]


def _write_pending(cursor, title_data, observed_at):
    cursor.execute(
        "SELECT attempt_count, first_seen_at FROM pending_titles WHERE tmdb_id=? AND type=?",
        (title_data["tmdb_id"], title_data["type"]),
    )
    existing = cursor.fetchone()
    attempt_count = (existing["attempt_count"] if existing else 0) + 1
    reason = title_data.get("pending_reason") or "missing_rating"
    next_retry = datetime.now(timezone.utc) + timedelta(
        days=_retry_delay_days(reason, attempt_count)
    )
    payload = dict(title_data)
    payload.pop("last_error", None)
    cursor.execute("""
        INSERT INTO pending_titles
            (tmdb_id, type, title, imdb_id, reason, attempt_count, next_retry_at,
             last_error, data_json, first_seen_at, last_seen_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tmdb_id, type) DO UPDATE SET
            title=excluded.title, imdb_id=COALESCE(excluded.imdb_id, pending_titles.imdb_id),
            reason=excluded.reason, attempt_count=excluded.attempt_count,
            next_retry_at=excluded.next_retry_at, last_error=excluded.last_error,
            data_json=excluded.data_json, last_seen_at=excluded.last_seen_at,
            updated_at=excluded.updated_at
    """, (
        title_data["tmdb_id"], title_data["type"], title_data.get("title"),
        title_data.get("imdb_id"), reason, attempt_count, next_retry.isoformat(),
        title_data.get("last_error"), json.dumps(payload, ensure_ascii=False),
        existing["first_seen_at"] if existing else observed_at,
        observed_at, observed_at, observed_at,
    ))


def persist_sync_batch(titles, pending_titles, provider_stale_days=PROVIDER_STALE_DAYS):
    """Open/use/commit/close SQLite in one worker thread and return structured outcomes."""
    conn = get_db_connection()
    outcomes = {
        "processed": 0, "skipped": 0, "inserted": 0,
        "updated": 0, "unchanged": 0, "provider_expired": 0,
        "errors": [],
    }
    observed_at = _utc_now()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        for item_index, title_data in enumerate(titles):
            savepoint = f"title_{item_index}"
            cursor.execute(f"SAVEPOINT {savepoint}")
            try:
                cursor.execute(
                    "SELECT * FROM titles WHERE tmdb_id=? AND type=?",
                    (title_data["tmdb_id"], title_data["type"]),
                )
                before = cursor.fetchone()
                comparable_fields = (
                    "imdb_id", "title", "original_title", "overview", "release_date",
                    "poster_url", "imdb_rating", "rating_source", "rating_votes",
                )
                changed = before is None or any(
                    title_data.get(field) not in (None, "")
                    and title_data.get(field) != before[field]
                    for field in comparable_fields
                )
                title_data = dict(title_data)
                title_data["last_seen_at"] = observed_at
                insert_title(title_data, conn=conn)
                cursor.execute(
                    "DELETE FROM pending_titles WHERE tmdb_id=? AND type=?",
                    (title_data["tmdb_id"], title_data["type"]),
                )
                outcomes["processed"] += 1
                if before is None:
                    outcomes["inserted"] += 1
                elif changed:
                    outcomes["updated"] += 1
                else:
                    outcomes["unchanged"] += 1
                cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception as exc:
                cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
                outcomes["skipped"] += 1
                outcomes["errors"].append(
                    f"{title_data.get('title', '?')}: {type(exc).__name__}: {exc}"
                )

        for item_index, title_data in enumerate(pending_titles):
            savepoint = f"pending_{item_index}"
            cursor.execute(f"SAVEPOINT {savepoint}")
            try:
                _write_pending(cursor, title_data, observed_at)
                cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception as exc:
                cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
                outcomes["skipped"] += 1
                outcomes["errors"].append(
                    f"pending {title_data.get('title', '?')}: {type(exc).__name__}: {exc}"
                )

        stale_before = (datetime.now(timezone.utc) - timedelta(days=provider_stale_days)).isoformat()
        cursor.execute("""
            UPDATE title_provider_availability
            SET is_active=0
            WHERE is_active=1 AND last_seen_at < ?
        """, (stale_before,))
        outcomes["provider_expired"] = max(cursor.rowcount, 0)
        conn.commit()
        return outcomes
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_sync_run(reason, days_back, max_pages, window_days):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO sync_runs
                (reason, status, days_back, max_pages, window_days, started_at, heartbeat_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (reason, "running", days_back, max_pages, window_days, _utc_now(), _utc_now()),
        )
        sync_run_id = cursor.lastrowid
        conn.commit()
        return sync_run_id
    finally:
        conn.close()


def finish_sync_run(sync_run_id, status, result):
    if not sync_run_id:
        return

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE sync_runs
            SET status=?, finished_at=?, discovered=?, qualified=?, processed=?,
                skipped=?, no_rating=?, low_rating=?, pending=?, request_failed=?,
                inserted=?, updated=?, unchanged=?, provider_expired=?, heartbeat_at=?, error=?
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
                result.get("pending", 0),
                result.get("request_failed", 0),
                result.get("inserted", 0),
                result.get("updated", 0),
                result.get("unchanged", 0),
                result.get("provider_expired", 0),
                _utc_now(),
                result.get("error"),
                sync_run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_sync_run_progress(sync_run_id, progress):
    if not sync_run_id:
        return

    stats = progress.get("stats") or {}
    processed = progress.get("processed")
    skipped = progress.get("skipped")
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if processed is not None and skipped is not None:
            cursor.execute(
                """
                UPDATE sync_runs
                SET discovered=?, qualified=?, processed=?, skipped=?,
                    no_rating=?, low_rating=?, pending=?, request_failed=?,
                    inserted=?, updated=?, unchanged=?, provider_expired=?,
                    current_provider=?, current_provider_index=?, provider_total=?, phase=?, heartbeat_at=?
                WHERE id=?
                """,
                (
                    stats.get("discovered", 0),
                    stats.get("qualified", 0),
                    processed,
                    skipped,
                    stats.get("no_rating", 0),
                    stats.get("low_rating", 0),
                    stats.get("pending", 0),
                    stats.get("request_failed", 0),
                    progress.get("inserted", 0),
                    progress.get("updated", 0),
                    progress.get("unchanged", 0),
                    progress.get("provider_expired", 0),
                    progress.get("provider"),
                    progress.get("provider_index", 0),
                    progress.get("provider_total", 0),
                    progress.get("phase"),
                    _utc_now(),
                    sync_run_id,
                ),
            )
            conn.commit()
        else:
            cursor.execute(
                """
                UPDATE sync_runs
                SET discovered=?, qualified=?, no_rating=?, low_rating=?, pending=?, request_failed=?,
                    current_provider=?, current_provider_index=?, provider_total=?, phase=?, heartbeat_at=?
                WHERE id=?
                """,
                (
                    stats.get("discovered", 0),
                    stats.get("qualified", 0),
                    stats.get("no_rating", 0),
                    stats.get("low_rating", 0),
                    stats.get("pending", 0),
                    stats.get("request_failed", 0),
                    progress.get("provider"),
                    progress.get("provider_index", 0),
                    progress.get("provider_total", 0),
                    progress.get("phase"),
                    _utc_now(),
                    sync_run_id,
                ),
            )
            conn.commit()
    finally:
        conn.close()


def record_sync_error(sync_run_id, scope, message):
    if not sync_run_id:
        return

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO sync_errors (sync_run_id, scope, message) VALUES (?, ?, ?)",
            (sync_run_id, scope, message),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_sync_run():
    conn = get_db_connection()
    try:
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
        return dict(row) if row else None
    finally:
        conn.close()


def mark_sync_run_abandoned(sync_run_id, error):
    if not sync_run_id:
        return

    conn = get_db_connection()
    try:
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
    finally:
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
        logger.exception("Failed to get latest finished sync run")
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
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM titles WHERE {TRUSTED_RATING_CONDITION}")
        total = cursor.fetchone()[0]
        return total
    finally:
        conn.close()


def count_untrusted_titles():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT COUNT(*)
            FROM titles
            WHERE {UNTRUSTED_RATING_CONDITION}
            """
        )
        total = cursor.fetchone()[0]
        return total
    finally:
        conn.close()


def purge_untrusted_titles():
    conn = get_db_connection()
    try:
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
        providers = [dict(row) for row in cursor.fetchall()]
        return providers
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
