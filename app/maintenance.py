"""数据库维护命令：清理无界增长的同步日志、pending 队列与 availability 行。

用法（容器或本机）：
    python -m app.maintenance prune-errors [--errors-days 30] [--runs-days 90]
    python -m app.maintenance prune-pending [--days 180] [--dry-run]
    python -m app.maintenance prune-availability [--keep-regions CN,TW,...] [--dry-run]
    python -m app.maintenance vacuum          # 停服务执行；USB 盘上需预留一倍空间

prune-availability 只删除 "others" 渠道中不在配置地区的行：六大平台行一律保留，
可见性依赖"任一地区存在主流平台"，删除 others 不会让作品消失。执行前后会各跑一次
count_titles() 并要求相等。
"""

import argparse
import logging
from datetime import datetime, timedelta, timezone

from app.config import ALL_PROVIDER_WATCH_REGIONS, DATABASE_URL, PROVIDER_REGIONS
from app.db import count_titles, get_db_connection, init_db

logger = logging.getLogger("maintenance")


def _cutoff(days):
    return (datetime.now(timezone.utc) - timedelta(days=max(int(days), 0))).isoformat()


def prune_errors(errors_days=30, runs_days=90):
    init_db()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sync_errors WHERE created_at < ?", (_cutoff(errors_days),))
        errors_removed = cursor.rowcount
        # 只删除已结束的 sync_runs，run 进行中不被清理
        cursor.execute(
            "DELETE FROM sync_runs WHERE status != 'running' AND started_at < ?",
            (_cutoff(runs_days),),
        )
        runs_removed = cursor.rowcount
        conn.commit()
        stats = {"sync_errors_removed": errors_removed, "sync_runs_removed": runs_removed}
        logger.info("prune-errors finished: %s", stats)
        return stats
    finally:
        conn.close()


def prune_pending(days=180, dry_run=False):
    init_db()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cutoff = _cutoff(days)
        cursor.execute(
            """
            SELECT COUNT(*) FROM pending_titles
            WHERE reason = 'low_rating' AND updated_at < ?
            """,
            (cutoff,),
        )
        candidates = cursor.fetchone()[0]
        if dry_run:
            logger.info("prune-pending dry-run: %s candidates", candidates)
            return {"candidates": candidates, "removed": 0}
        cursor.execute(
            "DELETE FROM pending_titles WHERE reason = 'low_rating' AND updated_at < ?",
            (cutoff,),
        )
        removed = cursor.rowcount
        conn.commit()
        stats = {"candidates": candidates, "removed": removed}
        logger.info("prune-pending finished: %s", stats)
        return stats
    finally:
        conn.close()


def _configured_regions(extra_regions=None):
    regions = set()
    for values in PROVIDER_REGIONS.values():
        regions.update(code.upper() for code in values)
    regions.update(code.upper() for code in ALL_PROVIDER_WATCH_REGIONS)
    for code in extra_regions or []:
        code = str(code or "").strip().upper()
        if code:
            regions.add(code)
    return sorted(regions)


def prune_availability(keep_regions=None, dry_run=False):
    init_db()
    regions = _configured_regions(keep_regions)
    if not regions:
        raise RuntimeError("keep-regions is empty; refusing to delete availability rows")
    placeholders = ",".join("?" for _ in regions)
    before = count_titles()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        where = f"""
            provider_name = 'others'
            AND region != ''
            AND region NOT IN ({placeholders})
        """
        cursor.execute(f"SELECT COUNT(*) FROM title_provider_availability WHERE {where}", regions)
        candidates = cursor.fetchone()[0]
        if dry_run:
            logger.info("prune-availability dry-run: %s candidates, keep=%s", candidates, regions)
            return {"candidates": candidates, "removed": 0, "kept_regions": regions}
        cursor.execute(f"DELETE FROM title_provider_availability WHERE {where}", regions)
        removed = cursor.rowcount
        conn.commit()
    finally:
        conn.close()
    after = count_titles()
    if before != after:
        raise RuntimeError(
            f"visibility changed after prune: {before} -> {after}; investigate before proceeding"
        )
    stats = {"candidates": candidates, "removed": removed, "kept_regions": regions}
    logger.info("prune-availability finished: %s", stats)
    return stats


def vacuum():
    """回收数据库文件空间；建议停服务后执行（SQLite VACUUM 需要独占锁）。"""
    init_db()
    conn = get_db_connection()
    try:
        conn.execute("VACUUM")
        logger.info("vacuum finished: %s", DATABASE_URL)
        return {"database": DATABASE_URL, "vacuumed": True}
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Streaming tracker database maintenance")
    sub = parser.add_subparsers(dest="command", required=True)

    errors = sub.add_parser("prune-errors", help="清理同步错误/运行日志")
    errors.add_argument("--errors-days", type=int, default=30)
    errors.add_argument("--runs-days", type=int, default=90)

    pending = sub.add_parser("prune-pending", help="清理超过 N 天的 low_rating 待处理队列")
    pending.add_argument("--days", type=int, default=180)
    pending.add_argument("--dry-run", action="store_true")

    availability = sub.add_parser("prune-availability", help="删除 others 渠道中配置地区之外的行")
    availability.add_argument("--keep-regions", type=str, default="")
    availability.add_argument("--dry-run", action="store_true")

    sub.add_parser("vacuum", help="回收数据库空间（需停服务）")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "prune-errors":
        prune_errors(errors_days=args.errors_days, runs_days=args.runs_days)
    elif args.command == "prune-pending":
        prune_pending(days=args.days, dry_run=args.dry_run)
    elif args.command == "prune-availability":
        extra = [code for code in args.keep_regions.split(",") if code.strip()]
        prune_availability(keep_regions=extra, dry_run=args.dry_run)
    elif args.command == "vacuum":
        vacuum()


if __name__ == "__main__":
    main()
