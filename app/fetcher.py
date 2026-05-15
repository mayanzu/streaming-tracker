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
    PROVIDER_REGIONS,
    PROVIDERS,
    TMDB_API_KEY,
    TMDB_BASE_URL,
)

logger = logging.getLogger(__name__)
TRUSTED_RATING_SOURCES = {"imdb", "omdb"}


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


async def fetch_tmdb(endpoint, params=None, retries=2, client=None):
    request_params = dict(params or {})
    request_params["api_key"] = TMDB_API_KEY
    request_params.setdefault("language", "zh-CN")

    for attempt in range(retries + 1):
        try:
            if client:
                response = await client.get(f"{TMDB_BASE_URL}{endpoint}", params=request_params)
            else:
                async with httpx.AsyncClient(timeout=20) as one_off_client:
                    response = await one_off_client.get(
                        f"{TMDB_BASE_URL}{endpoint}", params=request_params
                    )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 and attempt < retries:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt == retries:
                raise
            await asyncio.sleep(1 + attempt)


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


async def get_imdb_rating(imdb_id):
    """只返回 IMDb 评分。OMDb 也只使用其 imdbRating 字段，不使用 TMDB 评分兜底。"""
    if not imdb_id:
        return None, 0, None

    if imdb_id:
        from app.imdb_data import get_rating

        rating, votes = get_rating(imdb_id)
        if rating is not None and votes >= MIN_IMDB_VOTES:
            return rating, votes, "imdb"

    rating, votes = await fetch_omdb(imdb_id)
    if rating is not None and votes >= OMDB_MIN_VOTES:
        return rating, votes, "imdb"

    return None, 0, None


def _discover_date_ranges(days_back, window_days):
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    if window_days <= 0 or window_days >= days_back:
        yield cutoff, None
        return

    end = datetime.now()
    cutoff_date = end - timedelta(days=days_back)
    while end > cutoff_date:
        start = max(cutoff_date, end - timedelta(days=window_days))
        yield start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        end = start - timedelta(days=1)


async def fetch_new_releases(provider_name, days_back=1825, max_pages=29, window_days=90, client=None):
    """获取平台新片，不在 discover 阶段按评分过滤。"""
    if provider_name not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider_name}")
    if not TMDB_API_KEY:
        raise ValueError("TMDB_API_KEY is required")

    provider_id = PROVIDERS[provider_name]
    regions = PROVIDER_REGIONS.get(provider_name) or ("JP",)
    added_date = datetime.now().strftime("%Y-%m-%d")
    results = {}

    for region in regions:
        for media_type, date_field, sort_field in [
            ("movie", "primary_release_date", "primary_release_date.desc"),
            ("tv", "first_air_date", "first_air_date.desc"),
        ]:
            for window_start, window_end in _discover_date_ranges(days_back, window_days):
                for page in range(1, max_pages + 1):
                    params = {
                        "watch_region": region,
                        "with_watch_providers": provider_id,
                        "watch_monetization_types": "flatrate",
                        "sort_by": sort_field,
                        f"{date_field}.gte": window_start,
                        "page": page,
                    }
                    if window_end:
                        params[f"{date_field}.lte"] = window_end

                    data = await fetch_tmdb(
                        f"/discover/{media_type}",
                        params,
                        client=client,
                    )
                    page_results = data.get("results", [])
                    if not page_results:
                        break

                    for item in page_results:
                        if not item.get("poster_path"):
                            continue

                        key = (media_type, item["id"])
                        poster_path = item.get("poster_path")
                        results[key] = {
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
                            "rating_source": None,
                            "rating_votes": None,
                            "added_date": added_date,
                            "providers": [provider_name],
                        }

                    total_pages = min(data.get("total_pages", page) or page, max_pages)
                    if page >= total_pages:
                        break

    return list(results.values())


async def _notify_progress(callback, **payload):
    if not callback:
        return
    result = callback(payload)
    if asyncio.iscoroutine(result):
        await result


async def enrich_with_imdb(title_data, client=None):
    """填充评分和中文简介兜底。"""
    try:
        endpoint = f"/{'movie' if title_data['type'] == 'movie' else 'tv'}/{title_data['tmdb_id']}"
        details = await fetch_tmdb(
            endpoint,
            {
                "append_to_response": "external_ids,images",
                "include_image_language": "zh,null,en",
            },
            client=client,
        )

        localized_poster = _localized_poster_path(details)
        if localized_poster:
            title_data["poster_url"] = _poster_url(localized_poster)

        if not title_data.get("overview") and not details.get("overview"):
            en = await fetch_tmdb(endpoint, {"language": "en-US"}, client=client)
            if en.get("overview"):
                title_data["overview"] = await translate_to_chinese(en["overview"])

        imdb_id = details.get("external_ids", {}).get("imdb_id")
        rating, votes, source = await get_imdb_rating(imdb_id)
        if rating is not None:
            title_data["imdb_rating"] = rating
            title_data["rating_source"] = source
            title_data["rating_votes"] = votes
    except Exception:
        logger.exception("Failed to enrich title %s", title_data.get("title", "?"))

    return title_data


