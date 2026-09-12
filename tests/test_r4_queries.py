"""R4-02 / R4-03 回归：近期窗口上界与题材 facet 规范化去重。

运行：python -m pytest tests/test_r4_queries.py -q

不依赖外部网络与生产数据库，使用临时 SQLite。
与 tests/test_review_regressions.py 共用数据库的约定：
- 单独运行本文件时，在首个测试前设置 DATABASE_URL 再 init_db；
- 全套运行时 pytest 会先收集本文件（模块名排序在前），此时不在导入期绑定
  连接，等测试真正执行时复用 app.config 已选定的库，避免两个测试模块
  各自连接不同的临时库导致交叉失败。
"""

import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_db_ready = False


def _ensure_db():
    """按需初始化测试库；已有 app.config（其他测试模块先导入）时复用其数据库。"""
    global _db_ready
    if _db_ready:
        return
    if "app.config" not in sys.modules:
        tmp_dir = tempfile.mkdtemp(prefix="streaming-r4-test-")
        os.environ["DATABASE_URL"] = os.path.join(tmp_dir, "test.db")
    from app.db.schema import init_db

    init_db()
    _db_ready = True


def _connect():
    from app.db.connection import get_db_connection

    return get_db_connection()


def _insert_title(tmdb_id, media_type="movie", title="R4测试作品", rating=8.0,
                  votes=3000, release="2024-01-01", has_zh=1,
                  genres='["Drama"]', runtime=100):
    """直接写库：保留原始题材别名，模拟未回填的历史数据。"""
    _ensure_db()
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO titles
                (tmdb_id, imdb_id, title, original_title, type, overview,
                 release_date, poster_url, imdb_rating, rating_source,
                 rating_votes, added_date, first_seen_at, last_seen_at,
                 last_synced_at, has_zh, genres_json, runtime)
            VALUES (?, ?, ?, '', ?, '简介', ?, '', ?, 'imdb', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tmdb_id, f"tt{tmdb_id:07d}", title, media_type, release,
              rating, votes, "2024-01-01", "2024-01-01T00:00:00",
              "2024-01-01", "2024-01-01T00:00:00", has_zh, genres, runtime))
        title_id = cursor.lastrowid
        conn.commit()
        return title_id
    finally:
        conn.close()


