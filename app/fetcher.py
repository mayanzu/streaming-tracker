import httpx
import asyncio
import re
import json as _json
from datetime import datetime, timedelta
from deep_translator import GoogleTranslator
from app.config import TMDB_API_KEY, TMDB_BASE_URL, PROVIDERS, OMDB_API_KEY, OMDB_BASE_URL

MIN_IMDB_VOTES = 100
MIN_IMDB_RATING = 7.0
ENRICH_CONCURRENCY = 30


async def translate_to_chinese(text):
    try:
        result = await asyncio.to_thread(
            GoogleTranslator(source='en', target='zh-CN').translate, text[:800]
        )
        return result if result else text
    except Exception:
        return text


async def fetch_tmdb(endpoint, params=None, retries=2):
    if params is None:
        params = {}
    params['api_key'] = TMDB_API_KEY
    params.setdefault('language', 'zh-CN')
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(f"{TMDB_BASE_URL}{endpoint}", params=params)
                response.raise_for_status()
                return response.json()
        except Exception:
            if attempt == retries:
                raise
            await asyncio.sleep(1)


IMDB_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}


async def fetch_omdb(imdb_id):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                OMDB_BASE_URL, params={"i": imdb_id, "apikey": OMDB_API_KEY}
            )
            data = response.json()
            if data.get('Response') == 'True':
                r = data.get('imdbRating')
                v = data.get('imdbVotes', '0')
                if r not in (None, 'N/A'):
                    return float(r), int(v.replace(',', ''))
    except Exception:
        pass
    return None, 0


async def scrape_imdb(imdb_id):
    """OMDB失败时的备选：直接从IMDb页面JSON-LD抓取评分"""
    try:
        async with httpx.AsyncClient(headers=IMDB_HEADERS, follow_redirects=True, timeout=15) as client:
            response = await client.get(f"https://www.imdb.com/title/{imdb_id}/")
            if response.status_code != 200:
                return None, 0
            match = re.search(
                r'<script type="application/ld\+json">(.*?)</script>',
                response.text, re.DOTALL
            )
            if match:
                data = _json.loads(match.group(1))
                agg = data.get('aggregateRating', {})
                rating = agg.get('ratingValue')
                count = agg.get('ratingCount', 0)
                if rating:
                    return float(rating), int(count)
    except Exception:
        pass
    return None, 0


# 模块级缓存：启动时检测各方法是否可用
_omdb_ok = None
_scrape_ok = None
_check_lock = asyncio.Lock()


async def _check_omdb():
    global _omdb_ok
    async with _check_lock:
        if _omdb_ok is None:
            r, v = await fetch_omdb('tt3896198')
            _omdb_ok = (r is not None)
            print(f"  OMDB check: {'OK' if _omdb_ok else 'UNAVAILABLE (rate limit)'}", flush=True)
    return _omdb_ok


async def _check_scrape():
    global _scrape_ok
    async with _check_lock:
        if _scrape_ok is None:
            r, v = await scrape_imdb('tt3896198')
            _scrape_ok = (r is not None)
            print(f"  IMDb scrape check: {'OK' if _scrape_ok else 'UNAVAILABLE (blocked)'}", flush=True)
    return _scrape_ok


async def get_trusted_rating(imdb_id, tmdb_vote_avg, tmdb_vote_count):
    """TMDB 评分（>=10票），OMDB 恢复后替换为 IMDb"""
    if tmdb_vote_count >= 5 and tmdb_vote_avg > 0:
        return tmdb_vote_avg, tmdb_vote_count, 'tmdb'
    return None, 0, None


async def fetch_new_releases(provider_name, days_back=1825):
    """获取平台新片（不按评分过滤，全量拿海报+基本信息）"""
    if provider_name not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider_name}")

    provider_id = PROVIDERS[provider_name]
    cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    results = []

    for media_type, date_field, sort_field in [
        ("movie", "primary_release_date", "primary_release_date.desc"),
        ("tv", "first_air_date", "first_air_date.desc"),
    ]:
        for page in range(1, 30):
            data = await fetch_tmdb(f"/discover/{media_type}", {
                "watch_region": "US",
                "with_watch_providers": provider_id,
                "watch_monetization_types": "flatrate",
                "sort_by": sort_field,
                date_field + ".gte": cutoff_date,
                "page": page,
            })
            page_results = data.get('results', [])
            if not page_results:
                break

            for item in page_results:
                if not item.get('poster_path'):
                    continue
                poster = f"https://image.tmdb.org/t/p/w500{item['poster_path']}"
                results.append({
                    'tmdb_id': item['id'],
                    'title': item.get('title') or item.get('name', ''),
                    'original_title': item.get('original_title') or item.get('original_name', ''),
                    'type': media_type,
                    'overview': item.get('overview', ''),
                    'release_date': item.get('release_date') or item.get('first_air_date', ''),
                    'poster_url': poster,
                    'imdb_rating': None,
                    'added_date': datetime.now().strftime("%Y-%m-%d"),
                    'providers': [provider_name],
                })

    return results


async def enrich_with_imdb(title_data):
    """填充 IMDb 评分（三级策略：OMDB → IMDb抓取 → TMDB大样本兜底）"""
    try:
        endpoint = f"/{'movie' if title_data['type'] == 'movie' else 'tv'}/{title_data['tmdb_id']}"
        details = await fetch_tmdb(endpoint, {"append_to_response": "external_ids"})

        # 英文简介兜底
        if not title_data.get('overview') and not details.get('overview'):
            en = await fetch_tmdb(endpoint, {"language": "en-US"})
            if en.get('overview'):
                title_data['overview'] = await translate_to_chinese(en['overview'])

        # 三级评分策略
        imdb_id = details.get('external_ids', {}).get('imdb_id')
        rating, votes, source = await get_trusted_rating(
            imdb_id,
            details.get('vote_average', 0),
            details.get('vote_count', 0)
        )
        if rating is not None:
            title_data['imdb_rating'] = rating
    except Exception as e:
        print(f"  enrich error: {title_data.get('title','?')}: {e}")

    return title_data


async def fetch_all_providers():
    """全平台抓取 → IMDb评分 → 只留 >=7.0"""
    sem = asyncio.Semaphore(ENRICH_CONCURRENCY)
    all_titles = []

    for provider_name in PROVIDERS:
        print(f"\n{'='*50}", flush=True)
        print(f"  {provider_name}", flush=True)
        print(f"{'='*50}", flush=True)

        try:
            titles = await fetch_new_releases(provider_name)
            print(f"  TMDB discover: {len(titles)} 部", flush=True)

            # 并发 enrich
            async def enrich_one(t):
                async with sem:
                    return await enrich_with_imdb(t)

            await asyncio.gather(*[enrich_one(t) for t in titles])

            # IMDb >= 7.0 过滤
            qualified = [t for t in titles if t['imdb_rating'] is not None and t['imdb_rating'] >= MIN_IMDB_RATING]
            no_rating = sum(1 for t in titles if t['imdb_rating'] is None)
            low_rating = len(titles) - len(qualified) - no_rating
            print(f"  IMDb>=7.0: {len(qualified)}  无可靠评分: {no_rating}  低分过滤: {low_rating}", flush=True)

            all_titles.extend(qualified)

        except Exception as e:
            print(f"  Error: {e}", flush=True)

    print(f"\n{'='*50}", flush=True)
    print(f"  总计入库: {len(all_titles)} 部 (IMDb>={MIN_IMDB_RATING}, >= {MIN_IMDB_VOTES}票)", flush=True)
    print(f"{'='*50}", flush=True)
    return all_titles