def empty_fetch_stats():
    return {
        "discovered": 0,
        "qualified": 0,
        "no_rating": 0,
        "low_rating": 0,
        "errors": [],
    }


def merge_fetch_stats(total, partial):
    for key in ("discovered", "qualified", "no_rating", "low_rating"):
        total[key] += partial.get(key, 0)
    total["errors"].extend(partial.get("errors", []))


async def fetch_provider_titles(
    provider_name,
    days_back=1825,
    max_pages=29,
    window_days=90,
    provider_index=1,
    provider_total=1,
    client=None,
    progress_callback=None,
):
    """抓取单个平台，评分补全后只返回达标作品。"""
    sem = asyncio.Semaphore(ENRICH_CONCURRENCY)
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=20)

    stats = empty_fetch_stats()
    qualified = []

    try:
        logger.info("Fetching provider=%s", provider_name)
        titles = await fetch_new_releases(
            provider_name,
            days_back=days_back,
            max_pages=max_pages,
            window_days=window_days,
            client=client,
        )
        stats["discovered"] = len(titles)
        logger.info("TMDB discover provider=%s count=%s", provider_name, len(titles))
        await _notify_progress(
            progress_callback,
            phase="discovered",
            provider=provider_name,
            provider_index=provider_index,
            provider_total=provider_total,
            provider_discovered=len(titles),
            stats=dict(stats),
        )

        async def enrich_one(title):
            async with sem:
                return await enrich_with_imdb(title, client=client)

        enriched_titles = await asyncio.gather(*(enrich_one(title) for title in titles))
        qualified = [
            title
            for title in enriched_titles
            if title["imdb_rating"] is not None
            and title.get("rating_source") in TRUSTED_RATING_SOURCES
            and title["imdb_rating"] >= MIN_IMDB_RATING
        ]
        no_rating = sum(
            1
            for title in enriched_titles
            if title["imdb_rating"] is None
            or title.get("rating_source") not in TRUSTED_RATING_SOURCES
        )
        low_rating = len(enriched_titles) - len(qualified) - no_rating

        stats["qualified"] = len(qualified)
        stats["no_rating"] = no_rating
        stats["low_rating"] = low_rating
        logger.info(
            "Qualified provider=%s count=%s no_rating=%s low_rating=%s",
            provider_name,
            len(qualified),
            no_rating,
            low_rating,
        )
        await _notify_progress(
            progress_callback,
            phase="qualified",
            provider=provider_name,
            provider_index=provider_index,
            provider_total=provider_total,
            provider_discovered=len(titles),
            provider_qualified=len(qualified),
            stats=dict(stats),
        )

    except Exception as exc:
        message = f"{provider_name}: {exc}"
        stats["errors"].append(message)
        logger.exception("Failed to fetch provider=%s", provider_name)
    finally:
        if owns_client:
            await client.aclose()

    return {"provider": provider_name, "titles": qualified, "stats": stats}


async def fetch_all_providers(days_back=1825, max_pages=29, window_days=90, progress_callback=None):
    """全平台抓取 -> 评分补全 -> 只返回达标作品。"""
    all_titles = []
    stats = empty_fetch_stats()

    async with httpx.AsyncClient(timeout=20) as client:
        provider_total = len(PROVIDERS)
        for provider_index, provider_name in enumerate(PROVIDERS, start=1):
            provider_result = await fetch_provider_titles(
                provider_name,
                days_back=days_back,
                max_pages=max_pages,
                window_days=window_days,
                provider_index=provider_index,
                provider_total=provider_total,
                client=client,
                progress_callback=progress_callback,
            )
            merge_fetch_stats(stats, provider_result["stats"])
            all_titles.extend(provider_result["titles"])

    stats["qualified"] = len(all_titles)
    logger.info(
        "Fetch finished discovered=%s qualified=%s no_rating=%s low_rating=%s",
        stats["discovered"],
        stats["qualified"],
        stats["no_rating"],
        stats["low_rating"],
    )
    return {"titles": all_titles, "stats": stats}
