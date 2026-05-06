"""
一次性补充所有缺失 IMDB ID 并用 IMDB 数据集更新评分
"""
import asyncio
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.config import TMDB_API_KEY, TMDB_BASE_URL

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DB_PATH = os.path.join(DATA_DIR, 'tracker.db')
MAP_PATH = os.path.join(DATA_DIR, 'tmdb_imdb_map.json')
RATINGS_PATH = os.path.join(DATA_DIR, 'title.ratings.tsv')

TMDB_CONCUR = 30


def load_imdb_ratings():
    ratings = {}
    with open(RATINGS_PATH, 'r', encoding='utf-8') as f:
        next(f)
        for line in f:
            parts = line.strip().split('\t')
            if len(parts) >= 3:
                ratings[parts[0]] = (float(parts[1]), int(parts[2]))
    print(f"已加载 {len(ratings):,} 条 IMDB 评分")
    return ratings


def load_tmdb_map():
    with open(MAP_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_tmdb_map(mapping):
    with open(MAP_PATH, 'w', encoding='utf-8') as f:
        json.dump(mapping, f)


async def fetch_external_ids(client, tmdb_id, media_type, semaphore):
    async with semaphore:
        for attempt in range(3):
            try:
                r = await client.get(
                    f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}",
                    params={'api_key': TMDB_API_KEY, 'append_to_response': 'external_ids'}
                )
                if r.status_code == 200:
                    data = r.json()
                    imdb_id = data.get('external_ids', {}).get('imdb_id')
                    return tmdb_id, imdb_id
            except Exception:
                if attempt == 2:
                    return tmdb_id, None
                await asyncio.sleep(1)
        return tmdb_id, None


async def fetch_all_missing_imdb_ids(tmdb_ids_with_type):
    """批量获取缺失的 IMDB ID"""
    import httpx
    print(f"  需获取 {len(tmdb_ids_with_type)} 个 IMDB ID...")
    semaphore = asyncio.Semaphore(TMDB_CONCUR)
    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [
            fetch_external_ids(client, tmdb_id, media_type, semaphore)
            for tmdb_id, media_type in tmdb_ids_with_type
        ]
        results = await asyncio.gather(*tasks)

    tmdb_map = load_tmdb_map()
    new_count = 0
    for tmdb_id, imdb_id in results:
        if imdb_id and imdb_id.startswith('tt'):
            tmdb_map[str(tmdb_id)] = imdb_id
            new_count += 1

    save_tmdb_map(tmdb_map)
    print(f"  获取了 {new_count} 个新 IMDB ID")
    return new_count


def update_all_ratings(imdb_ratings):
    """用 IMDB 数据集批量更新所有标题的评分"""
    tmdb_map = load_tmdb_map()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT id, tmdb_id FROM titles")
    titles = cursor.fetchall()
    conn.close()

    ok = cleared = no_map = 0
    total = len(titles)
    for i, (title_id, tmdb_id) in enumerate(titles, 1):
        imdb_id = tmdb_map.get(str(tmdb_id))
        new_rating = None

        if imdb_id and imdb_id in imdb_ratings:
            r, v = imdb_ratings[imdb_id]
            if v >= 50 and r >= 7.0:
                new_rating = r
                ok += 1
            else:
                cleared += 1
        else:
            no_map += 1

        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE titles SET imdb_rating = ? WHERE id = ?", (new_rating, title_id))
        conn.commit()
        conn.close()

        if i % 300 == 0:
            print(f"    [{i}/{total}] ok={ok} cleared={cleared} no_map={no_map}")

    print(f"  更新完成: ok={ok} cleared={cleared} no_map={no_map}")
    return ok, cleared, no_map


def upload_to_router():
    import subprocess
    result = subprocess.run(
        ['wsl.exe', '--', 'bash', '-c',
         'sshpass -p password scp -o StrictHostKeyChecking=no data/tracker.db root@192.168.31.3:/app/data/tracker.db'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"\n  数据库已上传到路由器")
    else:
        print(f"\n  上传失败: {result.stderr}")


async def main():
    # Step 1: 加载 IMDB 数据集
    print("=" * 50)
    print("Step 1: 加载 IMDB 数据集")
    print("=" * 50)
    imdb_ratings = load_imdb_ratings()

    # Step 2: 找出缺失 IMDB ID 的标题
    print("\n" + "=" * 50)
    print("Step 2: 查找缺失 IMDB ID 的标题")
    print("=" * 50)
    tmdb_map = load_tmdb_map()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT id, tmdb_id, type FROM titles")
    titles = cursor.fetchall()
    conn.close()

    missing = []
    for title_id, tmdb_id, title_type in titles:
        if str(tmdb_id) not in tmdb_map:
            missing.append((tmdb_id, title_type))

    print(f"  缺失 IMDB ID 的标题: {len(missing)} 个")
    if not missing:
        print("  所有标题都已有关联 IMDB ID")
        missing_ids_count = 0
    else:
        # Step 3: 批量获取缺失的 IMDB ID
        print("\n" + "=" * 50)
        print("Step 3: 获取缺失的 IMDB ID")
        print("=" * 50)
        missing_ids_count = await fetch_all_missing_imdb_ids(missing)

    # Step 4: 用 IMDB 数据集更新所有评分
    print("\n" + "=" * 50)
    print("Step 4: 用 IMDB 数据集更新所有评分")
    print("=" * 50)
    update_all_ratings(imdb_ratings)

    # Step 5: 上传
    print("\n" + "=" * 50)
    print("Step 5: 上传数据库到路由器")
    print("=" * 50)
    upload_to_router()

    print("\n" + "=" * 50)
    print("全部完成！")
    print("=" * 50)


if __name__ == '__main__':
    asyncio.run(main())