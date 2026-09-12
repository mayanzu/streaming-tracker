"""zh_quality / overview_cjk_ratio 回填：对存量作品重新判定中文资料质量四态。

用法（容器或本机）：
    python -m app.backfill_zh_quality [--limit N] [--dry-run]

只处理 zh_quality IS NULL 的行；判定与 enrich 时一致：
full / machine（无法追溯机翻，此处按文本判定）/ overview_only / title_only / none。
"""

import argparse
import logging

from app.db import get_db_connection, init_db
from app.fetcher.common import _cjk_ratio, _zh_quality

logger = logging.getLogger("backfill_zh_quality")


def _load_candidates(limit=0):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        query = """
            SELECT id, title, original_title, overview
            FROM titles
            WHERE zh_quality IS NULL
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
            "UPDATE titles SET zh_quality = ?, overview_cjk_ratio = ? WHERE id = ?",
            updates,
        )
        conn.commit()
        return len(updates)
    finally:
        conn.close()


def run(limit=0, dry_run=False):
    init_db()
    candidates = _load_candidates(limit)
    logger.info("zh_quality candidates: %s", len(candidates))
    stats = {"total": len(candidates), "full": 0, "overview_only": 0,
             "title_only": 0, "none": 0}
    updates = []
    for row in candidates:
        quality = _zh_quality(row.get("title"), row.get("original_title"), row.get("overview"))
        ratio = round(_cjk_ratio(row.get("overview")), 4)
        stats[quality] = stats.get(quality, 0) + 1
        updates.append((quality, ratio, row["id"]))
    if not dry_run:
        _save_updates(updates)
    logger.info("zh_quality finished: %s", stats)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill zh_quality / overview_cjk_ratio")
    parser.add_argument("--limit", type=int, default=0, help="0 = all candidates")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stats = run(limit=args.limit, dry_run=args.dry_run)
    logger.info("backfill_zh_quality done: %s", stats)


if __name__ == "__main__":
    main()
