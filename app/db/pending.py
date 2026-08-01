"""待处理重试队列：领取/写入待处理条目、批量落库（同步批处理）。"""

import json
import math
from datetime import date, datetime, timedelta, timezone

from app.config import PROVIDER_STALE_DAYS
from app.db.connection import get_db_connection
from app.db.titles import insert_title
from app.db.utils import _retry_delay_days, _utc_now


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
