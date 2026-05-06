"""
本地同步脚本
功能：
  首次: 下载 IMDB 官方评分数据集，更新所有已有标题评分
  每日: TMDB Discover 新片 → OMDB API 评分（>=7.0 才入库）→ 上传数据库到软路由

用法:
  python local_sync.py --init           # 首次初始化（下载IMDB数据集 + 更新所有标题评分）
  python local_sync.py                   # 每日同步（TMDB discover + OMDB 评分 + 上传）
  python local_sync.py --omdb-only       # 仅用 OMDB 为新片评分（跳过 discover）
"""
import argparse
import asyncio
import gzip
import os
import sqlite3
import json
import sys
import urllib.request
from datetime import datetime, timedelta

# 加载项目配置
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.config import (
    TMDB_API_KEY, TMDB_BASE_URL, OMDB_API_KEY, OMDB_BASE_URL, PROVIDERS
)

# 路径配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'tracker.db')
MAP_PATH = os.path.join(DATA_DIR, 'tmdb_imdb_map.json')
RATINGS_PATH = os.path.join(DATA_DIR, 'title.ratings.tsv')

# IMDB 数据集
IMDB_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"

# 评分阈值
IMDB_MIN_VOTES = 50
IMDB_MIN_RATING = 7.0
OMDB_MIN_VOTES = 100
OMDB_MIN_RATING = 7.0

# TMDB 并发
TMDB_CONCUR = 20
OMDB_CONCUR = 10

# 路由器配置
ROUTER_HOST = "192.168.31.3"
ROUTER_USER = "root"
ROUTER_PASS = "password"
ROUTER_DB_PATH = "/app/data/tracker.db"


# ===================== IMDB 数据集 =====================

def download_imdb_ratings(force=False):
    """下载并缓存 IMDB 评分数据集"""
    if os.path.exists(RATINGS_PATH) and not force:
        print(f"  IMDB 数据集已存在: {RATINGS_PATH}")
        return

    print("  下载 IMDB 数据集...")
    gz_path = RATINGS_PATH + ".gz"
    try:
        urllib.request.urlretrieve(IMDB_URL, gz_path)
        with gzip.open(gz_path, 'rb') as f_in:
            with open(RATINGS_PATH, 'wb') as f_out:
                f_out.write(f_in.read())
        os.remove(gz_path)
        print(f"  IMDB 数据集已保存: {RATINGS_PATH}")
    except Exception as e:
        print(f"  下载 IMDB 数据集失败: {e}")
        if os.path.exists(gz_path):
            os.remove(gz_path)


def load_imdb_ratings():
    """加载 IMDB 评分到内存"""
    if not os.path.exists(RATINGS_PATH):
        print("  警告: IMDB 数据集不存在")
        return {}
    ratings = {}
    with open(RATINGS_PATH, 'r', encoding='utf-8') as f:
        next(f)  # 跳过表头
        for line in f:
            parts = line.strip().split('\t')
            if len(parts) >= 3:
                ratings[parts[0]] = (float(parts[1]), int(parts[2]))
    print(f"  已加载 {len(ratings):,} 条 IMDB 评分")
    return ratings


# ===================== TMDB API =====================

async def fetch_tmdb(client, endpoint, params=None, retries=2):
    params = params or {}
    params['api_key'] = TMDB_API_KEY
    params.setdefault('language', 'zh-CN')
    for attempt in range(retries + 1):
        try:
            r = await client.get(f"{TMDB_BASE_URL}{endpoint}", params=params)
            if r.status_code == 200:
                return r.json()
        except Exception:
            if attempt == retries:
                return None
            await asyncio.sleep(1)
    return None


async def fetch_external_ids(client, tmdb_id, media_type, semaphore):
    """获取单个标题的 IMDB ID"""
    async with semaphore:
        data = await fetch_tmdb(client, f"/{media_type}/{tmdb_id}", {"append_to_response": "external_ids"})
        if data:
            return tmdb_id, media_type, data.get('external_ids', {}).get('imdb_id')
        return tmdb_id, media_type, None


# ===================== OMDB API =====================