def _add_availability(title_id, provider="netflix", region="US"):
    _ensure_db()
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO title_provider_availability
                (title_id, provider_name, region, monetization_type,
                 first_seen_at, last_seen_at, is_active)
            VALUES (?, ?, ?, 'mixed', '2024-01-01', '2024-01-01', 1)
        """, (title_id, provider, region))
        conn.commit()
    finally:
        conn.close()


def _stats_genres():
    from app.db.queries import get_stats, invalidate_stats_cache

    invalidate_stats_cache()
    return get_stats()["genres"]


def _genre_facet_counts():
    return {row["name"]: row["count"] for row in _stats_genres()}


def test_future_release_excluded_by_released_before():
    """R4-02：released_after 单独使用仍可查未来；成对传入上界后排除未来作品。"""
    from app.db.queries import get_recent_releases, get_titles, invalidate_stats_cache

    today = date.today()
    window_start = (today - timedelta(days=30)).isoformat()
    tid = _insert_title(991001, title="R4未来上映验证", release="2099-01-01")
    _add_availability(tid)
    invalidate_stats_cache()

    loose = get_titles(search="R4未来上映验证", released_after=window_start)
    assert loose["total"] == 1 and loose["titles"][0]["id"] == tid

    bounded = get_titles(
        search="R4未来上映验证",
        released_after=window_start,
        released_before=today.isoformat(),
    )
    assert bounded["total"] == 0

    releases = get_recent_releases(days=30)
    assert tid not in {row["id"] for row in releases["titles"]}


def test_window_boundary_today_included_tomorrow_excluded():
    """R4-02：窗口上界闭区间 —— 今天包含，明天排除（日期动态生成不过期）。"""
    from app.db.queries import get_titles, invalidate_stats_cache

    today = date.today()
    window_start = (today - timedelta(days=30)).isoformat()
    tid_today = _insert_title(991002, title="R4今天上映验证", release=today.isoformat())
    tid_tomorrow = _insert_title(
        991003, title="R4明天上映验证",
        release=(today + timedelta(days=1)).isoformat(),
    )
    _add_availability(tid_today)
    _add_availability(tid_tomorrow)
    invalidate_stats_cache()

    included = get_titles(
        search="R4今天上映验证",
        released_after=window_start,
        released_before=today.isoformat(),
    )
    assert included["total"] == 1 and included["titles"][0]["id"] == tid_today

    excluded = get_titles(
        search="R4明天上映验证",
        released_after=window_start,
        released_before=today.isoformat(),
    )
    assert excluded["total"] == 0


def test_watchlist_branch_respects_released_before():
    """R4-02：片单分支与普通列表同样支持 released_before 上界。"""
    from app.db.queries import get_titles, invalidate_stats_cache, update_title_status

    today = date.today()
    window_start = (today - timedelta(days=30)).isoformat()
    tid = _insert_title(991004, title="R4片单未来验证", release="2099-01-01")
    _add_availability(tid)
    update_title_status(tid, "watchlist")
    try:
        invalidate_stats_cache()
        without_bound = get_titles(
            watch_status="watchlist", search="R4片单未来验证",
            released_after=window_start,
        )
        assert without_bound["total"] == 1
        assert without_bound["titles"][0]["id"] == tid

        bounded = get_titles(
            watch_status="watchlist", search="R4片单未来验证",
            released_after=window_start, released_before=today.isoformat(),
        )
        assert bounded["total"] == 0
        assert bounded["titles"] == []
    finally:
        update_title_status(tid, "")


def test_window_total_matches_list_content():
    """R4-02：窗口筛选下 total 与逐页内容一致，返回项全部落在窗口内。"""
    from app.db.queries import get_titles, invalidate_stats_cache

    today = date.today()
    window_start = (today - timedelta(days=30)).isoformat()
    ids = {}
    for tmdb_id, offset in ((991010, -30), (991011, 0), (991012, -31), (991013, 1)):
        tid = _insert_title(
            tmdb_id,
            title=f"R4窗口计数{offset}",
            release=(today + timedelta(days=offset)).isoformat(),
        )
        _add_availability(tid)
        ids[offset] = tid
    invalidate_stats_cache()

    bounded = get_titles(
        search="R4窗口计数",
        released_after=window_start,
        released_before=today.isoformat(),
        limit=50,
    )
    assert bounded["total"] == 2
    assert bounded["total"] == len(bounded["titles"])
    returned = {row["id"]: row["release_date"] for row in bounded["titles"]}
    assert set(returned) == {ids[-30], ids[0]}
    assert all(window_start <= value <= today.isoformat() for value in returned.values())

    # 分页计数与内容取自同一 where/params
    paged = get_titles(
        search="R4窗口计数",
        released_after=window_start,
        released_before=today.isoformat(),
        limit=1, page=1,
    )
    assert paged["total"] == 2 and len(paged["titles"]) == 1 and paged["has_next"] is True

    # 不带 released_before 时仍保留通用语义：未来作品可见
    loose = get_titles(search="R4窗口计数", released_after=window_start, limit=50)
    assert loose["total"] == 3
    assert ids[1] in {row["id"] for row in loose["titles"]}


def test_genre_facet_deduplicates_aliases_within_title():
    """R4-03：同一作品同时含两个科幻别名，facet 只计 1 次，且与筛选一致。"""
    from app.db.queries import get_titles

    before = _genre_facet_counts().get("科幻", 0)
    tid = _insert_title(
        991020, title="R4混合别名科幻",
        genres='["Science Fiction","Sci-Fi & Fantasy"]',
    )
    _add_availability(tid)

    after = _genre_facet_counts()
    assert after.get("科幻", 0) == before + 1
    assert get_titles(genre="科幻", limit=50)["total"] == after["科幻"]

    # 排序契约：count desc, name asc
    genres = _stats_genres()
    assert genres == sorted(genres, key=lambda row: (-row["count"], row["name"]))


def test_genre_facet_counts_two_titles_using_different_aliases():
    """R4-03：两部作品各用一个科幻别名，facet 计 2；混合别名作品只贡献一次奇幻。"""
    from app.db.queries import get_titles

    before = _genre_facet_counts()
    tid_a = _insert_title(991021, title="R4别名科幻甲", genres='["Science Fiction"]')
    tid_b = _insert_title(991022, title="R4别名科幻乙", genres='["Sci-Fi & Fantasy"]')
    _add_availability(tid_a)
    _add_availability(tid_b)

    after = _genre_facet_counts()
    assert after.get("科幻", 0) == before.get("科幻", 0) + 2
    assert after.get("奇幻", 0) == before.get("奇幻", 0) + 1
    assert get_titles(genre="科幻", limit=50)["total"] == after["科幻"]


def test_genre_facet_two_canonical_genres_each_counted_once():
    """R4-03：一部作品含 Action + Drama，动作/剧情各计 1。"""
    from app.db.queries import get_titles

    before = _genre_facet_counts()
    tid = _insert_title(991023, title="R4动作剧情", genres='["Action","Drama"]')
    _add_availability(tid)

    after = _genre_facet_counts()
    assert after.get("动作", 0) == before.get("动作", 0) + 1
    assert after.get("剧情", 0) == before.get("剧情", 0) + 1
    assert get_titles(genre="动作", limit=50)["total"] == after["动作"]


def test_genre_facet_compatible_with_null_and_invalid_json():
    """R4-03 兼容性：genres_json 为 NULL / 非法 JSON 的可见作品不参与 facet 且不报错。"""
    before = _genre_facet_counts()
    tid_null = _insert_title(991024, title="R4题材空值", genres=None)
    tid_bad = _insert_title(991025, title="R4题材异常", genres="not-json")
    _add_availability(tid_null)
    _add_availability(tid_bad)

    after = _genre_facet_counts()
    assert after == before


def test_api_released_before_validation():
    """R4-02（API 层）：日期格式非法或上下界倒置时返回 422。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)

    bad_format = client.get("/api/titles", params={"released_before": "2099-1-1"})
    assert bad_format.status_code == 422

    reversed_order = client.get(
        "/api/titles",
        params={"released_after": "2026-09-10", "released_before": "2026-09-01"},
    )
    assert reversed_order.status_code == 422
    assert "released_before" in reversed_order.json()["detail"]

    ordered = client.get(
        "/api/titles",
        params={"released_after": "2026-09-01", "released_before": "2026-09-10",
                "limit": 1},
    )
    assert ordered.status_code == 200
    assert "titles" in ordered.json()


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
