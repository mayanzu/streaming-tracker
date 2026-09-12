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
    get_stats,
    get_title_detail,
    get_titles,
    import_watchlist,
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
    """回填写入 label 后，该作品退出候选集（逐步推进可验证）。"""
    from app.backfill_channels import _load_candidates, _save_channels

    tid = _insert_title(900110, title="渠道回填验证", rating=8.4, votes=5000)
    _add_availability(tid, "others")
    ids_before = {c["id"] for c in _load_candidates()}
    assert tid in ids_before
    rows = _save_channels(tid, ["others"], {"others": ["US"]},
                          {"others": ["MUBI"]}, "2024-01-01T00:00:00")
    assert rows >= 1
    detail = get_title_detail(tid)
    by_name = {d["provider"]: d for d in detail["provider_details"]}
    assert by_name["others"]["labels"] == ["MUBI"]
    ids_after = {c["id"] for c in _load_candidates()}
    assert tid not in ids_after


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

