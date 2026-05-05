"""
初始化数据：全平台抓取 → IMDb评分 → 只留 >=7.0
"""
import asyncio
import sys
sys.path.insert(0, '.')
from app.config import TMDB_API_KEY
from app.database import init_db, insert_title
from app.fetcher import fetch_all_providers


async def main():
    if not TMDB_API_KEY:
        print("请先配置 TMDB_API_KEY")
        return

    print("全平台抓取中...\n")
    init_db()
    titles = await fetch_all_providers()

    print(f"\n写入数据库...")
    for t in titles:
        try:
            insert_title(t)
        except Exception as e:
            print(f"  跳过 {t['title']}: {e}")

    print(f"\n完成！共 {len(titles)} 部 IMDb>=7.0 作品入库")


if __name__ == "__main__":
    asyncio.run(main())
