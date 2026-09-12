"""R4-07 / R4-08 回归测试。

R4-07：相关推荐远端缓存命中且足够时不创建未 await 的补足 coroutine；
同一 cache key 的后台预热合并去重；缓存超容量时按到期时间淘汰。
R4-08：RATING_PRIOR_VOTES<=0 时加权评分退化为原始评分，不再因 0/0 得到 NULL。

运行：python -m pytest tests/test_r4_related_scoring.py -q
不依赖外部网络。app 模块延迟导入，避免与同会话其它测试模块的 DATABASE_URL 初始化互相干扰。
"""

import asyncio
import gc
import os
import sqlite3
import sys
import tempfile
import time
import warnings

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
    related = _related()
    with related._cache_lock:
        related._cache.clear()
    related._warmup_tasks.clear()
    try:
        yield
    finally:
        with related._cache_lock:
            related._cache.clear()
        related._warmup_tasks.clear()


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
