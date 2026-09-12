"""Review 报告回归集：F05 / F06 / F07 / 导入恢复 / 题材时长 / 搜索排序 / 隐藏已看 / 近期新片 / 个人记录。

运行：python -m pytest tests/test_review_regressions.py -q
要求：不依赖外部网络与真实数据库，使用临时 SQLite。
"""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="streaming-test-")
os.environ["DATABASE_URL"] = os.path.join(_tmp, "test.db")

from app.db.queries import (  # noqa: E402
    export_watchlist,
    get_preference_detail,
    get_recent_releases,
    get_stats,
    get_title_detail,
    get_titles,
    import_watchlist,
    update_preference_by_identity,
    update_status_by_identity,
    update_title_preference,
    update_title_status,
    update_titles_batch,
)
from app.db.schema import init_db  # noqa: E402

init_db()


def _insert_title(tmdb_id, media_type="movie", title="测试", rating=8.0,
                  votes=1000, release="2024-01-01", has_zh=1,
                  genres='["Drama"]', runtime=100, imdb_id=None):
    from app.config import DATABASE_URL

    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO titles
                (tmdb_id, imdb_id, title, original_title, type, overview,
                 release_date, poster_url, imdb_rating, rating_source,
                 rating_votes, added_date, first_seen_at, last_seen_at,
                 last_synced_at, has_zh, genres_json, runtime)
            VALUES (?, ?, ?, '', ?, '简介', ?, '', ?, 'imdb', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tmdb_id, imdb_id or f"tt{tmdb_id:07d}", title, media_type,
              release, rating, votes,
              "2024-01-01", "2024-01-01T00:00:00", "2024-01-01",
              "2024-01-01T00:00:00", has_zh, genres, runtime))
        title_id = cur.lastrowid
        conn.commit()
        return title_id
    finally:
        conn.close()


