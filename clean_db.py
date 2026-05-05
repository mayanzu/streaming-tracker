"""
清理数据库：删除 IMDb<7.0 或超过2年的作品
"""
import sqlite3
import os
import sys
sys.path.insert(0, '.')
from datetime import datetime, timedelta
from app.config import DATABASE_URL


def clean_db():
    if not os.path.exists(DATABASE_URL):
        print("数据库不存在")
        return

    conn = sqlite3.connect(DATABASE_URL)
    cursor = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=1825)).strftime("%Y-%m-%d")

    cursor.execute("SELECT COUNT(*) FROM titles")
    before = cursor.fetchone()[0]

    # 统计
    cursor.execute("SELECT COUNT(*) FROM titles WHERE imdb_rating IS NULL")
    no_rating = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM titles WHERE imdb_rating < 7.0")
    low_rating = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM titles WHERE release_date < ?", (cutoff,))
    old = cursor.fetchone()[0]

    # 删除
    cursor.execute("""
        DELETE FROM titles WHERE
            imdb_rating IS NULL
            OR imdb_rating < 7.0
            OR release_date < ?
    """, (cutoff,))

    cursor.execute("""
        DELETE FROM title_providers WHERE title_id NOT IN (SELECT id FROM titles)
    """)

    cursor.execute("SELECT COUNT(*) FROM titles")
    after = cursor.fetchone()[0]

    conn.commit()
    conn.close()

    print(f"清理完成：")
    print(f"  清理前: {before} 部")
    print(f"  无IMDb: {no_rating}  低分: {low_rating}  过期: {old}")
    print(f"  删除: {before - after} 部")
    print(f"  保留: {after} 部")


if __name__ == "__main__":
    clean_db()
