"""
刷新评分：OMDB → IMDb抓取 → TMDB大样本兜底
"""
import asyncio
import sys
sys.path.insert(0, '.')
from app.database import get_db_connection
from app.fetcher import fetch_tmdb, get_trusted_rating

MIN_RATING = 7.0
CONCURRENCY = 15


async def update_ratings():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, tmdb_id, type FROM titles")
    titles = cursor.fetchall()
    total = len(titles)
    conn.close()

    print(f"共 {total} 部作品\n")

    sem = asyncio.Semaphore(CONCURRENCY)
    stats = {'imdb': 0, 'tmdb': 0, 'clear': 0, 'done': 0}
    lock = asyncio.Lock()

    async def process(row):
        title_id, tmdb_id, title_type = row['id'], row['tmdb_id'], row['type']
        try:
            endpoint = f"/{'movie' if title_type == 'movie' else 'tv'}/{tmdb_id}"
            details = await fetch_tmdb(endpoint, {"append_to_response": "external_ids"})
            imdb_id = details.get('external_ids', {}).get('imdb_id')

            rating, votes, source = await get_trusted_rating(
                imdb_id,
                details.get('vote_average', 0),
                details.get('vote_count', 0)
            )

            async with lock:
                if rating is not None and rating >= MIN_RATING:
                    c = get_db_connection()
                    c.execute("UPDATE titles SET imdb_rating = ? WHERE id = ?", (rating, title_id))
                    c.commit(); c.close()
                    if source == 'imdb':
                        stats['imdb'] += 1
                    else:
                        stats['tmdb'] += 1
                else:
                    c = get_db_connection()
                    c.execute("UPDATE titles SET imdb_rating = NULL WHERE id = ?", (title_id,))
                    c.commit(); c.close()
                    stats['clear'] += 1
                stats['done'] += 1
                if stats['done'] % 30 == 0:
                    print(f"  [{stats['done']}/{total}] IMDb={stats['imdb']} TMDB={stats['tmdb']} 清除={stats['clear']}")
        except Exception:
            async with lock:
                stats['done'] += 1

    async def with_sem(row):
        async with sem:
            await process(row)

    await asyncio.gather(*[with_sem(row) for row in titles])

    print(f"\n完成：IMDb源={stats['imdb']}  TMDB源={stats['tmdb']}  清除低分={stats['clear']}")


if __name__ == "__main__":
    asyncio.run(update_ratings())
