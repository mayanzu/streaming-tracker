"""存量作品中文回填：TMDB 官方译名优先，缺译名时机翻兜底，并刷新 has_zh。

用法（容器内）：
    python -m app.backfill [--limit N] [--concurrency 8] [--dry-run]

只处理 has_zh 为 NULL（未回填）或 0（无中文）的作品；已回填为 1 的跳过。
"""

import argparse
import asyncio
import logging

import httpx

from app.config import TMDB_API_KEY
from app.db import get_db_connection, init_db
from app.db.utils import _has_chinese
from app.fetcher.common import _contains_cjk, translate_to_chinese
from app.fetcher.tmdb import fetch_tmdb

logger = logging.getLogger("backfill")
CHUNK_SIZE = 100


def _load_candidates(limit=0, newest_first=False):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        order = "date(release_date) DESC, id DESC" if newest_first else "id"
        query = f"""
            SELECT id, tmdb_id, type, title, original_title, overview
            FROM titles
            WHERE has_zh IS NULL OR has_zh = 0
            ORDER BY {order}
        """
        params = []
        if limit and limit > 0:
            query += " LIMIT ?"
            params.append(limit)
        return [dict(row) for row in cursor.execute(query, params)]
    finally:
        conn.close()


def _save_updates(updates):
    if not updates:
        return 0
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.executemany(
            "UPDATE titles SET title=?, overview=?, has_zh=? WHERE id=?",
            updates,
        )
        conn.commit()
        return len(updates)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def _resolve_one(row, client, semaphore):
    async with semaphore:
        endpoint = f"/{'movie' if row['type'] == 'movie' else 'tv'}/{row['tmdb_id']}"
        details = await fetch_tmdb(endpoint, {"language": "zh-CN"}, client=client)

    tmdb_title = (details.get("title") or details.get("name") or "").strip()
    tmdb_original = (details.get("original_title") or details.get("original_name") or "").strip()
    tmdb_overview = (details.get("overview") or "").strip()
    old_title = (row.get("title") or "").strip()
    old_overview = (row.get("overview") or "").strip()

    new_title = old_title
    if tmdb_title and _contains_cjk(tmdb_title):
        new_title = tmdb_title
    elif not _contains_cjk(old_title):
        source = tmdb_original or tmdb_title or old_title
        if source:
            translated = (await translate_to_chinese(source) or "").strip()
            if translated:
                new_title = translated

    new_overview = old_overview
    if tmdb_overview and _contains_cjk(tmdb_overview):
        new_overview = tmdb_overview
    elif not _contains_cjk(old_overview):
        source = tmdb_overview or old_overview
        if source:
            translated = (await translate_to_chinese(source) or "").strip()
            if translated:
                new_overview = translated

    has_zh = _has_chinese(new_title, new_overview)
    return {
        "id": row["id"],
        "new_title": new_title,
        "new_overview": new_overview,
        "has_zh": has_zh,
        "changed": new_title != old_title or new_overview != old_overview,
    }


async def run(limit=0, concurrency=8, dry_run=False, newest_first=False):
    await asyncio.to_thread(init_db)
    candidates = await asyncio.to_thread(_load_candidates, limit, newest_first)
    total = len(candidates)
    logger.info("backfill candidates: %s", total)
    stats = {"total": total, "processed": 0, "changed": 0, "no_zh": 0, "failed": 0}
    if not total:
        return stats
    if not TMDB_API_KEY:
        raise RuntimeError("TMDB_API_KEY is required")

    semaphore = asyncio.Semaphore(max(1, concurrency))
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        for start in range(0, total, CHUNK_SIZE):
            chunk = candidates[start:start + CHUNK_SIZE]
            results = await asyncio.gather(
                *(_resolve_one(row, client, semaphore) for row in chunk),
                return_exceptions=True,
            )
            updates = []
            for row, result in zip(chunk, results):
                stats["processed"] += 1
                if isinstance(result, Exception):
                    stats["failed"] += 1
                    logger.warning(
                        "backfill failed id=%s tmdb=%s: %s",
                        row["id"], row["tmdb_id"], result,
                    )
                    continue
                if result["changed"]:
                    stats["changed"] += 1
                if not result["has_zh"]:
                    stats["no_zh"] += 1
                updates.append(
                    (result["new_title"], result["new_overview"], result["has_zh"], result["id"])
                )
            if not dry_run:
                await asyncio.to_thread(_save_updates, updates)
            logger.info(
                "progress %s/%s changed=%s no_zh=%s failed=%s",
                min(start + CHUNK_SIZE, total), total,
                stats["changed"], stats["no_zh"], stats["failed"],
            )
    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill Chinese title/overview for stored titles")
    parser.add_argument("--limit", type=int, default=0, help="0 = all candidates")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--newest-first", action="store_true", help="优先回填最新上映的作品")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    stats = asyncio.run(run(
        limit=args.limit, concurrency=args.concurrency,
        dry_run=args.dry_run, newest_first=args.newest_first,
    ))
    logger.info("backfill finished: %s", stats)


if __name__ == "__main__":
    main()
