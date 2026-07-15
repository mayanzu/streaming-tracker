import argparse
import asyncio
from datetime import datetime, timezone

import httpx

from app.config import TMDB_API_KEY
from app.database import (
    get_titles_missing_countries,
    init_db,
    persist_title_countries,
)
from app.fetcher import _origin_countries_from_details, fetch_tmdb


async def fetch_country(item, client, semaphore):
    media_type = "movie" if item["type"] == "movie" else "tv"
    endpoint = f"/{media_type}/{item['tmdb_id']}"
    try:
        async with semaphore:
            details = await fetch_tmdb(endpoint, client=client)
        return {
            "id": item["id"],
            "origin_countries": _origin_countries_from_details(details),
            "countries_synced_at": datetime.now(timezone.utc).isoformat(),
        }, None
    except Exception as exc:
        return None, (
            f"id={item['id']} tmdb_id={item['tmdb_id']} type={item['type']} "
            f"title={item['title']!r}: {type(exc).__name__}: {exc}"
        )


async def backfill(limit=0, concurrency=8, batch_size=50):
    if not TMDB_API_KEY:
        raise RuntimeError("TMDB_API_KEY 未配置，无法回填地区")

    init_db()
    items = get_titles_missing_countries(limit=limit)
    total = len(items)
    if not total:
        print("没有需要回填地区的作品", flush=True)
        return 0

    print(f"开始回填 {total} 部作品的原产地区", flush=True)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    succeeded = 0
    failed = 0
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for offset in range(0, total, max(1, batch_size)):
            batch = items[offset:offset + max(1, batch_size)]
            results = await asyncio.gather(*(
                fetch_country(item, client, semaphore) for item in batch
            ))
            completed = [result for result, error in results if result]
            errors = [error for result, error in results if error]
            if completed:
                persist_title_countries(completed)
                succeeded += len(completed)
            failed += len(errors)
            for error in errors:
                print(f"回填失败：{error}", flush=True)
            processed = min(offset + len(batch), total)
            print(
                f"进度 {processed}/{total}，成功 {succeeded}，失败 {failed}",
                flush=True,
            )

    print(f"地区回填完成：成功 {succeeded}，失败 {failed}", flush=True)
    return failed


def parse_args():
    parser = argparse.ArgumentParser(description="回填作品的 TMDB 原产国家/地区")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条，0 表示全部")
    parser.add_argument("--concurrency", type=int, default=8, help="TMDB 请求并发数")
    parser.add_argument("--batch-size", type=int, default=50, help="每批持久化条数")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    failures = asyncio.run(backfill(
        limit=max(0, arguments.limit),
        concurrency=max(1, arguments.concurrency),
        batch_size=max(1, arguments.batch_size),
    ))
    raise SystemExit(1 if failures else 0)