async def fetch_omdb(client, imdb_id, semaphore):
    """获取单个标题的 OMDB 评分"""
    async with semaphore:
        try:
            r = await client.get(
                OMDB_BASE_URL,
                params={"i": imdb_id, "apikey": OMDB_API_KEY},
                timeout=10
            )
            data = r.json()
            if data.get('Response') == 'True':
                rating_str = data.get('imdbRating')
                votes_str = data.get('imdbVotes', '0')
                if rating_str not in (None, 'N/A'):
                    rating = float(rating_str)
                    votes = int(votes_str.replace(',', ''))
                    return imdb_id, rating, votes
        except Exception:
            pass
        return imdb_id, None, 0


# ===================== 数据库操作 =====================

def init_local_db():
    """初始化本地数据库（如果不存在）"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER UNIQUE,
            title TEXT NOT NULL,
            original_title TEXT,
            type TEXT CHECK(type IN ('movie', 'tv')),
            overview TEXT,
            release_date TEXT,
            poster_url TEXT,
            imdb_rating REAL,
            added_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS title_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER,
            provider_name TEXT,
            FOREIGN KEY (title_id) REFERENCES titles(id) ON DELETE CASCADE,
            UNIQUE(title_id, provider_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_id ON titles(tmdb_id)")
    conn.commit()
    conn.close()


def get_existing_tmdb_ids():
    """获取数据库中所有已存在的 tmdb_id"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT tmdb_id FROM titles")
    ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    return ids


