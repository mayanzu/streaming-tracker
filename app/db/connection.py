"""SQLite 连接管理与全局查询条件常量。"""

import os
import sqlite3
from contextlib import contextmanager

from app.config import DATABASE_URL

TRUSTED_RATING_SOURCES = ("imdb", "omdb")
TRUSTED_RATING_CONDITION = "imdb_rating IS NOT NULL AND rating_source IN ('imdb', 'omdb')"
TRUSTED_RATING_CONDITION_T = (
    "t.imdb_rating IS NOT NULL AND t.rating_source IN ('imdb', 'omdb')"
)
UNTRUSTED_RATING_CONDITION = (
    "imdb_rating IS NULL OR rating_source IS NULL OR rating_source NOT IN ('imdb', 'omdb')"
)


def get_db_connection():
    db_dir = os.path.dirname(DATABASE_URL)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DATABASE_URL)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    # 弱 ARM + 慢 USB 优化：扩 cache、开 mmap、临时表入内存
    conn.execute("PRAGMA cache_size = -4000")    # 4MB page cache（默认 2MB）
    conn.execute("PRAGMA mmap_size = 10485760")  # 10MB mmap，减少 read() 系统调用
    conn.execute("PRAGMA temp_store = MEMORY")
    # WAL 模式下 synchronous=NORMAL 是 SQLite 官方推荐（不丢已 commit 数据，
    # 仅 OS 崩溃时可能丢最后一个 WAL 段；exFAT 上 FULL 反而引入大量 fsync 浪费 I/O）
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


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
