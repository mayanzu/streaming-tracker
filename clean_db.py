"""
清理数据库：删除 IMDb<7.0 或超过指定天数的作品
"""
import os
from datetime import datetime, timedelta

from app.config import DATABASE_URL, MIN_IMDB_RATING, SYNC_BOOTSTRAP_DAYS_BACK
from app.database import get_db


# 清理超过此天数的老作品，默认与 bootstrap 范围一致
CLEANUP_DAYS_BACK = int(os.getenv("CLEANUP_DAYS_BACK", str(SYNC_BOOTSTRAP_DAYS_BACK)))


def clean_db():
    if not os.path.exists(DATABASE_URL):
        print("数据库不存在")
        return

    cutoff = (datetime.now() - timedelta(days=CLEANUP_DAYS_BACK)).strftime("%Y-%m-%d")

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM titles")
        before = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM titles WHERE imdb_rating IS NULL")
        no_rating = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM titles WHERE imdb_rating < ?", (MIN_IMDB_RATING,))
        low_rating = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM titles WHERE rating_source IS NULL OR rating_source NOT IN ('imdb', 'omdb')"
        )
        untrusted = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM titles WHERE release_date < ?", (cutoff,))
        old = cursor.fetchone()[0]

        cursor.execute("""
            DELETE FROM titles WHERE
                imdb_rating IS NULL
                OR imdb_rating < ?
                OR rating_source IS NULL
                OR rating_source NOT IN ('imdb', 'omdb')
                OR release_date < ?
        """, (MIN_IMDB_RATING, cutoff))

        cursor.execute("""
            DELETE FROM title_providers WHERE title_id NOT IN (SELECT id FROM titles)
        """)

        cursor.execute("SELECT COUNT(*) FROM titles")
        after = cursor.fetchone()[0]

    print(f"清理完成：")
    print(f"  清理前: {before} 部")
    print(f"  无IMDb: {no_rating}  低分: {low_rating}  非IMDb来源: {untrusted}  过期: {old}")
    print(f"  删除: {before - after} 部")
    print(f"  保留: {after} 部")


if __name__ == "__main__":
    clean_db()