def load_tmdb_map():
    """加载 TMDB → IMDB 映射"""
    if os.path.exists(MAP_PATH):
        with open(MAP_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_tmdb_map(mapping):
    """保存 TMDB → IMDB 映射"""
    with open(MAP_PATH, 'w', encoding='utf-8') as f:
        json.dump(mapping, f)


def insert_title(title_data, imdb_rating):
    """插入或更新标题"""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            "SELECT id FROM titles WHERE tmdb_id = ?", (title_data['tmdb_id'],)
        )
        existing = cursor.fetchone()

        if existing:
            conn.execute("""
                UPDATE titles SET title=?, original_title=?, type=?, overview=?,
                release_date=?, poster_url=?, imdb_rating=?, added_date=?
                WHERE tmdb_id=?
            """, (
                title_data['title'], title_data['original_title'],
                title_data['type'], title_data['overview'],
                title_data['release_date'], title_data['poster_url'],
                imdb_rating, title_data.get('added_date', ''),
                title_data['tmdb_id']
            ))
            title_id = existing[0]
        else:
            cursor = conn.execute("""
                INSERT INTO titles
                (tmdb_id, title, original_title, type, overview, release_date,
                 poster_url, imdb_rating, added_date)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                title_data['tmdb_id'], title_data['title'],
                title_data['original_title'], title_data['type'],
                title_data['overview'], title_data['release_date'],
                title_data['poster_url'], imdb_rating,
                title_data.get('added_date', datetime.now().strftime("%Y-%m-%d"))
            ))
            title_id = cursor.lastrowid

        # 更新 providers
        for provider in title_data.get('providers', []):
            conn.execute(
                "INSERT OR IGNORE INTO title_providers (title_id, provider_name) VALUES (?,?)",
                (title_id, provider)
            )
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"    插入失败 {title_data.get('title')}: {e}")
        return False
    finally:
        conn.close()


# ===================== TMDB Discover =====================

async def discover_titles(days_back=30, max_pages=5):
    """从 TMDB Discover 获取近 N 天的流媒体新片"""
    import httpx
    all_titles = []
    cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    for provider_name, provider_id in PROVIDERS.items():
        print(f"\n  === {provider_name} ===")
        for media_type, date_field, sort_field in [
            ("movie", "primary_release_date", "primary_release_date.desc"),
            ("tv", "first_air_date", "first_air_date.desc"),
        ]:
            for page in range(1, max_pages + 1):
                data = await fetch_tmdb(
                    f"/discover/{media_type}",
                    {
                        "watch_region": "US",
                        "with_watch_providers": provider_id,
                        "watch_monetization_types": "flatrate",
                        "sort_by": sort_field,
                        f"{date_field}.gte": cutoff_date,
                        "page": page,
                    }
                )
                if not data or not data.get('results'):
                    break

                for item in data['results']:
                    if not item.get('poster_path'):
                        continue
                    all_titles.append({
                        'tmdb_id': item['id'],
                        'title': item.get('title') or item.get('name', ''),
                        'original_title': item.get('original_title') or item.get('original_name', ''),
                        'type': media_type,
                        'overview': item.get('overview', ''),
                        'release_date': item.get('release_date') or item.get('first_air_date', ''),
                        'poster_url': f"https://image.tmdb.org/t/p/w500{item['poster_path']}",
                        'providers': [provider_name],
                    })

    print(f"\n  TMDB Discover 合计: {len(all_titles)} 部")
    return all_titles


async def fetch_missing_imdb_ids(titles, existing_map_tmdb_ids):
    """为还没有 IMDB ID 映射的标题批量获取 IMDB ID"""
    tmdb_map = load_tmdb_map()
    new_titles = [t for t in titles if str(t['tmdb_id']) not in tmdb_map]
    if not new_titles:
        print(f"  所有标题都已在映射中，跳过 IMDB ID 查询")
        return

    print(f"  需获取 {len(new_titles)} 个新标题的 IMDB ID...")
    import httpx
    semaphore = asyncio.Semaphore(TMDB_CONCUR)
    async with httpx.AsyncClient(timeout=20) as client:
        tasks = [
            fetch_external_ids(client, t['tmdb_id'], t['type'], semaphore)
            for t in new_titles
        ]
        results = await asyncio.gather(*tasks)

    # 更新映射文件
    new_count = 0
    for tmdb_id, media_type, imdb_id in results:
        if imdb_id and imdb_id.startswith('tt'):
            tmdb_map[str(tmdb_id)] = imdb_id
            new_count += 1

    save_tmdb_map(tmdb_map)
    print(f"  获取了 {new_count} 个新 IMDB ID，已保存映射")


# ===================== OMDB 批量评分 =====================

async def rate_new_titles_omdb(titles):
    """用 OMDB API 为新片评分，达标的插入数据库"""
    tmdb_map = load_tmdb_map()
    titles_to_rate = [
        (t, tmdb_map.get(str(t['tmdb_id'])))
        for t in titles
        if tmdb_map.get(str(t['tmdb_id']))
    ]

    if not titles_to_rate:
        print("  没有可评分的新片（无 IMDB ID）")
        return 0, 0

    print(f"  正在通过 OMDB 为 {len(titles_to_rate)} 部新片获取评分...")
    import httpx
    semaphore = asyncio.Semaphore(OMDB_CONCUR)
    async with httpx.AsyncClient(timeout=15) as client:
        tasks = [
            fetch_omdb(client, imdb_id, semaphore)
            for _, imdb_id in titles_to_rate
        ]
        results = await asyncio.gather(*tasks)

    inserted = skipped = 0
    for (title_data, imdb_id), (iid, rating, votes) in zip(titles_to_rate, results):
        if rating is not None and rating >= OMDB_MIN_RATING and votes >= OMDB_MIN_VOTES:
            if insert_title(title_data, rating):
                inserted += 1
        else:
            skipped += 1

    print(f"  OMDB 评分入库: {inserted} 部（跳过 {skipped} 部 < 7.0 或票数不足）")
    return inserted, skipped


# ===================== IMDB 批量更新（首次初始化用）=====================

def update_all_ratings_from_imdb(imdb_ratings):
    """用 IMDB 数据集批量更新所有已有标题的评分"""
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
            if v >= IMDB_MIN_VOTES and r >= IMDB_MIN_RATING:
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

        if i % 200 == 0:
            print(f"    [{i}/{total}] IMDb={ok} cleared={cleared} no_map={no_map}")

    print(f"  IMDB 更新完成: IMDb={ok} cleared={cleared} no_map={no_map}")
    return ok, cleared, no_map


# ===================== 上传到路由器 =====================

def upload_to_router():
    """通过 SCP 上传数据库到软路由"""
    import subprocess
    result = subprocess.run(
        ['wsl.exe', '--', 'bash', '-c',
         f"sshpass -p {ROUTER_PASS} scp -o StrictHostKeyChecking=no {DB_PATH} {ROUTER_USER}@{ROUTER_HOST}:{ROUTER_DB_PATH}"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"\n  数据库已上传到路由器: {ROUTER_DB_PATH}")
    else:
        print(f"\n  上传失败: {result.stderr}")


# ===================== 主流程 =====================

async def sync_init():
    """首次初始化：下载 IMDB 数据集 + 更新所有已有标题评分"""
    print("\n" + "=" * 50)
    print("Step 1: 下载 IMDB 数据集")
    print("=" * 50)
    download_imdb_ratings(force=True)

    print("\n" + "=" * 50)
    print("Step 2: IMDB 数据集批量更新所有已有标题评分")
    print("=" * 50)
    imdb_ratings = load_imdb_ratings()
    update_all_ratings_from_imdb(imdb_ratings)

    print("\n" + "=" * 50)
    print("初始化完成！")
    print("=" * 50)


async def sync_daily():
    """每日同步：TMDB discover + OMDB 评分 + 上传"""
    print("\n" + "=" * 50)
    print("Step 1: TMDB Discover 近30天新片")
    print("=" * 50)
    titles = await discover_titles(days_back=30, max_pages=5)

    existing_ids = get_existing_tmdb_ids()
    print(f"\n  数据库已有: {len(existing_ids)} 部")
    new_titles = [t for t in titles if t['tmdb_id'] not in existing_ids]
    print(f"  新增: {len(new_titles)} 部")

    if new_titles:
        print("\n" + "=" * 50)
        print("Step 2: 获取新片的 IMDB ID")
        print("=" * 50)
        await fetch_missing_imdb_ids(new_titles, existing_ids)

        print("\n" + "=" * 50)
        print("Step 3: OMDB API 为新片评分")
        print("=" * 50)
        await rate_new_titles_omdb(new_titles)
    else:
        print("\n  无新片，跳过 Step 2-3")

    print("\n" + "=" * 50)
    print("Step 4: 上传数据库到软路由")
    print("=" * 50)
    upload_to_router()

    print("\n" + "=" * 50)
    print("每日同步完成！")
    print("=" * 50)


async def sync_omdb_only():
    """仅用 OMDB 为新片评分（跳过 discover，用于补充之前漏掉的新片）"""
    print("\n" + "=" * 50)
    print("Step 1: 获取数据库中所有无评分的标题")
    print("=" * 50)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT tmdb_id, title, type FROM titles WHERE imdb_rating IS NULL")
    unrated = cursor.fetchall()
    conn.close()
    print(f"  无评分标题: {len(unrated)} 部")

    tmdb_map = load_tmdb_map()
    titles_to_rate = []
    for tmdb_id, title, title_type in unrated:
        imdb_id = tmdb_map.get(str(tmdb_id))
        if imdb_id:
            titles_to_rate.append({
                'tmdb_id': tmdb_id,
                'title': title,
                'original_title': '',
                'type': title_type,
                'overview': '',
                'release_date': '',
                'poster_url': '',
                'providers': [],
            })

    if not titles_to_rate:
        print("  没有可评分的新片")
        return

    print("\n" + "=" * 50)
    print("Step 2: OMDB API 为无评分标题评分")
    print("=" * 50)
    await rate_new_titles_omdb(titles_to_rate)

    print("\n" + "=" * 50)
    print("Step 3: 上传数据库到软路由")
    print("=" * 50)
    upload_to_router()

    print("\n" + "=" * 50)
    print("OMDB 补全完成！")
    print("=" * 50)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='streaming-tracker 本地同步脚本')
    parser.add_argument('--init', action='store_true',
                        help='首次初始化：下载IMDB数据集 + 更新所有已有标题评分')
    parser.add_argument('--omdb-only', action='store_true',
                        help='仅用 OMDB 为数据库中无评分的标题补充评分')
    args = parser.parse_args()

    init_local_db()

    if args.init:
        asyncio.run(sync_init())
    elif args.omdb_only:
        asyncio.run(sync_omdb_only())
    else:
        asyncio.run(sync_daily())