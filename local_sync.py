"""本地同步入口：抓取新片、刷新评分，并可选上传数据库。"""

import argparse
import asyncio
import json
import subprocess
from datetime import datetime, timezone

from app.config import (
    DATA_DIR,
    DATABASE_URL,
    MIN_IMDB_RATING,
    MIN_IMDB_VOTES,
    ROUTER_TARGET,
    SYNC_WINDOW_DAYS,
)
from app.database import get_db_connection
from app.imdb_data import load_ratings
from app.sync import sync_new_titles

MAP_PATH = DATA_DIR / "tmdb_imdb_map.json"


def load_tmdb_map():
    if not MAP_PATH.exists():
        return {}
    with open(MAP_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def refresh_existing_ratings(force_dataset_download=False):
    """使用本地 IMDb 数据集刷新已入库标题评分。"""
    ratings = load_ratings(force=force_dataset_download)
    tmdb_to_imdb = load_tmdb_map()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, tmdb_id FROM titles")
    rows = cursor.fetchall()

    imdb_ok = cleared = no_map = 0
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        imdb_id = tmdb_to_imdb.get(str(row["tmdb_id"]))
        new_rating = None
        new_source = None
        new_votes = None

        if imdb_id:
            rating, votes = ratings.get(imdb_id, (None, 0))
            if rating is not None and votes >= MIN_IMDB_VOTES and rating >= MIN_IMDB_RATING:
                new_rating = rating
                new_source = "imdb"
                new_votes = votes
                imdb_ok += 1
            else:
                cleared += 1
        else:
            no_map += 1

        cursor.execute(
            """
            UPDATE titles
            SET imdb_rating = ?, rating_source = ?, rating_votes = ?, last_synced_at = ?
            WHERE id = ?
            """,
            (new_rating, new_source, new_votes, now, row["id"]),
        )

    conn.commit()
    conn.close()
    print(f"评分刷新完成：IMDb={imdb_ok} 清除={cleared} 无映射={no_map}")


def upload_database():
    """通过 scp 上传数据库。ROUTER_TARGET 未配置时跳过。"""
    if not ROUTER_TARGET:
        print("未配置 ROUTER_TARGET，跳过上传")
        return

    result = subprocess.run(
        ["scp", DATABASE_URL, ROUTER_TARGET],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        print(f"数据库已上传到 {ROUTER_TARGET}")
    else:
        print(f"上传失败：{result.stderr.strip()}")


async def main():
    parser = argparse.ArgumentParser(description="streaming-tracker 本地同步脚本")
    parser.add_argument("--days-back", type=int, default=30, help="抓取最近 N 天内容")
    parser.add_argument("--max-pages", type=int, default=5, help="每类内容最多抓取页数")
    parser.add_argument(
        "--window-days",
        type=int,
        default=SYNC_WINDOW_DAYS,
        help="把抓取时间范围拆成 N 天一个窗口，避免长时间范围被 TMDB 分页截断",
    )
    parser.add_argument("--skip-fetch", action="store_true", help="跳过新片抓取")
    parser.add_argument("--skip-upload", action="store_true", help="跳过数据库上传")
    parser.add_argument("--refresh-ratings", action="store_true", help="刷新已有标题评分")
    parser.add_argument(
        "--init-ratings",
        action="store_true",
        help="强制重新下载 IMDb 数据集并刷新已有标题评分",
    )
    args = parser.parse_args()

    if args.refresh_ratings or args.init_ratings:
        refresh_existing_ratings(force_dataset_download=args.init_ratings)

    if not args.skip_fetch:
        result = await sync_new_titles(
            days_back=args.days_back,
            max_pages=args.max_pages,
            window_days=args.window_days,
        )
        print(
            f"同步完成：发现 {result.get('discovered', 0)} 部，"
            f"入库/更新 {result['processed']} 部，跳过 {result['skipped']} 部"
        )

    if not args.skip_upload:
        upload_database()


if __name__ == "__main__":
    asyncio.run(main())
