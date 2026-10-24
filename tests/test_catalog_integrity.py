"""2026-10-24 review 回归：评分跌破门槛下架、翻译覆盖保护、低分重试归档。

运行：python -m pytest tests/test_catalog_integrity.py -q
要求：不依赖外部网络与真实数据库，使用临时 SQLite。
"""

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "DATABASE_URL",
    os.path.join(tempfile.mkdtemp(prefix="streaming-test-integrity-"), "test.db"),
)

from app.db.schema import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    from app.db import connection
    from app.db.queries import invalidate_stats_cache

    monkeypatch.setattr(connection, "DATABASE_URL", str(tmp_path / "test.db"))
    init_db()
    invalidate_stats_cache()
    yield
    invalidate_stats_cache()


def _connect():
    from app.db import connection

    conn = sqlite3.connect(connection.DATABASE_URL)
    conn.row_factory = sqlite3.Row
    return conn


def _add_availability(title_id, provider="netflix"):
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO title_provider_availability
                (title_id, provider_name, region, monetization_type,
                 first_seen_at, last_seen_at, is_active)
            VALUES (?, ?, 'US', 'flatrate', '2024-01-01', '2024-01-01', 1)
            """,
            (title_id, provider),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_title(tmdb_id=1, rating=8.0, title="中文片名", overview="中文简介"):
    from app.db.titles import insert_title

    payload = {
        "tmdb_id": tmdb_id,
        "type": "movie",
        "title": title,
        "overview": overview,
        "release_date": "2024-01-01",
        "imdb_rating": rating,
        "rating_source": "imdb",
        "rating_votes": 5000,
    }
    insert_title(payload)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM titles WHERE tmdb_id=? AND type='movie'", (tmdb_id,)
        ).fetchone()
        return row["id"]
    finally:
        conn.close()


def _visible_count():
    from app.db.queries import get_titles

    return get_titles(page=1, limit=50)["total"]


def test_low_rating_is_not_visible_even_if_row_stores_it():
    """可见性 SQL 必须自己校验评分底线，不能只依赖写入时过滤。"""
    title_id = _insert_title()
    _add_availability(title_id)
    assert _visible_count() == 1

    conn = _connect()
    try:
        conn.execute("UPDATE titles SET imdb_rating=6.8 WHERE id=?", (title_id,))
        conn.commit()
    finally:
        conn.close()

    from app.db.queries import invalidate_stats_cache

    invalidate_stats_cache()
    assert _visible_count() == 0


def test_no_rating_grace_window_still_visible():
    """跌破门槛要下架，但新片无评分（宽限期）仍应保留展示。"""
    from datetime import date, timedelta

    release = (date.today() - timedelta(days=5)).isoformat()
    title_id = _insert_title(tmdb_id=2, rating=8.0)
    _add_availability(title_id)
    conn = _connect()
    try:
        conn.execute(
            "UPDATE titles SET imdb_rating=NULL, rating_source=NULL, rating_votes=NULL,"
            " release_date=? WHERE id=?",
            (release, title_id),
        )
        conn.commit()
    finally:
        conn.close()

    assert _visible_count() == 1


def test_translation_failure_does_not_overwrite_existing_chinese():
    from app.db.queries import get_title_detail

    title_id = _insert_title(title="中文片名", overview="中文简介")
    # 模拟下一次同步翻译失败：enrich 退回英文原文
    _insert_title(tmdb_id=1, title="English Title", overview="English overview")

    detail = get_title_detail(title_id)
    assert detail["title"] == "中文片名"
    assert detail["overview"] == "中文简介"
    assert detail["has_zh"] == 1


def test_low_rating_pending_demotes_existing_row():
    from app.db.pending import persist_sync_batch

    title_id = _insert_title(rating=7.0)
    _add_availability(title_id)

    persist_sync_batch(
        [],
        [{
            "tmdb_id": 1,
            "type": "movie",
            "title": "中文片名",
            "imdb_rating": 6.6,
            "rating_source": "imdb",
            "rating_votes": 5000,
            "pending_reason": "low_rating",
        }],
    )

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT imdb_rating FROM titles WHERE id=?", (title_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["imdb_rating"] == 6.6
    assert _visible_count() == 0


def test_low_rating_archives_after_two_confirmations():
    from app.db.utils import _retry_delay_days

    assert _retry_delay_days("low_rating", 1) == 30
    assert _retry_delay_days("low_rating", 2) == 30
    assert _retry_delay_days("low_rating", 3) is None
