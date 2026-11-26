"""R4-07 / R4-08 回归测试。

R4-07：相关推荐远端缓存命中且足够时不创建未 await 的补足 coroutine；
同一 cache key 的后台预热合并去重；缓存超容量时按到期时间淘汰。
R4-08：RATING_PRIOR_VOTES<=0 时加权评分退化为原始评分，不再因 0/0 得到 NULL。

运行：python -m pytest tests/test_r4_related_scoring.py -q
不依赖外部网络。app 模块延迟导入，避免与同会话其它测试模块的 DATABASE_URL 初始化互相干扰。
"""

import asyncio
import gc
import json
import os
import sqlite3
import sys
import tempfile
import time
import warnings

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 独立临时 DATABASE_URL：仅在 app.config 尚未导入且环境未指定时生效，
# 保证本文件单独运行时不会触碰真实数据库。
if "app.config" not in sys.modules and "DATABASE_URL" not in os.environ:
    _tmp = tempfile.mkdtemp(prefix="streaming-r4-related-")
    os.environ["DATABASE_URL"] = os.path.join(_tmp, "test.db")


def _related():
    import app.related as module

    return module


def _scoring():
    from app import scoring

    return scoring


@pytest.fixture(autouse=True)
def _clean_related_state():
    """用例前后清理共享缓存与预热任务，避免用例间串扰。"""
    from app.db import queries

    related = _related()
    with related._cache_lock:
        related._cache.clear()
    related._warmup_tasks.clear()
    with queries._related_cache_lock:
        queries._related_cache.clear()
        queries._related_pool_cache.clear()
    try:
        yield
    finally:
        with related._cache_lock:
            related._cache.clear()
        related._warmup_tasks.clear()
        with queries._related_cache_lock:
            queries._related_cache.clear()
            queries._related_pool_cache.clear()


# ---------------------------------------------------------------- R4-07


def test_cache_hit_full_does_not_create_unawaited_coroutine(monkeypatch):
    """缓存命中且填满 limit 时不调用本地补足，也不产生 never awaited 警告。"""
    related = _related()
    monkeypatch.setattr(
        related, "_load_base_sync",
        lambda _title_id: {"tmdb_id": 123, "type": "movie"},
    )
    monkeypatch.setattr(
        related, "_load_local_sync",
        lambda media_type, tmdb_ids, limit: [{"id": i} for i in tmdb_ids[:limit]],
    )

    def _must_not_run(*args, **kwargs):
        raise AssertionError("缓存足够时不应调用 get_related_titles")

    monkeypatch.setattr(related, "get_related_titles", _must_not_run)
    with related._cache_lock:
        related._cache[("tmdb", "movie", 123)] = (
            time.monotonic() + 60,
            list(range(1, 13)),
        )

    with warnings.catch_warnings(record=True) as logs:
        warnings.simplefilter("always")
        result = asyncio.run(related.get_related_titles_async(1, limit=12))
        gc.collect()

    assert [item["id"] for item in result] == list(range(1, 13))
    never_awaited = [
        str(w.message) for w in logs if "was never awaited" in str(w.message)
    ]
    assert never_awaited == []


def test_cache_hit_partial_fills_from_local_and_dedupes(monkeypatch):
    """远端缓存不足 limit 时用本地结果补足，重复 id 只保留一次。"""
    related = _related()
    monkeypatch.setattr(
        related, "_load_base_sync",
        lambda _title_id: {"tmdb_id": 123, "type": "movie"},
    )
    monkeypatch.setattr(
        related, "_load_local_sync",
        lambda *args, **kwargs: [{"id": 1}, {"id": 2}],
    )
    monkeypatch.setattr(
        related, "get_related_titles",
        lambda *args, **kwargs: [
            {"id": 1}, {"id": 2}, {"id": 3}, {"id": 3}, {"id": 4}, {"id": 5},
        ],
    )
    with related._cache_lock:
        related._cache[("tmdb", "movie", 123)] = (time.monotonic() + 60, [1, 2])

    result = asyncio.run(related.get_related_titles_async(1, limit=4))

    ids = [item["id"] for item in result]
    assert ids == [1, 2, 3, 4]
    assert len(ids) == len(set(ids))


