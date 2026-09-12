"""R4-04 回归：渠道复查候选两级队列与核验写入语义。

运行：python -m pytest tests/test_r4_channels.py -q
要求：不依赖外部网络与真实数据库。

说明：为兼容与 tests/test_review_regressions.py 同进程收集（各模块都会设置
DATABASE_URL，而 app.config 只会导入一次），本模块不在 import 阶段导入 app，
改为每个测试用独立临时库并通过 monkeypatch 切换 app.db.connection.DATABASE_URL，
测试之间互不干扰。
"""

import asyncio
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# run() 在无 key 时会抛 RuntimeError；这里给占位值（fetch 全程被替换，不会联网）
os.environ.setdefault("TMDB_API_KEY", "test-key")
# 若 app.config 尚未导入，避免其默认指向真实 data/tracker.db（各测试仍会用独立临时库）
os.environ.setdefault(
    "DATABASE_URL", os.path.join(tempfile.mkdtemp(prefix="streaming-test-r4-"), "bootstrap.db")
)


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """每个测试使用独立临时库：候选查询是全库范围，避免测试间互相干扰。"""
    from app.db import connection
    from app.db import init_db

    monkeypatch.setattr(connection, "DATABASE_URL", str(tmp_path / "test.db"))
    init_db()
    yield


