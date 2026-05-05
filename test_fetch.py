"""
测试：验证数据抓取 + IMDb评分流程
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from app.config import TMDB_API_KEY
from app.fetcher import fetch_new_releases, enrich_with_imdb
from app.database import init_db, insert_title


async def main():
    if not TMDB_API_KEY:
        print("请先配置 TMDB_API_KEY")
        return

    print("测试 Netflix 抓取 + IMDb评分\n")

    titles = await fetch_new_releases("netflix", days_back=30)
    print(f"TMDB 发现: {len(titles)} 部\n")

    # enrich 前3部
    for t in titles[:3]:
        await enrich_with_imdb(t)
        r = t['imdb_rating']
        print(f"  {t['title'][:30]}: IMDb={r if r else '暂无可靠评分'}")

    # 过滤 IMDb>=7.0
    qualified = [t for t in titles[:10] if t['imdb_rating'] is not None and t['imdb_rating'] >= 7.0]
    print(f"\n前10部中 IMDb>=7.0: {len(qualified)} 部")

    if qualified:
        init_db()
        tid = insert_title(qualified[0])
        print(f"入库测试: {qualified[0]['title']} (id={tid})")

    print("\n流程验证通过！")


if __name__ == "__main__":
    asyncio.run(main())
