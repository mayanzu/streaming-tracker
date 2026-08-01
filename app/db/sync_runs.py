"""同步运行记录：创建/结束/进度/错误/最近记录查询。"""

import logging

from app.db.connection import get_db_connection
from app.db.utils import _utc_now

logger = logging.getLogger(__name__)


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