def test_no_base_returns_empty_without_local_query(monkeypatch):
    """基础作品查不到时直接返回空，不触发本地推荐查询。"""
    related = _related()
    monkeypatch.setattr(related, "_load_base_sync", lambda _title_id: None)

    def _must_not_run(*args, **kwargs):
        raise AssertionError("无基础作品时不应查询本地推荐")

    monkeypatch.setattr(related, "get_related_titles", _must_not_run)
    assert asyncio.run(related.get_related_titles_async(1, limit=4)) == []


def test_local_query_failure_propagates(monkeypatch):
    """契约：本地补足失败时异常向上传递（修复不吞异常、不改变调用层处理）。"""
    related = _related()
    monkeypatch.setattr(
        related, "_load_base_sync",
        lambda _title_id: {"tmdb_id": None, "type": "movie"},
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("local related failure")

    monkeypatch.setattr(related, "get_related_titles", _boom)
    with pytest.raises(RuntimeError, match="local related failure"):
        asyncio.run(related.get_related_titles_async(1, limit=4))


def test_warmup_dedupes_same_key_and_cleans_up(monkeypatch):
    """同一 cache key 的 in-flight 预热只调度一次，完成后清理并允许重新调度。"""
    related = _related()
    calls = []
    release = asyncio.Event()

    async def slow_fetch(tmdb_id, media_type):
        calls.append((tmdb_id, media_type))
        await release.wait()
        return [1, 2]

    monkeypatch.setattr(related, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(related, "_fetch_tmdb_recommendation_ids", slow_fetch)

    async def scenario():
        try:
            related._schedule_warmup(123, "movie")
            related._schedule_warmup(123, "movie")
            related._schedule_warmup(456, "movie")
            await asyncio.sleep(0)
            assert calls == [(123, "movie"), (456, "movie")]
            assert len(related._warmup_tasks) == 2
        finally:
            release.set()
            pending = list(related._warmup_tasks.values())
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                await asyncio.sleep(0)

    asyncio.run(scenario())
    assert related._warmup_tasks == {}

    # 任务结束后同一 key 可再次调度
    async def instant_fetch(tmdb_id, media_type):
        calls.append((tmdb_id, media_type))
        return []

    monkeypatch.setattr(related, "_fetch_tmdb_recommendation_ids", instant_fetch)

    async def reschedule():
        related._schedule_warmup(123, "movie")
        await asyncio.sleep(0)
        await asyncio.gather(
            *related._warmup_tasks.values(), return_exceptions=True
        )
        await asyncio.sleep(0)

    asyncio.run(reschedule())
    assert calls[-1] == (123, "movie")
    assert related._warmup_tasks == {}


def test_cache_set_evicts_earliest_expiry_when_over_capacity(monkeypatch):
    """清过期后仍超容量时，按到期时间淘汰最旧项，维持硬上限。"""
    related = _related()
    monkeypatch.setattr(related, "_MAX_CACHE_ENTRIES", 3)
    now = time.monotonic()
    with related._cache_lock:
        for index in range(5):
            related._cache[("k", index)] = (now + index, index)

    related._cache_set(("fresh",), "value", ttl=1000)

    assert len(related._cache) == 3
    assert ("k", 0) not in related._cache
    assert ("k", 1) not in related._cache
    assert ("k", 2) not in related._cache
    assert ("k", 3) in related._cache
    assert ("k", 4) in related._cache
    assert ("fresh",) in related._cache


def test_cache_set_clears_expired_before_evicting(monkeypatch):
    """超容量时优先清理过期项；清理后不超容量就不淘汰未过期项。"""
    related = _related()
    monkeypatch.setattr(related, "_MAX_CACHE_ENTRIES", 3)
    now = time.monotonic()
    with related._cache_lock:
        related._cache[("expired",)] = (now - 10, "old")
        related._cache[("alive1",)] = (now + 10, "new1")
        related._cache[("alive2",)] = (now + 20, "new2")

    related._cache_set(("fresh",), "value", ttl=100)

    assert ("expired",) not in related._cache
    assert ("alive1",) in related._cache
    assert ("alive2",) in related._cache
    assert ("fresh",) in related._cache
    assert len(related._cache) == 3


# ---------------------------------------------------------------- R4-08


def _rate(expression, rating, votes):
    conn = sqlite3.connect(":memory:")
    try:
        row = conn.execute(
            f"SELECT {expression}"
            " FROM (SELECT ? AS imdb_rating, ? AS rating_votes) t",
            (rating, votes),
        ).fetchone()
    finally:
        conn.close()
    return row[0]


def test_zero_prior_returns_raw_rating_instead_of_null(monkeypatch):
    """RATING_PRIOR_VOTES=0 时零票/NULL 票不再因 0/0 得到 NULL。"""
    scoring = _scoring()
    monkeypatch.setattr(scoring, "RATING_PRIOR_VOTES", 0)
    expression = scoring.weighted_rating_sql("t")

    assert "/" not in expression
    assert expression == (
        "CASE WHEN t.imdb_rating IS NULL THEN NULL ELSE t.imdb_rating END"
    )
    assert _rate(expression, 8.0, 0) == 8.0
    assert _rate(expression, 8.0, None) == 8.0
    assert _rate(expression, None, 0) is None
    assert _rate(expression, None, None) is None
    assert _rate(expression, 7.25, 12345) == 7.25


def test_zero_prior_keeps_null_fallback(monkeypatch):
    """关闭先验后仍保留无评分 null_fallback 规则（片单 -1）。"""
    scoring = _scoring()
    monkeypatch.setattr(scoring, "RATING_PRIOR_VOTES", 0)

    library_expression = scoring.weighted_rating_sql("t", null_fallback=-1)
    assert _rate(library_expression, None, 0) == -1
    assert _rate(library_expression, None, None) == -1
    assert _rate(library_expression, 8.0, 0) == 8.0

    no_alias = scoring.weighted_rating_sql(alias="", null_fallback=-1)
    assert _rate(no_alias, None, None) == -1


def test_zero_prior_index_expression_reuses_same_expression(monkeypatch):
    """索引表达式继续复用同一函数生成的表达式。"""
    scoring = _scoring()
    monkeypatch.setattr(scoring, "RATING_PRIOR_VOTES", 0)
    assert scoring.weighted_rating_index_expression() == scoring.weighted_rating_sql(
        alias=""
    )


def test_positive_prior_matches_bayesian_formula(monkeypatch):
    """m=3000 时保持贝叶斯加权行为，与手工公式一致。"""
    scoring = _scoring()
    monkeypatch.setattr(scoring, "RATING_PRIOR_VOTES", 3000)
    expression = scoring.weighted_rating_sql("t")
    mean = scoring.RATING_PRIOR_MEAN

    for rating, votes in ((8.0, 0), (8.0, 100), (8.0, 100000), (9.2, 42)):
        expected = (votes / (votes + 3000)) * rating \
            + (3000 / (votes + 3000)) * mean
        assert _rate(expression, rating, votes) == pytest.approx(expected)

    assert _rate(expression, None, 0) is None


# ---------------------------------------------------------------- 相关推荐准确度与 L2 缓存


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """每条推荐算法单测使用独立临时 SQLite，避免与其他测试模块共享状态。"""
    db_path = str(tmp_path / "related_scoring.db")
    monkeypatch.setattr("app.db.connection.DATABASE_URL", db_path)
    from app.db.schema import init_db

    init_db()
    return db_path


def _seed_title(
    db_path,
    tmdb_id,
    *,
    media_type="movie",
    title="测试作品",
    rating=8.0,
    votes=5000,
    release="2022-05-01",
    genres=None,
    cast=None,
    director="",
    overview="",
    countries=("US",),
    providers=("netflix",),
):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO titles
                (tmdb_id, imdb_id, title, original_title, type, overview,
                 release_date, poster_url, imdb_rating, rating_source,
                 rating_votes, added_date, first_seen_at, last_seen_at,
                 last_synced_at, has_zh, director, cast_json, genres_json)
            VALUES (?, ?, ?, '', ?, ?, ?, '', ?, 'imdb', ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                tmdb_id,
                f"tt{tmdb_id:07d}",
                title,
                media_type,
                overview,
                release,
                rating,
                votes,
                "2024-01-01",
                "2024-01-01T00:00:00",
                "2024-01-01",
                "2024-01-01T00:00:00",
                director,
                json.dumps(list(cast or []), ensure_ascii=False),
                json.dumps(list(genres), ensure_ascii=False) if genres is not None else None,
            ),
        )
        tid = cur.lastrowid
        for code in countries:
            cur.execute(
                "INSERT INTO title_countries (title_id, country_code) VALUES (?, ?)",
                (tid, code),
            )
        for pname in providers:
            cur.execute(
                """
                INSERT INTO title_provider_availability
                    (title_id, provider_name, region, monetization_type,
                     first_seen_at, last_seen_at, is_active)
                VALUES (?, ?, 'TW', 'flatrate', '2024-01-01T00:00:00',
                        '2024-01-01T00:00:00', 1)
                """,
                (tid, pname),
            )
        conn.commit()
        return tid
    finally:
        conn.close()


def test_local_related_normalizes_genre_aliases(isolated_db):
    """历史别名（如 Sci-Fi & Fantasy）归一化后可命中 ["科幻", "奇幻"]。"""
    from app.db.queries import get_related_titles

    base = _seed_title(
        isolated_db, 1001, media_type="tv", title="怪奇物语",
        genres=["Sci-Fi & Fantasy", "剧情"],
    )
    matched = _seed_title(
        isolated_db, 1002, media_type="tv", title="暗黑",
        genres=["科幻", "奇幻", "剧情"],
    )
    unrelated = _seed_title(
        isolated_db, 1003, media_type="tv", title="生活喜剧",
        genres=["喜剧"],
    )

    items = get_related_titles(base, limit=5)
    ids = [item["id"] for item in items]
    assert matched in ids
    assert unrelated not in ids


def test_local_related_idf_prefers_rare_genre_over_generic_drama(isolated_db):
    """稀有题材（科幻）IDF 高于泛题材（剧情），精准匹配排在泛剧情前。"""
    from app.db.queries import get_related_titles

    base = _seed_title(isolated_db, 1010, title="降临", genres=["剧情", "科幻"])
    # 造一批只带“剧情”的作品拉低“剧情”的 IDF
    for idx in range(8):
        _seed_title(
            isolated_db, 1020 + idx, title=f"普通剧情片{idx}",
            genres=["剧情"], rating=8.0, votes=5000,
        )
    scifi_peer = _seed_title(
        isolated_db, 1011, title="接触未来", genres=["科幻"], rating=7.8, votes=5000,
    )
    drama_only = _seed_title(
        isolated_db, 1012, title="纯剧情", genres=["剧情"], rating=9.0, votes=5000,
    )

    items = get_related_titles(base, limit=10)
    ids = [item["id"] for item in items]
    assert ids.index(scifi_peer) < ids.index(drama_only)


def test_local_related_enforces_format_boundary_for_animation(isolated_db):
    """动画片优先匹配同样是动画的作品，真人片虽共享副题材也会被边界惩罚压低。"""
    from app.db.queries import get_related_titles

    base = _seed_title(
        isolated_db, 1040, title="机器人总动员", genres=["动画", "家庭", "科幻"],
    )
    anim_peer = _seed_title(
        isolated_db, 1041, title="荒野机器人", genres=["动画", "家庭", "科幻"], rating=8.2,
    )
    live_action = _seed_title(
        isolated_db, 1042, title="真人家庭科幻片", genres=["家庭", "科幻"], rating=8.5,
    )

    items = get_related_titles(base, limit=5)
    assert items[0]["id"] == anim_peer
    assert [item["id"] for item in items].index(anim_peer) < [item["id"] for item in items].index(live_action)


def test_local_related_prefers_acclaimed_over_borderline_filler(isolated_db):
    """同条件下高分高口碑佳作应排在 7.0 边缘片前面。"""
    from app.db.queries import get_related_titles

    base = _seed_title(isolated_db, 1050, title="暗夜追凶", genres=["犯罪", "剧情"])
    borderline = _seed_title(
        isolated_db, 1051, title="平庸档案", genres=["犯罪", "剧情"],
        rating=7.0, votes=1200, release="2022-06-01",
    )
    acclaimed = _seed_title(
        isolated_db, 1052, title="经典迷局", genres=["犯罪", "剧情"],
        rating=8.9, votes=120000, release="2021-06-01",
    )

    items = get_related_titles(base, limit=5)
    ids = [item["id"] for item in items]
    assert ids.index(acclaimed) < ids.index(borderline)


def test_local_related_missing_genres_matches_overview_semantics(isolated_db):
    """缺失 genres_json 时通过片名与简介关键词语义命中真实同类剧，而非无差别高分榜。"""
    from app.db.queries import get_related_titles

    base = _seed_title(
        isolated_db, 1060, media_type="tv", title="切尔诺贝利", genres=None,
        overview="1986年苏联切尔诺贝利核电站发生严重核泄漏灾难事故，消防员与核物理学家奔赴反应堆抢险封锁辐射区域。",
    )
    topical = _seed_title(
        isolated_db, 1061, media_type="tv", title="福岛核事故危机", genres=["剧情", "历史"],
        rating=7.8, votes=8000, countries=("JP",),
        overview="2011年核电站地震海啸后发生反应堆核泄漏事故，一线工程师冒着辐射危险抢险封锁。",
    )
    unrelated_top = _seed_title(
        isolated_db, 1062, media_type="tv", title="高分美食综艺", genres=["真人秀"],
        rating=9.4, votes=50000, countries=("GB",),
        overview="米其林主厨环游世界寻找传统烘焙甜点配方与地方食材的故事。",
    )

    items = get_related_titles(base, limit=5)
    ids = [item["id"] for item in items]
    assert ids[0] == topical
    assert unrelated_top not in ids


def test_local_related_same_director_gets_director_reason_and_cap(isolated_db):
    """同导演且同题材时理由展示“同导演”，同时同一导演不超过 2 部上限。"""
    from app.db.queries import get_related_titles

    base = _seed_title(
        isolated_db, 1070, title="诺兰基准", genres=["科幻", "悬疑"], director="克里斯托弗·诺兰",
    )
    same_dir_ids = [
        _seed_title(
            isolated_db, 1071 + i, title=f"诺兰作品{i}",
            genres=["科幻", "悬疑"], director="克里斯托弗·诺兰", rating=8.4 - i * 0.1,
        )
        for i in range(4)
    ]

    items = get_related_titles(base, limit=10)
    assert len([x for x in items if x["id"] in same_dir_ids]) == 2
    assert all(x["reason"] == "同导演" for x in items if x["id"] in same_dir_ids)


def test_local_related_pool_invalidates_after_direct_db_write(isolated_db):
    """直接向 SQLite 插入新作品后，库指纹变化并立刻刷新推荐结果。"""
    from app.db.queries import get_related_titles

    base = _seed_title(isolated_db, 1080, title="基准惊悚片", genres=["惊悚", "悬疑"])
    _seed_title(isolated_db, 1081, title="旧惊悚片", genres=["惊悚"], rating=7.4)

    first = get_related_titles(base, limit=5)
    assert len(first) == 1

    new_id = _seed_title(
        isolated_db, 1082, title="新入库神作", genres=["惊悚", "悬疑"], rating=8.8, votes=80000,
    )
    second = get_related_titles(base, limit=5)
    assert [item["id"] for item in second][0] == new_id


def test_tmdb_fetches_two_pages_and_persists_l2_cache(isolated_db, monkeypatch):
    """TMDB 并发拉取 page 1+2 去重合并、单页失败可降级、并写入 SQLite L2 持久化缓存。"""
    related = _related()
    monkeypatch.setattr(related, "TMDB_API_KEY", "test-key")
    seen_pages = []

    async def fake_fetch_tmdb(endpoint, params=None, client=None, retries=0):
        page = (params or {}).get("page")
        seen_pages.append(page)
        if page == 1:
            return {"results": [{"id": 10}, {"id": 20}]}
        return {"results": [{"id": 20}, {"id": 30}]}

    monkeypatch.setattr(related, "fetch_tmdb", fake_fetch_tmdb)
    ids = asyncio.run(related._fetch_tmdb_recommendation_ids(555, "movie"))
    assert sorted(seen_pages) == [1, 2]
    assert ids == [10, 20, 30]

    # 清空 L1 缓存并禁用网络后，仍应直接命中 SQLite L2 缓存
    with related._cache_lock:
        related._cache.clear()

    async def should_not_hit_network(*args, **kwargs):
        raise AssertionError("L2 缓存命中时不应发起 TMDB 网络请求")

    monkeypatch.setattr(related, "fetch_tmdb", should_not_hit_network)
    from_l2 = asyncio.run(related._fetch_tmdb_recommendation_ids(555, "movie"))
    assert from_l2 == [10, 20, 30]

    # 若 page 2 报错但 page 1 成功，仍保留 page 1 结果并正常缓存
    with related._cache_lock:
        related._cache.clear()
    conn = sqlite3.connect(isolated_db)
    conn.execute("DELETE FROM related_cache")
    conn.commit()
    conn.close()

    async def partial_fail(endpoint, params=None, client=None, retries=0):
        if (params or {}).get("page") == 2:
            raise httpx.ReadTimeout("page 2 timeout")
        return {"results": [{"id": 77}]}

    monkeypatch.setattr(related, "fetch_tmdb", partial_fail)
    partial_ids = asyncio.run(related._fetch_tmdb_recommendation_ids(666, "tv"))
    assert partial_ids == [77]


def test_first_visit_wait_returns_fast_tmdb_and_times_out_gracefully(isolated_db, monkeypatch):
    """首次访问 TMDB 在等待窗口内返回则首屏直出；超时则不阻塞并回退到本地推荐。"""
    related = _related()
    monkeypatch.setattr(related, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(related, "_FIRST_VISIT_WAIT_SECONDS", 0.5)

    base = _seed_title(isolated_db, 2001, title="基准电影", genres=["科幻"])
    tmdb_hit = _seed_title(isolated_db, 2002, title="TMDB首屏命中", genres=["喜剧"])
    local_hit = _seed_title(isolated_db, 2003, title="本地相似兜底", genres=["科幻"])

    async def fast_tmdb(endpoint, params=None, client=None, retries=0):
        if (params or {}).get("page") == 1:
            return {"results": [{"id": 2002}]}
        return {"results": []}

    monkeypatch.setattr(related, "fetch_tmdb", fast_tmdb)
    fast_res = asyncio.run(related.get_related_titles_async(base, limit=1))
    assert [item["id"] for item in fast_res] == [tmdb_hit]
    assert fast_res[0]["reason"] == "TMDB 推荐"

    # 慢网络超出等待窗口：先回退本地推荐，同时预热任务后台继续跑完写入缓存
    with related._cache_lock:
        related._cache.clear()
    conn = sqlite3.connect(isolated_db)
    conn.execute("DELETE FROM related_cache")
    conn.commit()
    conn.close()
    monkeypatch.setattr(related, "_FIRST_VISIT_WAIT_SECONDS", 0.03)
    release = asyncio.Event()

    async def slow_tmdb(endpoint, params=None, client=None, retries=0):
        await release.wait()
        return {"results": [{"id": 2002}]} if (params or {}).get("page") == 1 else {"results": []}

    monkeypatch.setattr(related, "fetch_tmdb", slow_tmdb)

    async def run_timeout_case():
        try:
            fallback_res = await related.get_related_titles_async(base, limit=2)
            assert [item["id"] for item in fallback_res] == [local_hit, tmdb_hit] or fallback_res[0]["id"] == local_hit
        finally:
            release.set()
            pending = list(related._warmup_tasks.values())
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                await asyncio.sleep(0)

    asyncio.run(run_timeout_case())

