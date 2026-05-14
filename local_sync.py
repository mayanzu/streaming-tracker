"""本地同步入口：抓取新片，并可选上传数据库。"""

import argparse
import asyncio
import subprocess

from app.config import (
    DATABASE_URL,
    ROUTER_TARGET,
    SYNC_BOOTSTRAP_DAYS_BACK,
    SYNC_BOOTSTRAP_MAX_PAGES,
    SYNC_BOOTSTRAP_WINDOW_DAYS,
)
from app.sync import sync_new_titles


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
    parser.add_argument(
        "--days-back",
        type=int,
        default=SYNC_BOOTSTRAP_DAYS_BACK,
        help="抓取最近 N 天内容",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=SYNC_BOOTSTRAP_MAX_PAGES,
        help="每类内容最多抓取页数",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=SYNC_BOOTSTRAP_WINDOW_DAYS,
        help="把抓取时间范围拆成 N 天一个窗口，避免长时间范围被 TMDB 分页截断",
    )
    parser.add_argument("--skip-fetch", action="store_true", help="跳过新片抓取")
    parser.add_argument("--skip-upload", action="store_true", help="跳过数据库上传")
    args = parser.parse_args()

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
