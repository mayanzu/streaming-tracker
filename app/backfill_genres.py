"""题材归一化回填：把存量 genres_json 中的英文/混合题材名改写为中文规范值。

用法（容器或本机）：
    python -m app.backfill_genres [--limit N] [--dry-run]

示例：["Sci-Fi & Fantasy"] → ["科幻", "奇幻"]；["动作冒险"] → ["动作", "冒险"]。
与 enrich / insert_title 使用同一张 alias 表（app.genres），保证新老数据一致。
"""

import argparse
import json
import logging

from app.db import get_db_connection, init_db
from app.genres import normalize_genres

logger = logging.getLogger("backfill_genres")


def _load_candidates(limit=0):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        query = """
            SELECT id, genres_json
            FROM titles
            WHERE genres_json IS NOT NULL
              AND genres_json != ''
              AND genres_json != '[]'
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
        cursor.executemany("UPDATE titles SET genres_json = ? WHERE id = ?", updates)
        conn.commit()
        return len(updates)
    finally:
        conn.close()


def run(limit=0, dry_run=False):
    init_db()
    candidates = _load_candidates(limit)
    logger.info("genre candidates: %s", len(candidates))
    updates = []
    changed = 0
    for row in candidates:
        try:
            parsed = json.loads(row.get("genres_json") or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, list):
            continue
        normalized = normalize_genres(parsed)
        if not normalized or normalized == parsed:
            continue
        updates.append((json.dumps(normalized, ensure_ascii=False), row["id"]))
        changed += 1
    if not dry_run:
        _save_updates(updates)
    stats = {"total": len(candidates), "changed": changed}
    logger.info("backfill_genres finished: %s", stats)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Normalize genres_json to Chinese canonical names")
    parser.add_argument("--limit", type=int, default=0, help="0 = all rows")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stats = run(limit=args.limit, dry_run=args.dry_run)
    logger.info("backfill_genres done: %s", stats)


if __name__ == "__main__":
    main()
