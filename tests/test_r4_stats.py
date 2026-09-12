"""R4-D1：数据状态聚合（zh_quality / provider_check / ready.data_health）回归。

运行：python -m pytest tests/test_r4_stats.py -q
要求：不依赖外部网络与真实数据库；每个测试使用独立临时库。
"""

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 避免 app.config 默认指向真实 data/tracker.db；各测试仍用独立临时库
os.environ.setdefault(
    "DATABASE_URL", os.path.join(tempfile.mkdtemp(prefix="streaming-test-stats-"), "bootstrap.db")
)


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    from app.db import connection, init_db
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


def _insert_title(tmdb_id, zh_quality=None, has_zh=1, release="2020-01-01"):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO titles
                (tmdb_id, imdb_id, title, original_title, type, overview,
                 release_date, poster_url, imdb_rating, rating_source,
                 rating_votes, added_date, first_seen_at, last_seen_at,
                 last_synced_at, has_zh, zh_quality, genres_json, runtime)
            VALUES (?, ?, ?, '', 'movie', '简介', ?, '', 8.0, 'imdb',
                    5000, '2020-01-01', '2020-01-01T00:00:00', '2020-01-01',
                    '2020-01-01T00:00:00', ?, ?, '["Drama"]', 100)
        """, (tmdb_id, f"tt{tmdb_id:07d}", f"统计验证{tmdb_id}", release, has_zh, zh_quality))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _add_availability(title_id, provider="netflix", active=1, label="Netflix"):
    conn = _connect()
    try:
        conn.execute("""
            INSERT INTO title_provider_availability
                (title_id, provider_name, region, monetization_type,
                 first_seen_at, last_seen_at, is_active, provider_label)
            VALUES (?, ?, 'US', 'mixed', '2020-01-01', '2020-01-01', ?, ?)
        """, (title_id, provider, active, label))
        conn.commit()
    finally:
        conn.close()


def _add_offer(title_id, provider_id=8, region="US"):
    conn = _connect()
    try:
        conn.execute("""
            INSERT INTO title_watch_offers
                (title_id, provider_id, provider_group, provider_name, region,
                 monetization, first_seen_at, last_seen_at, is_active)
            VALUES (?, ?, 'netflix', 'Netflix', ?, 'flatrate',
                    '2020-01-01', '2020-01-01', 1)
        """, (title_id, provider_id, region))
        conn.commit()
    finally:
        conn.close()


def _set_checked(title_id, offset_days):
    conn = _connect()
    try:
        conn.execute(
            "UPDATE titles SET providers_checked_at=datetime('now', ?) WHERE id=?",
            (f"-{offset_days} days", title_id),
        )
        conn.commit()
    finally:
        conn.close()


def _add_origin(title_id, country="JP"):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO title_countries (title_id, country_code) VALUES (?, ?)",
            (title_id, country),
        )
        conn.commit()
    finally:
        conn.close()


def _fixture_titles():
    """构造四种核验状态与中文质量分档，返回 id 字典。"""
    recent = _insert_title(996001, zh_quality="full")
    _add_availability(recent)
    _set_checked(recent, 5)
    _add_offer(recent)

    stale = _insert_title(996002, zh_quality="none")
    _add_availability(stale)
    _set_checked(stale, 60)

    never = _insert_title(996003, zh_quality=None)
    _add_availability(never)

    # 亚洲产地：无活跃渠道仍可见；核验成功但确认暂无渠道
    empty = _insert_title(996004, zh_quality="machine")
    _add_origin(empty)
    _set_checked(empty, 5)
    return {"recent": recent, "stale": stale, "never": never, "empty": empty}


def test_provider_check_aggregates():
    ids = _fixture_titles()
    from app.db.queries import get_stats

    stats = get_stats()
    assert stats["total"] == 4
    assert ids["recent"] <= stats["total"]
    assert stats["provider_check"] == {
        "verified_recent": 2,        # recent + empty（成功空结果同样是已核验）
        "verified_stale": 1,
        "never_checked": 1,
        "checked_empty": 1,          # empty：已核验但无活跃渠道
        "active_offer_titles": 1,
    }


def test_zh_quality_distribution_includes_unknown():
    _fixture_titles()
    from app.db.queries import get_stats

    stats = get_stats()
    assert stats["zh_quality"] == {"full": 1, "none": 1, "machine": 1, "unknown": 1}
    # 中文质量分档不得把 unknown 混入 full
    assert stats["zh_quality"].get("unknown") == 1


def test_ready_exposes_data_health():
    _fixture_titles()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import router

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/ready")
    payload = response.json()
    assert payload["data_health"] == {
        "zh_unknown": 1,
        "provider_never_checked": 1,
        "provider_verified_recent": 2,
        "active_offer_titles": 1,
    }