def _add_availability(title_id, provider="others", region=""):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO title_provider_availability
                (title_id, provider_name, region, monetization_type,
                 first_seen_at, last_seen_at, is_active)
            VALUES (?, ?, ?, 'mixed', '2024-01-01', '2024-01-01', 1)
        """, (title_id, provider, region))
        conn.commit()
    finally:
        conn.close()


def test_watchlist_count_matches_list_for_other_platform_title():
    """F06：法国、仅其他平台的作品加入想看后，计数与列表一致。"""
    tid = _insert_title(900101, title="法国边缘作品", rating=8.2, votes=1200)
    _add_availability(tid, "others")
    update_title_status(tid, "watchlist")
    listed = get_titles(watch_status="watchlist")
    stats = get_stats()
    assert listed["total"] >= 1
    assert stats["by_status"].get("watchlist", 0) == listed["total"]
    update_title_status(tid, "")


def test_stats_cache_invalidated_after_status_write():
    """F05：PATCH 成功后紧接着读统计，能立即读到新值。"""
    tid = _insert_title(900102, title="统计失效验证", rating=8.5, votes=2000)
    before = get_stats()
    before_count = before["by_status"].get("watchlist", 0)
    update_title_status(tid, "watchlist")
    after = get_stats()
    assert after["by_status"].get("watchlist", 0) == before_count + 1
    update_title_status(tid, "")


def test_expired_favorite_accessible_and_removable():
    """F07：过宽限期仍无评分的收藏，可打开也可移除。"""
    tid = _insert_title(900103, title="过期待开分老片", rating=None,
                        votes=None, release="2020-01-01")
    update_title_status(tid, "watchlist")
    detail = get_title_detail(tid)
    assert detail is not None
    assert detail["watch_status"] == "watchlist"
    update_title_status(tid, "")
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        row = conn.execute(
            "SELECT * FROM title_preferences WHERE tmdb_id=? AND type=?",
            (900103, "movie")).fetchone()
    finally:
        conn.close()
    assert row is None


def test_watchlist_export_import_roundtrip():
    """备份恢复：导出 → 清空 → 导入，数量一致且可重复执行。"""
    tid = _insert_title(900104, title="备份恢复验证", rating=8.1, votes=1500)
    update_title_status(tid, "watchlist")
    items = export_watchlist()
    assert any(i["tmdb_id"] == 900104 for i in items)
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        conn.execute("DELETE FROM title_preferences WHERE tmdb_id=?", (900104,))
        conn.commit()
    finally:
        conn.close()
    payload = [i for i in items if i["tmdb_id"] == 900104]
    first = import_watchlist(payload)
    assert first["added"] == 1
    second = import_watchlist(payload)
    assert second["added"] == 0 and second["skipped"] == 0
    update_title_status(tid, "")


def test_genre_and_runtime_filters():
    tid_action = _insert_title(900105, title="动作短片", rating=8.0,
                               votes=5000, genres='["Action"]', runtime=80)
    tid_drama = _insert_title(900106, title="剧情长片", rating=8.0,
                              votes=5000, genres='["Drama"]', runtime=150)
    _add_availability(tid_action, "netflix")
    _add_availability(tid_drama, "netflix")
    by_genre = get_titles(genre="Action")
    ids = {t["id"] for t in by_genre["titles"]}
    assert tid_action in ids and tid_drama not in ids
    by_runtime = get_titles(max_runtime=90)
    ids = {t["id"] for t in by_runtime["titles"]}
    assert tid_action in ids and tid_drama not in ids


def test_search_exact_title_first():
    tid_exact = _insert_title(900107, title="星际穿越", rating=8.8, votes=5000,
                              genres='["Drama"]')
    tid_other = _insert_title(900108, title="无关作品", rating=8.0, votes=5000,
                              genres='["Drama"]')
    _add_availability(tid_exact, "netflix")
    _add_availability(tid_other, "netflix")
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        conn.execute("UPDATE titles SET overview='一部提到星际穿越的纪录片' WHERE tmdb_id=900108")
        conn.commit()
    finally:
        conn.close()
    result = get_titles(search="星际穿越")
    assert result["total"] >= 2
    assert result["titles"][0]["title"] == "星际穿越"


def test_provider_labels_shown_in_detail():
    """其他平台保留具体名称：insert_title 写入 label，详情可读出。"""
    from app.db.titles import insert_title

    now = "2024-01-01T00:00:00"
    title_id = insert_title({
        "tmdb_id": 900109, "type": "movie", "title": "标签验证片",
        "release_date": "2024-01-01", "imdb_rating": 8.3,
        "rating_source": "imdb", "rating_votes": 5000,
        "has_zh": 1, "providers": ["netflix", "others"],
        "provider_regions": {"netflix": ["US"], "others": ["US"]},
        "provider_labels": {"netflix": ["Netflix"], "others": ["MUBI", "Criterion Channel"]},
        "first_seen_at": now, "last_seen_at": now, "last_synced_at": now,
    })
    detail = get_title_detail(title_id)
    by_name = {d["provider"]: d for d in detail["provider_details"]}
    assert by_name["others"]["labels"] == ["MUBI", "Criterion Channel"]
    assert by_name["netflix"]["labels"] == ["Netflix"]


def test_channels_backfill_parser_keeps_display_names():
    """抓取解析保留原始平台名，供回填使用（纯函数，无网络）。"""
    from app.fetcher.common import _provider_availability

    payload = {"results": {"US": {"flatrate": [
        {"provider_id": 8, "provider_name": "Netflix"},
        {"provider_id": 999001, "provider_name": "MUBI"},
        {"provider_id": 999002, "provider_name": "Criterion Channel"},
    ]}}}
    providers, regions, labels = _provider_availability(payload)
    assert "netflix" in providers and "others" in providers
    assert regions["others"] == ["US"]
    assert labels["others"] == ["MUBI", "Criterion Channel"]
    assert labels["netflix"] == ["Netflix"]


def test_channels_backfill_save_and_candidate_progress():
    """回填写入 label 后 30 天内退出候选；超过复查期重新进入候选（R4-04）。"""
    from app.backfill_channels import _load_candidates, _save_channels
    from app.db.utils import _utc_now

    tid = _insert_title(900110, title="渠道回填验证", rating=8.4, votes=5000)
    _add_availability(tid, "others")
    ids_before = {c["id"] for c in _load_candidates()}
    assert tid in ids_before
    rows = _save_channels(tid, ["others"], {"others": ["US"]},
                          {"others": ["MUBI"]}, _utc_now())
    assert rows >= 1
    detail = get_title_detail(tid)
    by_name = {d["provider"]: d for d in detail["provider_details"]}
    assert by_name["others"]["labels"] == ["MUBI"]
    # 刚核验完（30 天复查期内）退出候选集，避免重复请求
    assert tid not in {c["id"] for c in _load_candidates()}
    # R4-04：核验时间过期后，资料完整的记录也必须重新进入候选复查，发现渠道下架
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        conn.execute(
            "UPDATE titles SET providers_checked_at='2020-01-01T00:00:00' WHERE id=?",
            (tid,),
        )
        conn.commit()
    finally:
        conn.close()
    assert tid in {c["id"] for c in _load_candidates()}


def test_channels_backfill_empty_marks_checked():
    """7.5：无渠道结果也记录检查时间并停用已有渠道，30 天内不再重复请求。"""
    from app.backfill_channels import _load_candidates, _save_channels

    tid = _insert_title(900111, title="无渠道验证", rating=8.4, votes=5000)
    _add_availability(tid, "others")
    assert tid in {c["id"] for c in _load_candidates()}
    from app.db.utils import _utc_now
    _save_channels(tid, [], {}, {}, _utc_now())
    assert tid not in {c["id"] for c in _load_candidates()}
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM title_provider_availability WHERE title_id=? AND is_active=1",
            (tid,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert active == 0


def test_sync_does_not_expire_unchecked_providers():
    """7.5：同步未覆盖的作品不得因时间流逝被停用渠道（旧时钟过期策略回归）。"""
    from app.db.pending import persist_sync_batch

    tid = _insert_title(900125, title="过期保护验证", rating=8.0, votes=3000)
    _add_availability(tid, "netflix")
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        conn.execute(
            "UPDATE title_provider_availability SET last_seen_at='2020-01-01T00:00:00' WHERE title_id=?",
            (tid,),
        )
        conn.commit()
    finally:
        conn.close()
    persist_sync_batch([], [])
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        active = conn.execute(
            "SELECT is_active FROM title_provider_availability WHERE title_id=?",
            (tid,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert active == 1


def test_backfill_deactivates_missing_providers():
    """7.5：单片核验成功后，本次未出现的渠道才标记失效。"""
    from app.backfill_channels import _save_channels

    tid = _insert_title(900126, title="渠道否定验证", rating=8.0, votes=3000)
    _add_availability(tid, "netflix", "US")
    _add_availability(tid, "others", "US")
    rows = _save_channels(tid, ["netflix"], {"netflix": ["US"]},
                          {"netflix": ["Netflix"]}, "2024-06-01T00:00:00")
    assert rows >= 1
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        statuses = dict(conn.execute(
            "SELECT provider_name, is_active FROM title_provider_availability WHERE title_id=?",
            (tid,),
        ).fetchall())
        checked = conn.execute(
            "SELECT providers_checked_at FROM titles WHERE id=?", (tid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert statuses["netflix"] == 1
    assert statuses["others"] == 0
    assert checked == "2024-06-01T00:00:00"


def test_exclude_watched_filter():
    """6.5：隐藏已看过滤 —— 已看作品排除，未看保留。"""
    tid_keep = _insert_title(900112, title="未看保留验证", rating=8.0, votes=3000)
    tid_seen = _insert_title(900113, title="已看排除验证", rating=8.0, votes=3000)
    _add_availability(tid_keep, "netflix")
    _add_availability(tid_seen, "netflix")
    update_title_status(tid_seen, "watched")
    try:
        kept = get_titles(search="未看保留验证", exclude_watched=True)
        excluded = get_titles(search="已看排除验证", exclude_watched=True)
        assert any(t["id"] == tid_keep for t in kept["titles"])
        assert excluded["total"] == 0
        # 加回“已看”视图时状态过滤优先于隐藏已看，记录必须可见
        in_watched = get_titles(watch_status="watched", exclude_watched=True)
        assert any(t["id"] == tid_seen for t in in_watched["titles"])
    finally:
        update_title_status(tid_seen, "")


def test_released_after_filter():
    """6.2：近期新片模式 —— 只保留指定日期之后上映的作品。"""
    tid_new = _insert_title(900114, title="近期新片验证", rating=7.0, votes=3000,
                            release="2099-01-01")
    tid_old = _insert_title(900115, title="老片排除验证", rating=7.0, votes=3000,
                            release="2001-01-01")
    _add_availability(tid_new, "netflix")
    _add_availability(tid_old, "netflix")
    recent = get_titles(search="近期新片验证", released_after="2098-01-01")
    assert any(t["id"] == tid_new for t in recent["titles"])
    excluded = get_titles(search="老片排除验证", released_after="2098-01-01")
    assert excluded["total"] == 0


def test_preference_fields_roundtrip_and_export():
    """6.7：优先级/备注/个人评分可更新，导出包含，导入可恢复。"""
    tid = _insert_title(900116, title="个人记录验证", rating=8.0, votes=3000)
    update_title_status(tid, "watchlist")
    detail = update_title_preference(tid, priority=2, note="周末陪家人看", personal_rating=9.5)
    assert detail["priority"] == 2
    assert detail["note"] == "周末陪家人看"
    assert detail["personal_rating"] == 9.5
    exported = {i["tmdb_id"]: i for i in export_watchlist()}
    row = exported[900116]
    assert row["priority"] == 2 and row["note"] == "周末陪家人看"
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        conn.execute("DELETE FROM title_preferences WHERE tmdb_id=900116")
        conn.commit()
    finally:
        conn.close()
    result = import_watchlist([row])
    assert result["added"] == 1
    detail = get_title_detail(tid)
    assert detail["priority"] == 2 and detail["note"] == "周末陪家人看"
    assert detail["personal_rating"] == 9.5
    # 清除个人评分
    detail = update_title_preference(tid, personal_rating=0)
    assert detail["personal_rating"] is None
    update_title_status(tid, "")


def test_watched_at_recorded_on_watched():
    """6.7：标记已看自动记录观看日期。"""
    tid = _insert_title(900117, title="观看日期验证", rating=8.0, votes=3000)
    update_title_status(tid, "watchlist")
    assert not get_title_detail(tid)["watched_at"]
    update_title_status(tid, "watched")
    assert get_title_detail(tid)["watched_at"]
    update_title_status(tid, "")


def test_library_sort_by_updated_at_and_priority():
    """6.7：我的片单支持按最近更新与优先级排序。"""
    tid_a = _insert_title(900118, title="片单排序A", rating=8.0, votes=3000)
    tid_b = _insert_title(900119, title="片单排序B", rating=8.0, votes=3000)
    update_title_status(tid_a, "watchlist")
    update_title_status(tid_b, "watchlist")
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        conn.execute("UPDATE title_preferences SET updated_at='2099-01-01T00:00:00' WHERE tmdb_id=900119")
        conn.execute("UPDATE title_preferences SET updated_at='2020-01-01T00:00:00' WHERE tmdb_id=900118")
        conn.commit()
    finally:
        conn.close()
    result = get_titles(watch_status="watchlist", sort_by="updated_at", order="desc")
    ids = [t["id"] for t in result["titles"]]
    assert ids.index(tid_b) < ids.index(tid_a)
    update_title_preference(tid_a, priority=2)
    result = get_titles(watch_status="watchlist", sort_by="priority", order="desc")
    assert result["titles"][0]["id"] == tid_a
    update_title_status(tid_a, "")
    update_title_status(tid_b, "")


def test_search_match_reason():
    """6.4：搜索返回匹配原因（片名/简介）。"""
    tid = _insert_title(900120, title="匹配原因验证", rating=8.0, votes=3000)
    _add_availability(tid, "netflix")
    by_title = get_titles(search="匹配原因验证")
    assert by_title["titles"][0]["match_reason"] == "title"
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        conn.execute("UPDATE titles SET overview='一段提到独角兽关键词的简介' WHERE tmdb_id=900120")
        conn.commit()
    finally:
        conn.close()
    by_overview = get_titles(search="独角兽关键词")
    assert by_overview["titles"][0]["match_reason"] == "overview"


def test_batch_update_titles():
    """6.7 批量操作：批量改状态 / 设优先级 / 移除。"""
    tid_a = _insert_title(900121, title="批量A", rating=8.0, votes=3000)
    tid_b = _insert_title(900122, title="批量B", rating=8.0, votes=3000)
    tid_c = _insert_title(900123, title="批量C", rating=8.0, votes=3000)
    result = update_titles_batch([tid_a, tid_b, tid_c], watch_status="watchlist")
    assert result["updated"] == 3
    assert get_title_detail(tid_a)["watch_status"] == "watchlist"
    # 批量设优先级只影响片单成员
    result = update_titles_batch([tid_a, tid_b], priority=2)
    assert result["updated"] == 2
    assert get_title_detail(tid_a)["priority"] == 2
    assert get_title_detail(tid_c)["priority"] == 0
    # 批量标记已看记录观看日期
    update_titles_batch([tid_b], watch_status="watched")
    assert get_title_detail(tid_b)["watched_at"]
    # 批量移除
    result = update_titles_batch([tid_a, tid_b, tid_c], watch_status="")
    assert result["updated"] == 3
    assert get_title_detail(tid_a)["watch_status"] == ""
    assert not get_title_detail(tid_a)["watched_at"]


def test_list_count_cache_invalidated_after_status_write():
    """性能优化回归：过滤总数缓存必须随片单写入失效，翻页不能拿到旧总数。"""
    tid = _insert_title(900124, title="计数缓存失效验证", rating=8.0, votes=3000)
    before = get_titles(watch_status="watchlist")["total"]
    update_title_status(tid, "watchlist")
    after = get_titles(watch_status="watchlist")["total"]
    assert after == before + 1
    update_title_status(tid, "")


def test_schema_migration_idempotent():
    """A：新表/迁移可重复执行，不破坏已有偏好与快照。"""
    init_db()
    init_db()
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "title_preference_snapshots" in tables
    assert "title_watch_offers" in tables


def test_missing_catalog_restore_listed_and_export_roundtrip():
    """R01：目录缺失也能恢复、显示、导出快照，且补全前不丢身份。"""
    payload = [{
        "tmdb_id": 990201, "type": "movie", "watch_status": "watchlist",
        "title": "快照标题", "poster_url": "https://image.tmdb.org/t/p/w500/x.jpg",
        "release_date": "2025-05-01", "imdb_id": "tt9900201",
        "priority": 1, "note": "备份备注",
    }]
    result = import_watchlist(payload, schema_version=2)
    assert result["added"] == 1
    listed = get_titles(watch_status="watchlist", search="快照标题")
    assert listed["total"] == 1
    row = listed["titles"][0]
    assert row["catalog_available"] is False
    assert row["id"] is None
    assert row["title"] == "快照标题"
    assert row["tmdb_id"] == 990201 and row["type"] == "movie"
    stats = get_stats()
    assert stats["by_status"].get("watchlist", 0) >= 1
    assert stats["library_pending"] >= 1
    exported = {item["tmdb_id"]: item for item in export_watchlist()}
    assert exported[990201]["title"] == "快照标题"
    assert exported[990201]["poster_url"].endswith("/x.jpg")
    assert exported[990201]["priority"] == 1
    # 完全相同重复导入：无变化
    again = import_watchlist(payload, schema_version=2)
    assert again["unchanged"] == 1 and again["added"] == 0
    # 快照丢失时，unchanged 的重复导入仍能补齐快照
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        conn.execute("DELETE FROM title_preference_snapshots WHERE tmdb_id=990201")
        conn.commit()
    finally:
        conn.close()
    restored = import_watchlist(payload, schema_version=2)
    assert restored["unchanged"] == 1
    assert {i["tmdb_id"]: i for i in export_watchlist()}[990201]["title"] == "快照标题"
    entry = get_preference_detail("movie", 990201)
    assert entry["title"] == "快照标题" and entry["catalog_available"] is False
    # 补全目录后仍是一条偏好，并改为显示目录标题
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        conn.execute("""
            INSERT INTO titles
                (tmdb_id, imdb_id, title, original_title, type, overview,
                 release_date, poster_url, imdb_rating, rating_source, rating_votes,
                 added_date, first_seen_at, last_seen_at, last_synced_at, has_zh)
            VALUES (990201, 'tt9900201', '目录补全标题', '', 'movie', '简介',
                    '2025-05-01', '', 8.0, 'imdb', 2000, '2025-05-01',
                    '2025-05-01T00:00:00', '2025-05-01T00:00:00',
                    '2025-05-01T00:00:00', 1)
        """)
        conn.commit()
    finally:
        conn.close()
    linked = get_preference_detail("movie", 990201)
    assert linked["catalog_available"] is True
    assert linked["title"] == "目录补全标题"
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        pref_count = conn.execute(
            "SELECT COUNT(*) FROM title_preferences WHERE tmdb_id=990201"
        ).fetchone()[0]
    finally:
        conn.close()
    assert pref_count == 1
    exported_linked = {item["tmdb_id"]: item for item in export_watchlist()}
    assert exported_linked[990201]["title"] == "目录补全标题"
    # 身份级移除后偏好与快照都不再出现
    update_status_by_identity("movie", 990201, "")
    assert get_preference_detail("movie", 990201) is None


def test_import_metadata_update_counting_and_clear():
    """R05：仅改备注/优先级也计为 updated；完全重复为 unchanged；v2 null 为清空。"""
    tid = _insert_title(990202, title="计数验证", rating=8.0, votes=3000)
    update_title_status(tid, "watchlist")
    try:
        base = next(item for item in export_watchlist() if item["tmdb_id"] == 990202)
        changed = dict(base, note="仅改备注", priority=2)
        result = import_watchlist([changed], schema_version=2)
        assert result["updated"] == 1
        assert result["field_changes"]["note"] == 1
        assert result["field_changes"]["priority"] == 1
        detail = get_title_detail(tid)
        assert detail["note"] == "仅改备注" and detail["priority"] == 2
        same = import_watchlist([changed], schema_version=2)
        assert same["unchanged"] == 1 and same["updated"] == 0
        cleared = dict(changed, note=None)
        cleared_result = import_watchlist([cleared], schema_version=2)
        assert cleared_result["updated"] == 1
        assert get_title_detail(tid)["note"] is None
        # 每条输入归入一种计数，计数之和等于输入条数
        summary = import_watchlist(
            [changed, {"tmdb_id": -1, "type": "movie", "watch_status": "watchlist"},
             changed],
            schema_version=2,
        )
        assert summary["total"] == 3
        assert summary["added"] + summary["updated"] + summary["unchanged"] \
            + summary["protected"] + summary["invalid"] + summary["duplicate"] == 3
        assert summary["invalid"] == 1 and summary["duplicate"] >= 1
    finally:
        update_title_status(tid, "")


def test_import_protects_newer_local_record():
    """R05：默认保护较新的本地记录，显式 force 才按备份覆盖。"""
    tid = _insert_title(990203, title="保护验证", rating=8.0, votes=3000)
    update_title_status(tid, "watchlist")
    try:
        update_title_preference(tid, note="本地新备注")
        backup_item = {
            "tmdb_id": 990203, "type": "movie", "watch_status": "watchlist",
            "note": "旧备份备注", "priority": 0,
            "updated_at": "2000-01-01T00:00:00+00:00",
        }
        protected = import_watchlist([backup_item], schema_version=2)
        assert protected["protected"] == 1
        assert get_title_detail(tid)["note"] == "本地新备注"
        forced = import_watchlist([backup_item], schema_version=2, force=True)
        assert forced["updated"] == 1
        assert get_title_detail(tid)["note"] == "旧备份备注"
    finally:
        update_title_status(tid, "")


def test_provider_offers_stay_per_region():
    """R02：不同地区的平台名与观看方式不能串台。"""
    from app.db.titles import insert_title

    now = "2026-01-01T00:00:00"
    title_id = insert_title({
        "tmdb_id": 990204, "type": "movie", "title": "地区隔离验证",
        "release_date": "2026-01-01", "imdb_rating": 8.0,
        "rating_source": "imdb", "rating_votes": 3000, "has_zh": 1,
        "providers": ["others"],
        "provider_regions": {"others": ["US", "JP"]},
        "provider_labels": {"others": ["MUBI", "U-NEXT"]},
        "provider_offers": [
            {"provider_id": 11, "provider_name": "MUBI", "provider_group": "others",
             "region": "US", "monetization": "flatrate"},
            {"provider_id": 999, "provider_name": "U-NEXT", "provider_group": "others",
             "region": "JP", "monetization": "rent"},
        ],
        "first_seen_at": now, "last_seen_at": now, "last_synced_at": now,
    })
    detail = get_title_detail(title_id)
    offers = {(o["region"], o["provider_name"], o["monetization"]) for o in detail["offers"]}
    assert ("US", "MUBI", "flatrate") in offers
    assert ("JP", "U-NEXT", "rent") in offers
    assert ("US", "U-NEXT", "rent") not in offers
    assert ("JP", "MUBI", "flatrate") not in offers


def test_backfill_offers_publish_negative_conclusion_per_region():
    """R02：单片核验成功后，未出现的地区 offer 必须失效。"""
    from app.backfill_channels import _save_channels

    tid = _insert_title(990205, title="offer核验验证", rating=8.0, votes=3000)
    _save_channels(
        tid, ["others"], {"others": ["US"]}, {"others": ["MUBI"]}, "2026-06-01T00:00:00",
        [{"provider_id": 11, "provider_name": "MUBI", "provider_group": "others",
          "region": "US", "monetization": "flatrate"}],
    )
    detail = get_title_detail(tid)
    assert any(o["region"] == "US" and o["provider_name"] == "MUBI" and not o["legacy"]
               for o in detail["offers"])
    _save_channels(
        tid, ["others"], {"others": ["JP"]}, {"others": ["U-NEXT"]}, "2026-06-02T00:00:00",
        [{"provider_id": 999, "provider_name": "U-NEXT", "provider_group": "others",
          "region": "JP", "monetization": "flatrate"}],
    )
    detail = get_title_detail(tid)
    assert {(o["region"], o["provider_name"]) for o in detail["offers"]} == {("JP", "U-NEXT")}


def test_imdb_id_search_exact():
    """R11：合法 IMDb ID/链接走精确检索，普通文本搜索不受影响。"""
    tid = _insert_title(990206, title="IMDb检索验证", imdb_id="tt9900206")
    _add_availability(tid, "netflix")
    found = get_titles(search="tt9900206")
    assert found["total"] == 1 and found["titles"][0]["id"] == tid
    found_url = get_titles(search="https://www.imdb.com/title/tt9900206/")
    assert found_url["total"] == 1
    missing = get_titles(search="tt9999999")
    assert missing["total"] == 0
    # 普通关键词仍按片名命中
    assert get_titles(search="IMDb检索验证")["total"] == 1


def test_recent_releases_window_has_next_and_future_excluded():
    """R12：时间范围为 [今天-N, 今天]，未来与更早作品排除，分页信息可用。"""
    from datetime import date, timedelta

    today = date.today()

    def day(offset):
        return (today + timedelta(days=offset)).isoformat()

    titles = {
        "today": _insert_title(990207, title="今天上映", release=day(0)),
        "edge": _insert_title(990208, title="窗口边界上映", release=day(-30)),
        "recent": _insert_title(9902081, title="窗口内上映", release=day(-29)),
        "old": _insert_title(990209, title="窗口外上映", release=day(-31)),
        "future": _insert_title(990210, title="未来上映", release=day(5)),
    }
    for tid in titles.values():
        _add_availability(tid, "netflix")
    result = get_recent_releases(days=30, limit=10, page=1)
    ids = {t["id"] for t in result["titles"]}
    assert titles["today"] in ids and titles["recent"] in ids
    assert titles["edge"] in ids  # [today-30, today] 两端都包含
    assert titles["old"] not in ids and titles["future"] not in ids
    assert result["window_end"] == today.isoformat()
    assert result["window_start"] == day(-30)
    assert result["total"] >= 2
    page1 = get_recent_releases(days=30, limit=1, page=1)
    if page1["total"] > 1:
        assert page1["has_next"] is True
        page2 = get_recent_releases(days=30, limit=1, page=2)
        assert page2["page"] == 2 and len(page2["titles"]) == 1


def test_stats_by_status_includes_catalog_missing():
    """A：统计以 preferences 为准，目录缺失也计入片单。"""
    import_watchlist(
        [{"tmdb_id": 990212, "type": "tv", "watch_status": "watched",
          "title": "统计缺失验证"}],
        schema_version=2,
    )
    before = get_stats()["by_status"].get("watched", 0)
    assert before >= 1
    update_status_by_identity("tv", 990212, "")


def test_zh_quality_helper_distinguishes_translation_sources():
    """维护项：区分中文已补全 / 机器翻译 / 仅片名中文 / 暂无中文。"""
    from app.fetcher.common import _zh_quality

    assert _zh_quality('中文片名', '', '中文简介') == 'full'
    assert _zh_quality('English', '', 'translated later', overview_translated=True) == 'machine'
    assert _zh_quality('中文片名', '', 'English overview') == 'title_only'
    assert _zh_quality('English', '', '中文简介') == 'overview_only'
    assert _zh_quality('English', '', 'English overview') == 'none'


def test_zh_quality_persisted_and_returned_in_detail():
    """维护项：zh_quality 落库并在详情返回，旧数据保持 NULL 不误判。"""
    from app.db.titles import insert_title

    now = "2026-01-01T00:00:00"
    title_id = insert_title({
        "tmdb_id": 990213, "type": "movie", "title": "机翻简介验证",
        "release_date": "2026-01-01", "imdb_rating": 8.0,
        "rating_source": "imdb", "rating_votes": 3000, "has_zh": 1,
        "zh_quality": "machine",
        "first_seen_at": now, "last_seen_at": now, "last_synced_at": now,
    })
    detail = get_title_detail(title_id)
    assert detail["zh_quality"] == "machine"


def test_weighted_rating_prefers_more_votes():
    """WP-5：同分作品票数多的排在前面；min_rating 仍按原始分过滤。"""
    tid_low = _insert_title(990301, title="低票同分作品", rating=8.6, votes=1000)
    tid_high = _insert_title(990302, title="高票同分作品", rating=8.6, votes=90000)
    _add_availability(tid_low, "netflix")
    _add_availability(tid_high, "netflix")
    result = get_titles(sort_by="rating", limit=10, min_rating=8.5)
    ids = [t["id"] for t in result["titles"]]
    assert tid_high in ids and tid_low in ids
    assert ids.index(tid_high) < ids.index(tid_low)
    # 徽标数据仍是原始分，不因加权变成先验值
    by_id = {t["id"]: t for t in result["titles"]}
    assert by_id[tid_low]["imdb_rating"] == 8.6


def test_genre_aliases_union_and_year_filter():
    """WP-5：英文题材别名归并到中文题材；多选取并集；年代范围可过滤。"""
    tid_scifi = _insert_title(990303, title="科幻剧集", rating=8.4, votes=5000,
                              genres='["Sci-Fi & Fantasy"]', release="2021-05-01")
    tid_action = _insert_title(990304, title="动作电影", rating=8.4, votes=5000,
                               genres='["动作冒险"]', release="2015-03-01")
    tid_old = _insert_title(990305, title="老片", rating=8.4, votes=5000,
                            genres='["Drama"]', release="1998-01-01")
    for tid in (tid_scifi, tid_action, tid_old):
        _add_availability(tid, "netflix")

    by_scifi = get_titles(genre="科幻", limit=50)
    ids = {t["id"] for t in by_scifi["titles"]}
    assert tid_scifi in ids and tid_action not in ids

    by_union = get_titles(genre=["科幻", "动作"], limit=50)
    ids = {t["id"] for t in by_union["titles"]}
    assert tid_scifi in ids and tid_action in ids and tid_old not in ids

    by_year = get_titles(year_from=2020, year_to=2029, limit=50)
    ids = {t["id"] for t in by_year["titles"]}
    assert tid_scifi in ids and tid_action not in ids and tid_old not in ids


def test_genre_normalization_helpers():
    """WP-5：归一化与查询变体保持互逆，未知题材原样保留。"""
    from app.genres import normalize_genres, genre_query_variants

    assert normalize_genres(["Sci-Fi & Fantasy"]) == ["科幻", "奇幻"]
    assert normalize_genres(["动作冒险"]) == ["动作", "冒险"]
    assert normalize_genres(["Drama", "Drama"]) == ["剧情"]
    assert normalize_genres(["自定义题材"]) == ["自定义题材"]
    assert "Sci-Fi & Fantasy" in genre_query_variants("科幻")
    assert genre_query_variants("自定义题材") == ["自定义题材"]


def test_looks_chinese_ratio_helper():
    """D04：中英混排简介按 CJK 占比判定，不再因含一句中文而跳过翻译。"""
    from app.fetcher.common import _cjk_ratio, _looks_chinese

    assert _looks_chinese("这是完整的中文简介内容") is True
    mixed = "A long English paragraph.只有这一句是中文。"
    assert _looks_chinese(mixed) is False
    assert 0 < _cjk_ratio(mixed) < 0.3
    assert _cjk_ratio("") == 0.0


def test_related_local_diversity_and_reason():
    """F3：本地推荐理由收敛为一个词，同一导演最多出现两次。"""
    from app.db.queries import get_related_titles

    base = _insert_title(990401, title="推荐基准片", rating=8.0, votes=5000,
                         genres='["喜剧"]')
    _add_availability(base, "netflix")
    candidates = []
    for index in range(4):
        tid = _insert_title(990402 + index, title=f"同导演喜剧{index}", rating=8.0,
                            votes=5000, genres='["喜剧"]')
        _add_availability(tid, "netflix")
        candidates.append(tid)
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    try:
        conn.execute("UPDATE titles SET director='同一导演' WHERE id=?", (base,))
        conn.execute(
            "UPDATE titles SET director='同一导演' WHERE id IN (?,?,?,?)",
            candidates,
        )
        conn.commit()
    finally:
        conn.close()
    related = get_related_titles(base, limit=12)
    reasons = [item["reason"] for item in related]
    assert all(reason in {"同题材", "同导演", "同主演", "同产地"} for reason in reasons)
    assert len([reason for reason in reasons if reason == "同导演"]) <= 2


def test_related_no_api_key_returns_fast_without_network():
    """P01：无 TMDB key 时不发起外网请求，负缓存直接兜底。"""
    import asyncio

    from app import related

    original_key = related.TMDB_API_KEY
    related.TMDB_API_KEY = ""
    related._cache.clear()
    try:
        async def call():
            return await asyncio.wait_for(
                related._fetch_tmdb_recommendation_ids(990401, "movie"), timeout=1.0,
            )
        assert asyncio.run(call()) == []
        # 第二次命中负缓存，同样立即返回
        assert asyncio.run(call()) == []
    finally:
        related.TMDB_API_KEY = original_key
        related._cache.clear()


def test_weighted_rating_index_used_by_query_plan():
    """WP-5 性能：表达式索引存在，评分排序走索引而不是全表扫描 + 临时排序。"""
    from app.db.connection import DEFAULT_VISIBILITY_CONDITION_T
    from app.db.queries import WEIGHTED_RATING_SQL

    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.row_factory = sqlite3.Row
    try:
        index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_titles_weighted_rating'"
        ).fetchone()
        assert index is not None and "CASE WHEN" in index["sql"]
        plan = "\n".join(
            row["detail"] for row in conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT t.id FROM titles t "
                f"WHERE {DEFAULT_VISIBILITY_CONDITION_T} "
                f"ORDER BY {WEIGHTED_RATING_SQL} DESC NULLS LAST LIMIT 40"
            )
        )
    finally:
        conn.close()
    assert "idx_titles_weighted_rating" in plan


if __name__ == "__main__":
    import traceback

    fns = sorted(n for n in list(globals()) if n.startswith("test_"))
    failed = 0
    for n in fns:
        try:
            globals()[n]()
            print(f"PASS {n}")
        except Exception:
            failed += 1
            print(f"FAIL {n}")
            traceback.print_exc()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)

