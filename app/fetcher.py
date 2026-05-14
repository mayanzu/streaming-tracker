import asyncio
import logging
from datetime import datetime, timedelta

import httpx
from deep_translator import GoogleTranslator

from app.config import (
    ENRICH_CONCURRENCY,
    MIN_IMDB_RATING,
    MIN_IMDB_VOTES,
    OMDB_API_KEY,
    OMDB_BASE_URL,
    OMDB_MIN_VOTES,
    PROVIDERS,
    TMDB_API_KEY,
    TMDB_BASE_URL,
    TMDB_FALLBACK_MIN_VOTES,
)

logger = logging.getLogger(__name__)


def _localized_poster_path(details):
    posters = details.get("images", {}).get("posters", [])
    if not posters:
        return details.get("poster_path")

    for language in ("zh", "zh-CN", "zh-TW", "zh-HK", None, "en"):
        for poster in posters:
            if poster.get("iso_639_1") == language and poster.get("file_path"):
                return poster["file_path"]

    return details.get("poster_path") or posters[0].get("file_path")


def _poster_url(path):
    return f"https://image.tmdb.org/t/p/w500{path}" if path else None


async def translate_to_chinese(text):
    if not text:
        return text

    try:
        result = await asyncio.to_thread(
            GoogleTranslator(source="en", target="zh-CN").translate, text[:800]
        )
        return result if result else text
    except Exception as exc:
        logger.warning("Failed to translate overview: %s", exc)
        return text


async def fetch_tmdb(endpoint, params=None, retries=2):
    request_params = dict(params or {})
    request_params["api_key"] = TMDB_API_KEY
    request_params.setdefault("language", "zh-CN")

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(f"{TMDB_BASE_URL}{endpoint}", params=request_params)
                response.raise_for_status()
                return response.json()
        except Exception:
            if attempt == retries:
                raise
            await asyncio.sleep(1)


async def fetch_omdb(imdb_id):
    """返回 (rating, votes)，无数据返回 (None, 0)。"""
    if not OMDB_API_KEY:
        return None, 0

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                OMDB_BASE_URL, params={"i": imdb_id, "apikey": OMDB_API_KEY}
            )
            data = response.json()
            if data.get("Response") == "True":
                rating = data.get("imdbRating")
                votes = data.get("imdbVotes", "0")
                if rating not in (None, "N/A"):
                    return float(rating), int(votes.replace(",", ""))
    except Exception as exc:
        logger.warning("Failed to fetch OMDB rating for %s: %s", imdb_id, exc)

    return None, 0


async def get_trusted_rating(imdb_id, tmdb_vote_avg, tmdb_vote_count):
    """IMDb 数据集 -> OMDB -> TMDB 三级评分来源。"""
    if imdb_id:
        from app.imdb_data import get_rating

        rating, votes = get_rating(imdb_id)
        if rating is not None and votes >= MIN_IMDB_VOTES:
            return rating, votes, "imdb"

    if imdb_id:
        rating, votes = await fetch_omdb(imdb_id)
        if rating is not None and votes >= OMDB_MIN_VOTES:
            return rating, votes, "omdb"

    if tmdb_vote_count >= TMDB_FALLBACK_MIN_VOTES and tmdb_vote_avg > 0:
        return tmdb_vote_avg, tmdb_vote_count, "tmdb"

    return None, 0, None


async def fetch_new_releases(provider_name, days_back=1825, max_pages=29):
    """获取平台新片，不在 discover 阶段按评分过滤。"""
    if provider_name not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider_name}")
    if not TMDB_API_KEY:
        raise ValueError("TMDB_API_KEY is required")

    provider_id = PROVIDERS[provider_name]
    cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    added_date = datetime.now().strftime("%Y-%m-%d")
    results = []

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
                },
            )
            page_results = data.get("results", [])
            if not page_results:
                break

            for item in page_results:
                if not item.get("poster_path"):
                    continue

                poster_path = item.get("poster_path")
                results.append(
                    {
                        "tmdb_id": item["id"],
                        "title": item.get("title") or item.get("name", ""),
                        "original_title": item.get("original_title")
                        or item.get("original_name", ""),
                        "type": media_type,
                        "overview": item.get("overview", ""),
                        "release_date": item.get("release_date")
                        or item.get("first_air_date", ""),
                        "poster_url": _poster_url(poster_path),
                        "imdb_rating": None,
                        "added_date": added_date,
                        "providers": [provider_name],
                    }
                )

    return results


async def enrich_with_imdb(title_data):
    """填充评分和中文简介兜底。"""
    try:
        endpoint = f"/{'movie' if title_data['type'] == 'movie' else 'tv'}/{title_data['tmdb_id']}"
        details = await fetch_tmdb(
            endpoint,
            {
                "append_to_response": "external_ids,images",
                "include_image_language": "zh,null,en",
            },
        )

        localized_poster = _localized_poster_path(details)
        if localized_poster:
            title_data["poster_url"] = _poster_url(localized_poster)

        if not title_data.get("overview") and not details.get("overview"):
            en = await fetch_tmdb(endpoint, {"language": "en-US"})
            if en.get("overview"):
                title_data["overview"] = await translate_to_chinese(en["overview"])

        imdb_id = details.get("external_ids", {}).get("imdb_id")
        rating, _votes, _source = await get_trusted_rating(
            imdb_id,
            details.get("vote_average", 0),
            details.get("vote_count", 0),
        )
        if rating is not None:
            title_data["imdb_rating"] = rating
    except Exception:
        logger.exception("Failed to enrich title %s", title_data.get("title", "?"))

    return title_data


async def fetch_all_providers(days_back=1825, max_pages=29):
    """全平台抓取 -> 评分补全 -> 只返回达标作品。"""
    sem = asyncio.Semaphore(ENRICH_CONCURRENCY)
    all_titles = []

    for provider_name in PROVIDERS:
        print(f"\n{'=' * 50}", flush=True)
        print(f"  {provider_name}", flush=True)
        print(f"{'=' * 50}", flush=True)

        try:
            titles = await fetch_new_releases(
                provider_name, days_back=days_back, max_pages=max_pages
            )
            print(f"  TMDB discover: {len(titles)} 部", flush=True)

            async def enrich_one(title):
                async with sem:
                    return await enrich_with_imdb(title)

            enriched_titles = await asyncio.gather(*(enrich_one(title) for title in titles))
            qualified = [
                title
                for title in enriched_titles
                if title["imdb_rating"] is not None
                and title["imdb_rating"] >= MIN_IMDB_RATING
            ]
            no_rating = sum(1 for title in enriched_titles if title["imdb_rating"] is None)
            low_rating = len(enriched_titles) - len(qualified) - no_rating
            print(
                f"  IMDb>={MIN_IMDB_RATING}: {len(qualified)}  "
                f"无可靠评分: {no_rating}  低分过滤: {low_rating}",
                flush=True,
            )

            all_titles.extend(qualified)

        except Exception as exc:
            print(f"  Error: {exc}", flush=True)

    print(f"\n{'=' * 50}", flush=True)
    print(
        f"  总计入库: {len(all_titles)} 部 "
        f"(IMDb>={MIN_IMDB_RATING}, >= {MIN_IMDB_VOTES}票)",
        flush=True,
    )
    print(f"{'=' * 50}", flush=True)
    return all_titles
