"""中文资料回填：无中文片名与中英混排简介，按配置的翻译源重译。

用法（容器或本机）：
    python -m app.backfill_translations --titles --overviews [--limit N] [--concurrency 4] [--dry-run]

- TRANSLATE_PROVIDER=google|deepl 时走对应翻译源；none 时不翻译，
  仅把 zh_quality 标为 none / 现状，前端显示原文标签；
- 先处理无中文片名（zh_quality ∈ none / overview_only），再处理
  overview_cjk_ratio < 0.3 的中英混排简介。
"""

import argparse
import asyncio
import logging

from app.db import get_db_connection, init_db
from app.db.utils import _has_chinese
from app.fetcher.common import _cjk_ratio, _looks_chinese, _zh_quality, translate_to_chinese

logger = logging.getLogger("backfill_translations")
CHUNK_SIZE = 50


def _load_title_candidates(limit=0):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        query = """
            SELECT id, title, original_title, overview, zh_quality
            FROM titles
            WHERE zh_quality IS NULL OR zh_quality IN ('none', 'overview_only')
            ORDER BY id
        """
        params = []
        if limit and limit > 0:
            query += " LIMIT ?"
            params.append(limit)
        return [dict(row) for row in cursor.execute(query, params)]
    finally:
        conn.close()


def _load_overview_candidates(limit=0):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        query = """
            SELECT id, title, original_title, overview, zh_quality
            FROM titles
            WHERE overview IS NOT NULL AND overview != ''
              AND (overview_cjk_ratio IS NULL OR overview_cjk_ratio < 0.3)
            ORDER BY id
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
            """
            UPDATE titles
            SET title = ?, overview = ?, has_zh = ?, zh_quality = ?, overview_cjk_ratio = ?
            WHERE id = ?
            """,
            updates,
        )
        conn.commit()
        return len(updates)
    finally:
        conn.close()


async def _translate_titles(candidates, concurrency):
    semaphore = asyncio.Semaphore(max(1, concurrency))
    updates = []
    stats = {"titles_total": len(candidates), "titles_changed": 0, "titles_failed": 0}

    async def process(row):
        async with semaphore:
            source = (row.get("original_title") or row.get("title") or "").strip()
            if not source:
                return None
            translated = (await translate_to_chinese(source) or "").strip()
            if translated and translated != source and _looks_chinese(translated, 0.05):
                return row | {"new_title": translated}
            return None

    results = await asyncio.gather(*(process(row) for row in candidates))
    for row, result in zip(candidates, results):
        if result is None:
            stats["titles_failed"] += 1
            continue
        new_title = result["new_title"]
        quality = _zh_quality(new_title, row.get("original_title"), row.get("overview"))
        updates.append((
            new_title, row.get("overview") or "", _has_chinese(new_title, row.get("overview")),
            quality, round(_cjk_ratio(row.get("overview")), 4), row["id"],
        ))
        stats["titles_changed"] += 1
    return updates, stats


async def _translate_overviews(candidates, concurrency):
    semaphore = asyncio.Semaphore(max(1, concurrency))
    updates = []
    stats = {"overviews_total": len(candidates), "overviews_changed": 0, "overviews_failed": 0}

    async def process(row):
        async with semaphore:
            source = (row.get("overview") or "").strip()
            if not source:
                return None
            translated = (await translate_to_chinese(source) or "").strip()
            if translated and _looks_chinese(translated) and translated != source:
                return row | {"new_overview": translated}
            return None

    results = await asyncio.gather(*(process(row) for row in candidates))
    for row, result in zip(candidates, results):
        if result is None:
            stats["overviews_failed"] += 1
            continue
        new_overview = result["new_overview"]
        quality = _zh_quality(row.get("title"), row.get("original_title"), new_overview)
        updates.append((
            row.get("title") or "", new_overview, _has_chinese(row.get("title"), new_overview),
            quality, round(_cjk_ratio(new_overview), 4), row["id"],
        ))
        stats["overviews_changed"] += 1
    return updates, stats


async def run(limit=0, concurrency=4, dry_run=False, titles=True, overviews=True):
    init_db()
    stats = {}
    if titles:
        candidates = _load_title_candidates(limit)
        logger.info("title translation candidates: %s", len(candidates))
        updates = []
        for start in range(0, len(candidates), CHUNK_SIZE):
            chunk_updates, chunk_stats = await _translate_titles(
                candidates[start:start + CHUNK_SIZE], concurrency,
            )
            updates.extend(chunk_updates)
            for key, value in chunk_stats.items():
                stats[key] = stats.get(key, 0) + value
        if not dry_run:
            _save_updates(updates)
    if overviews:
        candidates = _load_overview_candidates(limit)
        logger.info("overview translation candidates: %s", len(candidates))
        updates = []
        for start in range(0, len(candidates), CHUNK_SIZE):
            chunk_updates, chunk_stats = await _translate_overviews(
                candidates[start:start + CHUNK_SIZE], concurrency,
            )
            updates.extend(chunk_updates)
            for key, value in chunk_stats.items():
                stats[key] = stats.get(key, 0) + value
        if not dry_run:
            _save_updates(updates)
    logger.info("backfill_translations finished: %s", stats)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Re-translate titles/overviews by configured provider")
    parser.add_argument("--titles", action="store_true", help="回填无中文片名")
    parser.add_argument("--overviews", action="store_true", help="回填中英混排简介")
    parser.add_argument("--limit", type=int, default=0, help="0 = all candidates")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.titles and not args.overviews:
        args.titles = args.overviews = True

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stats = asyncio.run(run(
        limit=args.limit, concurrency=args.concurrency, dry_run=args.dry_run,
        titles=args.titles, overviews=args.overviews,
    ))
    logger.info("backfill_translations done: %s", stats)


if __name__ == "__main__":
    main()
