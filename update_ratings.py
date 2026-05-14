"""离线批量更新 IMDb 评分 + 插入数据库缺失的新片（调用 TMDB API 补全信息）"""
import asyncio
import json
from datetime import datetime, timezone

import httpx

from app.config import DATA_DIR, MIN_IMDB_RATING, MIN_IMDB_VOTES, TMDB_API_KEY, TMDB_BASE_URL
from app.database import get_db_connection, insert_title
from app.imdb_data import load_ratings

CONCUR = 30


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def build_tmdb_to_imdb():
    """从 tmdb_imdb_map.json 加载已有映射"""
    path = DATA_DIR / 'tmdb_imdb_map.json'
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            mapping = json.load(f)
        print(f'  Loaded {len(mapping):,} TMDB→IMDb mappings from map file')
        return mapping
    print('  tmdb_imdb_map.json not found, mapping will be empty')
    return {}


async def fetch_tmdb_detail(client, tmdb_id, media_type, semaphore):
    async with semaphore:
        try:
            r = await client.get(
                f'{TMDB_BASE_URL}/{media_type}/{tmdb_id}',
                params={'api_key': TMDB_API_KEY, 'language': 'zh-CN', 'append_to_response': 'external_ids'}
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None


async def insert_missing_titles(tmdb_to_imdb, ratings):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT tmdb_id FROM titles")
    existing = {row['tmdb_id'] for row in cursor.fetchall()}
    conn.close()

    all_new_ids = []
    for kind in ['movie_ids', 'tv_series_ids']:
        path = DATA_DIR / f'{kind}.json'
        if not path.exists():
            continue
        media_type = 'movie' if kind == 'movie_ids' else 'tv'
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    item = json.loads(line.strip())
                except Exception:
                    continue
                tmdb_id = item.get('id')
                if tmdb_id not in existing:
                    all_new_ids.append((tmdb_id, media_type))

    if not all_new_ids:
        print('  无需插入新片')
        return {}

    print(f'  找到 {len(all_new_ids)} 部新片，开始从 TMDB 获取详情...')
    semaphore = asyncio.Semaphore(CONCUR)
    async with httpx.AsyncClient(timeout=20) as client:
        tasks = [fetch_tmdb_detail(client, tid, mtype, semaphore) for tid, mtype in all_new_ids]
        results = await asyncio.gather(*tasks)

    inserted = 0
    new_mappings = {}
    for (tmdb_id, media_type), detail in zip(all_new_ids, results):
        if not detail:
            continue

        imdb_id = detail.get('external_ids', {}).get('imdb_id')
        if not imdb_id:
            imdb_id = tmdb_to_imdb.get(str(tmdb_id))

        if imdb_id and imdb_id.startswith('tt'):
            new_mappings[str(tmdb_id)] = imdb_id

        rating = None
        votes = None
        if imdb_id and imdb_id in ratings:
            r, v = ratings[imdb_id]
            if v >= MIN_IMDB_VOTES and r >= MIN_IMDB_RATING:
                rating = r
                votes = v
        if rating is None:
            continue

        title_data = {
            'tmdb_id': tmdb_id,
            'title': detail.get('title') or detail.get('name', ''),
            'original_title': detail.get('original_title') or detail.get('original_name', ''),
            'type': media_type,
            'overview': detail.get('overview', ''),
            'release_date': detail.get('release_date') or detail.get('first_air_date', ''),
            'poster_url': f"https://image.tmdb.org/t/p/w500{detail['poster_path']}" if detail.get('poster_path') else None,
            'imdb_rating': rating,
            'rating_source': 'imdb' if rating is not None else None,
            'rating_votes': votes,
            'added_date': '',
            'providers': [],
        }
        if title_data['title']:
            try:
                insert_title(title_data)
                inserted += 1
            except Exception:
                pass
    print(f'  新插入 {inserted} 部，获得 {len(new_mappings)} 个新映射')
    return new_mappings


def update_ratings():
    if not TMDB_API_KEY:
        print('请先配置 TMDB_API_KEY')
        return

    print('Step 1: Load IMDb ratings...')
    ratings = load_ratings()

    print('\nStep 2: Build TMDB→IMDb mapping...')
    tmdb_to_imdb = build_tmdb_to_imdb()

    print('\nStep 3: Insert missing titles...')
    new_mappings = asyncio.run(insert_missing_titles(tmdb_to_imdb, ratings))

    print('\nStep 4: Update existing titles...')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, tmdb_id, type FROM titles")
    titles = cursor.fetchall()
    total = len(titles)

    imdb_ok = cleared = no_map = 0
    now = utc_now()

    for i, row in enumerate(titles, 1):
        title_id, tmdb_id, _ = row['id'], row['tmdb_id'], row['type']
        imdb_id = tmdb_to_imdb.get(str(tmdb_id)) or new_mappings.get(str(tmdb_id))

        new_rating = None
        new_source = None
        new_votes = None
        if imdb_id:
            r, v = ratings.get(imdb_id, (None, 0))
            if r is not None and v >= MIN_IMDB_VOTES and r >= MIN_IMDB_RATING:
                new_rating = r
                new_source = 'imdb'
                new_votes = v
                imdb_ok += 1
            else:
                cleared += 1
        else:
            no_map += 1

        cursor.execute(
            """
            UPDATE titles
            SET imdb_rating = ?, rating_source = ?, rating_votes = ?, last_synced_at = ?
            WHERE id = ?
            """,
            (new_rating, new_source, new_votes, now, title_id),
        )

        if i % 500 == 0:
            conn.commit()
            print(f'  [{i}/{total}] IMDb={imdb_ok} 清除={cleared} 无映射={no_map}')

    conn.commit()
    conn.close()
    print(f'\n完成：IMDb={imdb_ok}  清除={cleared}  无TMDB→IMDb映射={no_map}')
    print(f'总计：{total}')

    if new_mappings:
        print('\nStep 5: Saving new IMDB mappings...')
        map_path = DATA_DIR / 'tmdb_imdb_map.json'
        all_mappings = dict(tmdb_to_imdb)
        all_mappings.update(new_mappings)
        with open(map_path, 'w', encoding='utf-8') as f:
            json.dump(all_mappings, f)
        print(f'  已保存 {len(all_mappings)} 个映射到 tmdb_imdb_map.json')


if __name__ == '__main__':
    update_ratings()
