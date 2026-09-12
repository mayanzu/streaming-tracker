"""SQLite 连接管理与全局查询条件常量。"""

import os
import sqlite3
import time
from contextlib import contextmanager

from app.config import (
    ASIAN_MIN_IMDB_VOTES,
    ASIAN_ORIGIN_COUNTRIES,
    DATABASE_URL,
    MIN_IMDB_VOTES,
    MIN_IMDB_VOTES_GRACE,
    NEW_TITLE_GRACE_DAYS,
    PROVIDERS,
)

TRUSTED_RATING_SOURCES = ("imdb", "omdb")
# 可信 = 有 IMDb/OMDb 评分且票数达标，或无评分但首播在宽限期内（新剧先收录展示，评分回填后走正常路径）。
# 门槛与 app.fetcher.enrich 保持一致：宽限期内用 MIN_IMDB_VOTES_GRACE，否则用 MIN_IMDB_VOTES；
# 新片窗口为 [今天-N天, 今天]，未来上映日期不算宽限期。
# 注意：release_date 为空/非法时 date() 得 NULL，比较为假，不会误放行。
_GRACE_DAYS = max(int(NEW_TITLE_GRACE_DAYS), 0)
_MIN_VOTES = max(int(MIN_IMDB_VOTES), 0)
_MIN_VOTES_GRACE = max(int(MIN_IMDB_VOTES_GRACE), 0)
_RECENT_WINDOW = f"date(release_date) BETWEEN date('now', '-{_GRACE_DAYS} days') AND date('now')"
_RECENT_WINDOW_T = f"date(t.release_date) BETWEEN date('now', '-{_GRACE_DAYS} days') AND date('now')"
_ASIAN_ORIGINS_SQL = ",".join(f"'{code}'" for code in ASIAN_ORIGIN_COUNTRIES)
_ASIAN_MIN_VOTES = max(int(ASIAN_MIN_IMDB_VOTES), 0)
ASIAN_ORIGIN_CONDITION = (
    "EXISTS (SELECT 1 FROM title_countries asian_c"
    " WHERE asian_c.title_id = titles.id"
    f" AND asian_c.country_code IN ({_ASIAN_ORIGINS_SQL}))"
)
ASIAN_ORIGIN_CONDITION_T = (
    "EXISTS (SELECT 1 FROM title_countries asian_c"
    " WHERE asian_c.title_id = t.id"
    f" AND asian_c.country_code IN ({_ASIAN_ORIGINS_SQL}))"
)
# 亚洲产地内容票数达标即可信（门槛低于 MIN_IMDB_VOTES），避免漏掉低票数亚洲剧集
_ASIAN_RATING_BASE = (
    "imdb_rating IS NOT NULL AND rating_source IN ('imdb', 'omdb')"
    f" AND COALESCE(rating_votes, 0) >= {_ASIAN_MIN_VOTES}"
)
_ASIAN_RATING_BASE_T = (
    "t.imdb_rating IS NOT NULL AND t.rating_source IN ('imdb', 'omdb')"
    f" AND COALESCE(t.rating_votes, 0) >= {_ASIAN_MIN_VOTES}"
)
TRUSTED_RATING_CONDITION = (
    "(imdb_rating IS NOT NULL AND rating_source IN ('imdb', 'omdb')"
    f" AND COALESCE(rating_votes, 0)"
    f" >= CASE WHEN {_RECENT_WINDOW} THEN {_MIN_VOTES_GRACE} ELSE {_MIN_VOTES} END"
    f" OR ({_ASIAN_RATING_BASE} AND {ASIAN_ORIGIN_CONDITION})"
    f" OR (imdb_rating IS NULL AND {_RECENT_WINDOW}))"
)
TRUSTED_RATING_CONDITION_T = (
    "(t.imdb_rating IS NOT NULL AND t.rating_source IN ('imdb', 'omdb')"
    f" AND COALESCE(t.rating_votes, 0)"
    f" >= CASE WHEN {_RECENT_WINDOW_T} THEN {_MIN_VOTES_GRACE} ELSE {_MIN_VOTES} END"
    f" OR ({_ASIAN_RATING_BASE_T} AND {ASIAN_ORIGIN_CONDITION_T})"
    f" OR (t.imdb_rating IS NULL AND {_RECENT_WINDOW_T}))"
)
UNTRUSTED_RATING_CONDITION = f"NOT ({TRUSTED_RATING_CONDITION})"

# 中文就绪：写入/回填时计算。NULL（尚未回填）视为可见，避免迁移瞬间清空列表；
# 0 表示标题和简介都没有中文，从列表/统计中隐藏。
ZH_CONDITION = "COALESCE(has_zh, 1) = 1"
ZH_CONDITION_T = "COALESCE(t.has_zh, 1) = 1"
# 仅"其他平台"（无六大平台）的作品默认不展示；播放平台只在详情中展示，不再作为筛选项。
_PRIMARY_PROVIDER_SQL = ",".join(f"'{name}'" for name in PROVIDERS)
PRIMARY_PROVIDER_CONDITION = (
    "EXISTS (SELECT 1 FROM title_provider_availability primary_av"
    " WHERE primary_av.title_id = titles.id AND primary_av.is_active = 1"
    f" AND primary_av.provider_name IN ({_PRIMARY_PROVIDER_SQL}))"
)
PRIMARY_PROVIDER_CONDITION_T = (
    "EXISTS (SELECT 1 FROM title_provider_availability primary_av"
    " WHERE primary_av.title_id = t.id AND primary_av.is_active = 1"
    f" AND primary_av.provider_name IN ({_PRIMARY_PROVIDER_SQL}))"
)
DEFAULT_VISIBILITY_CONDITION = (
    f"{TRUSTED_RATING_CONDITION} AND {ZH_CONDITION}"
    f" AND ({PRIMARY_PROVIDER_CONDITION} OR {ASIAN_ORIGIN_CONDITION})"
)
DEFAULT_VISIBILITY_CONDITION_T = (
    f"{TRUSTED_RATING_CONDITION_T} AND {ZH_CONDITION_T}"
    f" AND ({PRIMARY_PROVIDER_CONDITION_T} OR {ASIAN_ORIGIN_CONDITION_T})"
)


def get_db_connection():
    db_dir = os.path.dirname(DATABASE_URL)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    last_error = None
    for attempt in range(4):
        conn = sqlite3.connect(DATABASE_URL, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            # busy_timeout 必须最先设置：部分 PRAGMA 在锁竞争下直接返回 SQLITE_BUSY，
            # 不走 busy handler，需要靠外层重试兜底（回填/同步与 API 并发时很关键）。
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("PRAGMA foreign_keys = ON")
            # 弱 ARM + 慢 USB 优化：扩 cache、开 mmap、临时表入内存
            conn.execute("PRAGMA cache_size = -4000")    # 4MB page cache（默认 2MB）
            conn.execute("PRAGMA mmap_size = 10485760")  # 10MB mmap，减少 read() 系统调用
            conn.execute("PRAGMA temp_store = MEMORY")
            # WAL 模式下 synchronous=NORMAL 是 SQLite 官方推荐（不丢已 commit 数据，
            # 仅 OS 崩溃时可能丢最后一个 WAL 段；exFAT 上 FULL 反而引入大量 fsync 浪费 I/O）
            conn.execute("PRAGMA synchronous = NORMAL")
            return conn
        except sqlite3.OperationalError as exc:
            conn.close()
            last_error = exc
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            time.sleep(0.25 * (attempt + 1))
    raise last_error


@contextmanager
def get_db():
    """上下文管理器：自动 commit/rollback/close，推荐用于简单读写场景。"""
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
