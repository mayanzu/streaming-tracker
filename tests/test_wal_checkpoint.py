"""同步收尾的 WAL checkpoint：避免大事务遗留的 WAL 拖慢下次启动。

运行：python -m pytest tests/test_wal_checkpoint.py -q
要求：不依赖外部网络与真实数据库；使用独立临时库。
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "DATABASE_URL", os.path.join(tempfile.mkdtemp(prefix="streaming-test-wal-"), "bootstrap.db")
)


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    from app.db import connection, init_db

    monkeypatch.setattr(connection, "DATABASE_URL", str(tmp_path / "test.db"))
    init_db()
    yield


def test_checkpoint_wal_truncates_after_bulk_write():
    from app.db import connection
    from app.db.connection import checkpoint_wal, get_db_connection

    writer = get_db_connection()
    try:
        # 关掉自动 checkpoint 并保持连接，模拟“大事务写完、WAL 尚未回写”的状态
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        for index in range(200):
            writer.execute(
                "INSERT INTO titles (tmdb_id, title, type) VALUES (?, ?, 'movie')",
                (index + 1, "载荷" * 200),
            )
        writer.commit()
        wal_path = connection.DATABASE_URL + "-wal"
        assert os.path.getsize(wal_path) > 0

        result = checkpoint_wal("TRUNCATE")

        assert result is not None
        assert result[0] == 0, f"checkpoint busy: {result}"
        assert not os.path.exists(wal_path) or os.path.getsize(wal_path) == 0
    finally:
        writer.close()


def test_checkpoint_wal_never_raises():
    from app.db.connection import checkpoint_wal

    # 收尾 checkpoint 只是优化，任何情况下都不应让同步流程失败
    result = checkpoint_wal("PASSIVE")
    assert result is None or isinstance(result, tuple)
