"""存量作品观看渠道回填：用 TMDB 详情数据补齐具体平台名称与覆盖地区。

背景：历史 availability 行只有归一后的内部渠道（六大 + others），
"其他平台"看不到具体是哪个平台。本脚本逐部请求 TMDB
`/{type}/{tmdb_id}/watch/providers` 详情接口，用
`_provider_availability` 同一套解析逻辑回填 `provider_label` 与 region，
详情页自动展示具体名称（如 MUBI、爱奇艺）。

用法（容器内）：
    python -m app.backfill_channels [--limit N] [--concurrency 5] [--dry-run] [--newest-first]

核验语义（7.5）：
- 请求失败：不写库、不改动任何行；
- 请求成功：upsert 本次看到的渠道行，并把该片本次未出现的活跃行标记失效
  （只有单片核验成功后才发布否定结论）；
- 无论是否命中渠道都记录 `providers_checked_at`，空结果 30 天内不重复请求；
- 候选集同时覆盖"有未标注渠道"与"已无活跃渠道（可能被旧时钟策略误停用）"
  的作品，可分批、断点续跑。
"""

import argparse
import asyncio
import logging

import httpx

from app.config import TMDB_API_KEY
from app.db import get_db_connection, init_db
from app.db.utils import _utc_now
from app.fetcher.common import _provider_availability
from app.fetcher.tmdb import fetch_tmdb

logger = logging.getLogger("channels-backfill")
CHUNK_SIZE = 50
# 无渠道结果的重查间隔：TMDB 侧未来可能上架，避免每次全量重复请求
RECHECK_DAYS = 30


def _load_candidates(limit=0, newest_first=False):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        order = "date(t.release_date) DESC, t.id DESC" if newest_first else "t.id"
        query = f"""
            SELECT DISTINCT t.id, t.tmdb_id, t.type, t.title
            FROM titles t
            WHERE (t.providers_checked_at IS NULL
                   OR t.providers_checked_at < datetime('now', '-{RECHECK_DAYS} days'))
              AND (
                  EXISTS (
                      SELECT 1 FROM title_provider_availability a
                      WHERE a.title_id = t.id AND a.is_active = 1
                        AND (a.provider_label IS NULL OR a.provider_label = '')
                  )
                  OR NOT EXISTS (
                      SELECT 1 FROM title_provider_availability a
                      WHERE a.title_id = t.id AND a.is_active = 1
                  )
              )
            ORDER BY {order}
        """
        params = []
        if limit and limit > 0:
            query += " LIMIT ?"
            params.append(limit)
        return [dict(row) for row in cursor.execute(query, params)]
    finally:
        conn.close()


def _save_channels(title_id, providers, regions, labels, observed_at):
    """单片核验成功后写入渠道：upsert 本次命中行，并把未出现的活跃行标记失效。
    空结果同样会停用该片所有活跃渠道并记录检查时间（30 天内不重复请求）。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        touched = 0
        seen = []
        for provider in providers:
            cursor.execute(
                "INSERT OR IGNORE INTO title_providers (title_id, provider_name) VALUES (?,?)",
                (title_id, provider),
            )
            label = " / ".join(dict.fromkeys(labels.get(provider) or []))
            for region in regions.get(provider) or [""]:
                cursor.execute("""
                    INSERT INTO title_provider_availability
                        (title_id, provider_name, region, monetization_type,
                         first_seen_at, last_seen_at, is_active, provider_label)
                    VALUES (?, ?, ?, 'mixed', ?, ?, 1, ?)
                    ON CONFLICT(title_id, provider_name, region, monetization_type)
                    DO UPDATE SET last_seen_at=excluded.last_seen_at, is_active=1,
                        provider_label=COALESCE(NULLIF(excluded.provider_label, ''),
                            title_provider_availability.provider_label)
                """, (title_id, provider, region, observed_at, observed_at, label))
                touched += 1
                seen.append((provider, region))
            if label:
                # 顺带标注同渠道的历史行（如迁移遗留的空 region 行），使其退出候选集
                cursor.execute("""
                    UPDATE title_provider_availability
                    SET provider_label = ?
                    WHERE title_id = ? AND provider_name = ?
                      AND (provider_label IS NULL OR provider_label = '')
                """, (label, title_id, provider))
        # 本次核验未出现的活跃行失效：只有请求成功才会走到这里
        if seen:
            placeholders = ",".join("(?,?)" for _ in seen)
            flat = [value for pair in seen for value in pair]
            cursor.execute(f"""
                UPDATE title_provider_availability
                SET is_active = 0
                WHERE title_id = ? AND is_active = 1
                  AND (provider_name, region) NOT IN (VALUES {placeholders})
            """, (title_id, *flat))
        else:
            cursor.execute("""
                UPDATE title_provider_availability
                SET is_active = 0
                WHERE title_id = ? AND is_active = 1
            """, (title_id,))
        cursor.execute(
            "UPDATE titles SET providers_checked_at = ? WHERE id = ?",
            (observed_at, title_id),
        )
        conn.commit()
        return touched
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def _resolve_one(row, client, semaphore):
    async with semaphore:
        endpoint = f"/{'movie' if row['type'] == 'movie' else 'tv'}/{row['tmdb_id']}/watch/providers"
        payload = await fetch_tmdb(endpoint, {"language": "en-US"}, client=client)
    providers, regions, labels = _provider_availability(payload or {})
    return {
        "id": row["id"],
        "providers": providers,
        "regions": regions,
        "labels": labels,
    }


async def run(limit=0, concurrency=5, dry_run=False, newest_first=False):
    await asyncio.to_thread(init_db)
    candidates = await asyncio.to_thread(_load_candidates, limit, newest_first)
    total = len(candidates)
    logger.info("channels-backfill candidates: %s", total)
    stats = {"total": total, "processed": 0, "rows": 0, "empty": 0, "failed": 0}
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
            for row, result in zip(chunk, results):
                stats["processed"] += 1
                if isinstance(result, Exception):
                    # 请求失败：不写库、不改动任何行，等待下轮
                    stats["failed"] += 1
                    logger.warning(
                        "channels-backfill failed id=%s tmdb=%s: %s",
                        row["id"], row["tmdb_id"], result,
                    )
                    continue
                if not result["providers"]:
                    stats["empty"] += 1
                if not dry_run:
                    # 成功核验（含空结果）：upsert 命中行 + 停用本次未出现的行
                    stats["rows"] += await asyncio.to_thread(
                        _save_channels, result["id"], result["providers"],
                        result["regions"], result["labels"], _utc_now(),
                    )
            logger.info(
                "progress %s/%s rows=%s empty=%s failed=%s",
                min(start + CHUNK_SIZE, total), total,
                stats["rows"], stats["empty"], stats["failed"],
            )
    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill watch channel labels/regions for stored titles")
    parser.add_argument("--limit", type=int, default=0, help="0 = all candidates")
    parser.add_argument("--concurrency", type=int, default=5)
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
    logger.info("channels-backfill finished: %s", stats)


if __name__ == "__main__":
    main()