def _connect():
    from app.db import connection

    conn = sqlite3.connect(connection.DATABASE_URL)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_title(tmdb_id, title, release="2020-01-01", media_type="movie",
                  rating=8.0, votes=3000, genres='["Drama"]'):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO titles
                (tmdb_id, imdb_id, title, original_title, type, overview,
                 release_date, poster_url, imdb_rating, rating_source,
                 rating_votes, added_date, first_seen_at, last_seen_at,
                 last_synced_at, has_zh, genres_json, runtime)
            VALUES (?, ?, ?, '', ?, '简介', ?, '', ?, 'imdb', ?, '2020-01-01',
                    '2020-01-01T00:00:00', '2020-01-01', '2020-01-01T00:00:00',
                    1, ?, 100)
        """, (tmdb_id, f"tt{tmdb_id:07d}", title, media_type, release, rating, votes, genres))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _add_availability(title_id, provider="netflix", label="Netflix", region="US", active=1):
    conn = _connect()
    try:
        conn.execute("""
            INSERT INTO title_provider_availability
                (title_id, provider_name, region, monetization_type,
                 first_seen_at, last_seen_at, is_active, provider_label)
            VALUES (?, ?, ?, 'mixed', '2020-01-01', '2020-01-01', ?, ?)
        """, (title_id, provider, region, active, label))
        conn.commit()
    finally:
        conn.close()


def _add_offer(title_id, provider_id=8, provider_name="Netflix", region="US",
               monetization="flatrate", active=1):
    conn = _connect()
    try:
        conn.execute("""
            INSERT INTO title_watch_offers
                (title_id, provider_id, provider_group, provider_name, region,
                 monetization, first_seen_at, last_seen_at, is_active)
            VALUES (?, ?, 'netflix', ?, ?, ?, '2020-01-01', '2020-01-01', ?)
        """, (title_id, provider_id, provider_name, region, monetization, active))
        conn.commit()
    finally:
        conn.close()


def _set_checked_raw(title_id, value):
    conn = _connect()
    try:
        conn.execute(
            "UPDATE titles SET providers_checked_at=? WHERE id=?", (value, title_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_checked_offset(title_id, offset):
    """用 SQLite 时间偏移（如 '-29 days'）动态写入，避免写死日期导致测试过期。"""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE titles SET providers_checked_at=datetime('now', ?) WHERE id=?",
            (offset, title_id),
        )
        conn.commit()
    finally:
        conn.close()


def _title_state(title_id):
    conn = _connect()
    try:
        title = conn.execute(
            "SELECT providers_checked_at FROM titles WHERE id=?", (title_id,)
        ).fetchone()
        avail = conn.execute("""
            SELECT COUNT(*) AS total, COALESCE(SUM(is_active), 0) AS active
            FROM title_provider_availability WHERE title_id=?
        """, (title_id,)).fetchone()
        offers = conn.execute("""
            SELECT COUNT(*) AS total, COALESCE(SUM(is_active), 0) AS active
            FROM title_watch_offers WHERE title_id=?
        """, (title_id,)).fetchone()
        return {
            "checked_at": title["providers_checked_at"],
            "avail_total": avail["total"],
            "avail_active": avail["active"],
            "offer_total": offers["total"],
            "offer_active": offers["active"],
        }
    finally:
        conn.close()


def _candidate_ids(**kwargs):
    from app.backfill_channels import _load_candidates

    return [row["id"] for row in _load_candidates(**kwargs)]


def test_expired_complete_title_enters_candidates():
    """R4-04：2020 年已核验、Netflix 标签完整、有活跃 availability/offer 的老片必须入选。"""
    tid = _insert_title(995001, "完整老片复查")
    _add_availability(tid, "netflix", label="Netflix")
    _add_offer(tid, 8, "Netflix")
    _set_checked_raw(tid, "2020-01-01T00:00:00")

    assert tid in _candidate_ids()


def test_recheck_window_boundaries():
    """R4-04：29 天前核验不进候选，31 天前进入（资料完整与否只看资格时间）。"""
    tid_fresh = _insert_title(995002, "窗口内不复查")
    tid_expired = _insert_title(995003, "窗口外复查")
    for tid in (tid_fresh, tid_expired):
        _add_availability(tid, "netflix", label="Netflix")
        _add_offer(tid, 8, "Netflix")
    _set_checked_offset(tid_fresh, "-29 days")
    _set_checked_offset(tid_expired, "-31 days")

    ids = _candidate_ids()
    assert tid_fresh not in ids
    assert tid_expired in ids


def test_starvation_budget_keeps_expired_complete():
    """R4-04：未核验积压再多，limit 也为过期完整记录保留复查配额。"""
    complete = _insert_title(995010, "过期完整配额")
    _add_availability(complete, "netflix", label="Netflix")
    _add_offer(complete, 8, "Netflix")
    _set_checked_offset(complete, "-60 days")

    never_checked = [
        _insert_title(995011 + index, f"从未核验{index}") for index in range(5)
    ]

    ids = _candidate_ids(limit=2)
    assert len(ids) == 2
    assert complete in ids
    assert any(tid in ids for tid in never_checked)

    # 复查组不足时由优先组补足；优先组不足时也由复查组补足
    ids = _candidate_ids(limit=4)
    assert len(ids) == 4 and complete in ids

    # 优先组为空时，名额全部归复查组
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM titles WHERE id IN (%s)" % ",".join("?" * len(never_checked)),
            never_checked,
        )
        conn.commit()
    finally:
        conn.close()
    assert _candidate_ids(limit=1) == [complete]


def test_newest_first_order_preserved():
    """R4-04：组内顺序确定，newest_first 仍按上映日期倒序。"""
    older = _insert_title(995040, "旧片", release="2001-01-01")
    newer = _insert_title(995041, "新片", release="2024-01-01")

    by_id = _candidate_ids()
    assert by_id.index(older) < by_id.index(newer)

    by_date = _candidate_ids(newest_first=True)
    assert by_date.index(newer) < by_date.index(older)


def test_run_empty_result_deactivates_and_records(monkeypatch):
    """成功空结果：停用活跃渠道与 offer、更新时间；30 天内不再进入候选。"""
    from app import backfill_channels as backfill

    tid = _insert_title(995020, "成功空结果")
    _add_availability(tid, "netflix", label="Netflix")
    _add_offer(tid, 8, "Netflix")
    _set_checked_raw(tid, "2020-01-01T00:00:00")

    monkeypatch.setattr(backfill, "TMDB_API_KEY", "test-key")

    async def fake_fetch(endpoint, params=None, retries=3, client=None):
        return {}

    monkeypatch.setattr(backfill, "fetch_tmdb", fake_fetch)
    real_load_candidates = backfill._load_candidates

    stats = asyncio.run(backfill.run(limit=1, concurrency=1))
    assert stats == {"total": 1, "processed": 1, "rows": 0, "empty": 1, "failed": 0}

    state = _title_state(tid)
    assert state["avail_total"] == 1 and state["avail_active"] == 0
    assert state["offer_total"] == 1 and state["offer_active"] == 0
    assert state["checked_at"] not in (None, "2020-01-01T00:00:00")
    assert tid not in {row["id"] for row in real_load_candidates()}


def test_run_failure_keeps_existing_data(monkeypatch):
    """请求失败：不写库、不更新核验时间，失败计入 failed。"""
    from app import backfill_channels as backfill

    tid = _insert_title(995021, "请求失败保留")
    _add_availability(tid, "netflix", label="Netflix")
    _add_offer(tid, 8, "Netflix")
    _set_checked_raw(tid, "2020-01-01T00:00:00")

    monkeypatch.setattr(backfill, "TMDB_API_KEY", "test-key")

    async def fake_fetch(endpoint, params=None, retries=3, client=None):
        raise RuntimeError("simulated tmdb failure")

    monkeypatch.setattr(backfill, "fetch_tmdb", fake_fetch)

    stats = asyncio.run(backfill.run(limit=1, concurrency=1))
    assert stats == {"total": 1, "processed": 1, "rows": 0, "empty": 0, "failed": 1}

    state = _title_state(tid)
    assert state["avail_total"] == 1 and state["avail_active"] == 1
    assert state["offer_total"] == 1 and state["offer_active"] == 1
    assert state["checked_at"] == "2020-01-01T00:00:00"


def test_run_payload_writes_channels_and_is_idempotent(monkeypatch):
    """完整 payload：渠道与 offer 落库，同一输入重复写入不产生重复行。"""
    from app import backfill_channels as backfill
    from app.fetcher.common import _provider_availability, _provider_offers

    tid = _insert_title(995030, "核验写入幂等")
    payload = {
        "results": {
            "US": {
                "flatrate": [{"provider_id": 8, "provider_name": "Netflix"}],
                "rent": [{"provider_id": 999001, "provider_name": "MUBI"}],
            },
        },
    }

    monkeypatch.setattr(backfill, "TMDB_API_KEY", "test-key")

    async def fake_fetch(endpoint, params=None, retries=3, client=None):
        return payload

    monkeypatch.setattr(backfill, "fetch_tmdb", fake_fetch)

    stats = asyncio.run(backfill.run(limit=1, concurrency=1))
    assert stats["total"] == 1 and stats["processed"] == 1
    assert stats["empty"] == 0 and stats["failed"] == 0 and stats["rows"] >= 1

    state = _title_state(tid)
    assert state["avail_total"] == 1 and state["avail_active"] == 1
    assert state["offer_total"] == 2 and state["offer_active"] == 2
    assert state["checked_at"] not in (None, "2020-01-01T00:00:00")

    conn = _connect()
    try:
        label = conn.execute(
            "SELECT provider_label FROM title_provider_availability WHERE title_id=?",
            (tid,),
        ).fetchone()["provider_label"]
        offers = {
            (row["provider_id"], row["region"], row["monetization"])
            for row in conn.execute(
                "SELECT provider_id, region, monetization FROM title_watch_offers WHERE title_id=?",
                (tid,),
            )
        }
    finally:
        conn.close()
    assert label == "Netflix"
    assert offers == {(8, "US", "flatrate"), (999001, "US", "rent")}

    # 同一解析结果再次写入：ON CONFLICT 幂等，行数不变
    providers, regions, labels = _provider_availability(payload)
    offers = _provider_offers(payload)
    backfill._save_channels(
        tid, providers, regions, labels, "2026-01-02T00:00:00", offers,
    )
    state_again = _title_state(tid)
    assert state_again["avail_total"] == 1 and state_again["avail_active"] == 1
    assert state_again["offer_total"] == 2 and state_again["offer_active"] == 2
