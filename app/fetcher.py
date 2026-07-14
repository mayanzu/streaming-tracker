import asyncio
import logging
import random
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache

import httpx
from deep_translator import GoogleTranslator

from app.config import (
    DEFAULT_PROVIDER_REGIONS,
    DETAIL_REFRESH_DAYS,
    DISCOVER_CONCURRENCY,
    ENRICH_CONCURRENCY,
    HTTP_RETRIES,
    MIN_IMDB_RATING,
    MIN_IMDB_VOTES,
    OMDB_API_KEY,
    OMDB_BASE_URL,
    OMDB_MIN_VOTES,
    PROVIDER_REGIONS,
    PROVIDERS,
    TMDB_API_KEY,
    TMDB_BASE_URL,
    WATCH_MONETIZATION_TYPES,
)

logger = logging.getLogger(__name__)
TRUSTED_RATING_SOURCES = {"imdb", "omdb"}
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class ExternalRequestError(RuntimeError):
    def __init__(self, service, message, status_code=None):
        super().__init__(message)
        self.service = service
        self.status_code = status_code


@lru_cache(maxsize=5000)
def _translate_cached(text: str) -> str:
    try:
        result = GoogleTranslator(source="en", target="zh-CN").translate(text[:800])
        return result if result else text
    except Exception:
        return text


def _localized_poster_path(details):
    posters = (details.get("images") or {}).get("posters") or []
    if not posters:
        return details.get("poster_path")

    for language in ("zh", None, "en"):
        candidates = [
            poster for poster in posters
            if poster.get("iso_639_1") == language and poster.get("file_path")
        ]
        if candidates:
            return max(candidates, key=lambda item: item.get("vote_average") or 0)["file_path"]
    return details.get("poster_path") or posters[0].get("file_path")


def _poster_url(path):
    return f"https://image.tmdb.org/t/p/w500{path}" if path else None


async def translate_to_chinese(text):
    if not text:
        return text
    return await asyncio.to_thread(_translate_cached, text[:800])


def _retry_delay(response, attempt):
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, min((retry_at - datetime.now(timezone.utc)).total_seconds(), 60.0))
            except (TypeError, ValueError):
                pass
    return min(2 ** attempt + random.uniform(0.0, 0.75), 30.0)


async def fetch_tmdb(endpoint, params=None, retries=HTTP_RETRIES, client=None):
    request_params = dict(params or {})
    headers = {}
    if len(TMDB_API_KEY) == 32:
        request_params["api_key"] = TMDB_API_KEY
    else:
        headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
    request_params.setdefault("language", "zh-CN")

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    try:
        for attempt in range(retries + 1):
            response = None
            try:
                response = await client.get(
                    f"{TMDB_BASE_URL}{endpoint}", params=request_params, headers=headers,
                )
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < retries:
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < retries:
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
                raise ExternalRequestError("tmdb", type(exc).__name__) from exc
            except httpx.HTTPStatusError as exc:
                raise ExternalRequestError(
                    "tmdb", f"HTTP {exc.response.status_code}", exc.response.status_code,
                ) from exc
        raise ExternalRequestError("tmdb", "retry budget exhausted")
    finally:
        if owns_client:
            await client.aclose()


async def fetch_omdb(imdb_id, client=None, retries=HTTP_RETRIES):
    if not OMDB_API_KEY:
        return None, 0
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=8.0))
    try:
        for attempt in range(retries + 1):
            response = None
            try:
                response = await client.get(
                    OMDB_BASE_URL, params={"i": imdb_id, "apikey": OMDB_API_KEY},
                )
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < retries:
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
                response.raise_for_status()
                data = response.json()
                if data.get("Response") != "True":
                    return None, 0
                rating = data.get("imdbRating")
                votes = data.get("imdbVotes", "0")
                if rating in (None, "N/A"):
                    return None, 0
                return float(rating), int(votes.replace(",", ""))
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < retries:
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
                raise ExternalRequestError("omdb", type(exc).__name__) from exc
            except httpx.HTTPStatusError as exc:
                raise ExternalRequestError(
                    "omdb", f"HTTP {exc.response.status_code}", exc.response.status_code,
                ) from exc
        raise ExternalRequestError("omdb", "retry budget exhausted")
    finally:
        if owns_client:
            await client.aclose()


async def get_imdb_ratings(imdb_ids, client=None):
    from app.imdb_data import get_ratings

    requested = {imdb_id for imdb_id in imdb_ids if imdb_id}
    resolved = {}
    errors = {}
    try:
        local = await get_ratings(requested)
    except Exception as exc:
        logger.exception("IMDb dataset lookup failed")
        local = {}
        for imdb_id in requested:
            errors[imdb_id] = f"IMDb dataset: {type(exc).__name__}"

    for imdb_id, (rating, votes) in local.items():
        if rating is not None and votes >= MIN_IMDB_VOTES:
            resolved[imdb_id] = (rating, votes, "imdb")

    missing = requested - set(resolved)
    if not missing or not OMDB_API_KEY:
        return resolved, errors

    semaphore = asyncio.Semaphore(ENRICH_CONCURRENCY)

    async def fetch_one(imdb_id):
        try:
            async with semaphore:
                rating, votes = await fetch_omdb(imdb_id, client=client)
            if rating is not None and votes >= OMDB_MIN_VOTES:
                return imdb_id, (rating, votes, "omdb"), None
            return imdb_id, None, None
        except Exception as exc:
            return imdb_id, None, f"OMDb: {type(exc).__name__}: {exc}"

    for imdb_id, value, error in await asyncio.gather(*(fetch_one(item) for item in missing)):
        if value:
            resolved[imdb_id] = value
            errors.pop(imdb_id, None)
        elif error:
            errors[imdb_id] = error
    return resolved, errors


async def get_imdb_rating(imdb_id):
    if not imdb_id:
        return None, 0, None
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        ratings, _ = await get_imdb_ratings({imdb_id}, client=client)
    return ratings.get(imdb_id, (None, 0, None))


def _date_ranges(days_back, window_days, end_date=None):
    end = end_date or date.today()
    start = end - timedelta(days=max(days_back, 0))
    if window_days <= 0 or window_days >= days_back:
        return [(start, end)]
    ranges = []
    cursor = end
    while cursor >= start:
        window_start = max(start, cursor - timedelta(days=window_days - 1))
        ranges.append((window_start, cursor))
        cursor = window_start - timedelta(days=1)
    return ranges


def _merge_candidate(target, incoming):
    target["providers"] = list(dict.fromkeys((target.get("providers") or []) + (incoming.get("providers") or [])))
    regions = target.setdefault("provider_regions", {})
    for provider, values in (incoming.get("provider_regions") or {}).items():
        regions[provider] = list(dict.fromkeys((regions.get(provider) or []) + values))
    target["discovery_channels"] = list(dict.fromkeys(
        (target.get("discovery_channels") or []) + (incoming.get("discovery_channels") or [])
    ))
    for field in ("title", "original_title", "overview", "release_date", "poster_url"):
        if not target.get(field) and incoming.get(field):
            target[field] = incoming[field]
    return target


def _candidate_from_item(item, media_type, provider_name, region, channel):
    return {
        "tmdb_id": item["id"],
        "title": item.get("title") or item.get("name") or "",
        "original_title": item.get("original_title") or item.get("original_name") or "",
        "type": media_type,
        "overview": item.get("overview") or "",
        "release_date": item.get("release_date") or item.get("first_air_date") or "",
        "poster_url": _poster_url(item.get("poster_path")),
        "imdb_rating": None,
        "rating_source": None,
        "rating_votes": None,
        "added_date": date.today().isoformat(),
        "providers": [provider_name],
        "provider_regions": {provider_name: [region]},
        "discovery_channels": [channel],
    }


async def _discover_range(
    client, semaphore, provider_name, provider_id, region, media_type,
    date_field, sort_field, range_start, range_end, max_pages, channel,
):
    async def fetch_page(page):
        params = {
            "watch_region": region,
            "with_watch_providers": provider_id,
            "watch_monetization_types": WATCH_MONETIZATION_TYPES,
            "sort_by": sort_field,
            f"{date_field}.gte": range_start.isoformat(),
            f"{date_field}.lte": range_end.isoformat(),
            "page": page,
        }
        async with semaphore:
            return await fetch_tmdb(f"/discover/{media_type}", params, client=client)

    first = await fetch_page(1)
    total_pages = int(first.get("total_pages") or 0)
    if total_pages > max_pages and range_start < range_end:
        midpoint = range_start + (range_end - range_start) // 2
        left, right = await asyncio.gather(
            _discover_range(
                client, semaphore, provider_name, provider_id, region, media_type,
                date_field, sort_field, range_start, midpoint, max_pages, channel,
            ),
            _discover_range(
                client, semaphore, provider_name, provider_id, region, media_type,
                date_field, sort_field, midpoint + timedelta(days=1), range_end, max_pages, channel,
            ),
        )
        return left[0] + right[0], left[1] + right[1]

    errors = []
    if total_pages > max_pages:
        errors.append(
            f"truncated provider={provider_name} region={region} type={media_type} "
            f"channel={channel} date={range_start.isoformat()} pages={total_pages} cap={max_pages}"
        )
    pages = [first]
    page_cap = min(max(total_pages, 1), max_pages)
    for page in range(2, page_cap + 1):
        pages.append(await fetch_page(page))

    candidates = []
    for payload in pages:
        for item in payload.get("results") or []:
            candidates.append(_candidate_from_item(item, media_type, provider_name, region, channel))
    return candidates, errors


async def discover_provider(
    provider_name, days_back=30, max_pages=5, window_days=0,
    client=None, catalog_range=None,
):
    if provider_name not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider_name}")
    if not TMDB_API_KEY:
        raise ValueError("TMDB_API_KEY is required")

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    semaphore = asyncio.Semaphore(DISCOVER_CONCURRENCY)
    provider_id = PROVIDERS[provider_name]
    regions = PROVIDER_REGIONS.get(provider_name) or DEFAULT_PROVIDER_REGIONS[provider_name]
    recent_ranges = _date_ranges(days_back, window_days)
    media_specs = (
        ("movie", "release_date", "release_date.desc", "movie_release"),
        ("tv", "first_air_date", "first_air_date.desc", "tv_premiere"),
        ("tv", "air_date", "popularity.desc", "tv_current_airing"),
    )

    tasks = []
    labels = []
    for region in regions:
        for media_type, date_field, sort_field, channel in media_specs:
            for range_start, range_end in recent_ranges:
                tasks.append(_discover_range(
                    client, semaphore, provider_name, provider_id, region, media_type,
                    date_field, sort_field, range_start, range_end, max_pages, channel,
                ))
                labels.append((region, media_type, channel, range_start, range_end))
            if catalog_range and channel in {"movie_release", "tv_premiere"}:
                range_start, range_end = catalog_range
                tasks.append(_discover_range(
                    client, semaphore, provider_name, provider_id, region, media_type,
                    date_field, sort_field, range_start, range_end, max_pages, "catalog_compensation",
                ))
                labels.append((region, media_type, "catalog_compensation", range_start, range_end))

    merged = {}
    errors = []
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for label, result in zip(labels, results):
            if isinstance(result, Exception):
                region, media_type, channel, range_start, range_end = label
                message = (
                    f"provider={provider_name} region={region} type={media_type} "
                    f"channel={channel} dates={range_start}:{range_end}: "
                    f"{type(result).__name__}: {result}"
                )
                errors.append(message)
                logger.warning("Discover query failed: %s", message)
                continue
            candidates, query_errors = result
            errors.extend(query_errors)
            for candidate in candidates:
                key = (candidate["type"], candidate["tmdb_id"])
                if key in merged:
                    _merge_candidate(merged[key], candidate)
                else:
                    merged[key] = candidate
        return {"provider": provider_name, "titles": list(merged.values()), "errors": errors}
    finally:
        if owns_client:
            await client.aclose()


def empty_fetch_stats():
    return {
        "discovered": 0,
        "unique_discovered": 0,
        "cached": 0,
        "qualified": 0,
        "pending": 0,
        "no_rating": 0,
        "low_rating": 0,
        "request_failed": 0,
        "errors": [],
    }


def merge_fetch_stats(total, partial):
    for key in (
        "discovered", "unique_discovered", "cached", "qualified", "pending",
        "no_rating", "low_rating", "request_failed",
    ):
        total[key] = total.get(key, 0) + partial.get(key, 0)
    total.setdefault("errors", []).extend(partial.get("errors", []))


async def _notify_progress(callback, **payload):
    if not callback:
        return
    result = callback(payload)
    if asyncio.iscoroutine(result):
        await result


async def discover_all_providers(
    days_back=30, max_pages=5, window_days=0, catalog_range=None, progress_callback=None,
):
    merged = {}
    stats = empty_fetch_stats()
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        provider_total = len(PROVIDERS)
        for provider_index, provider_name in enumerate(PROVIDERS, start=1):
            result = await discover_provider(
                provider_name,
                days_back=days_back,
                max_pages=max_pages,
                window_days=window_days,
                client=client,
                catalog_range=catalog_range,
            )
            stats["discovered"] += len(result["titles"])
            stats["errors"].extend(result["errors"])
            for candidate in result["titles"]:
                key = (candidate["type"], candidate["tmdb_id"])
                if key in merged:
                    _merge_candidate(merged[key], candidate)
                else:
                    merged[key] = candidate
            stats["unique_discovered"] = len(merged)
            await _notify_progress(
                progress_callback,
                phase="discovered",
                provider=provider_name,
                provider_index=provider_index,
                provider_total=provider_total,
                provider_discovered=len(result["titles"]),
                stats=dict(stats),
            )
    return {"titles": list(merged.values()), "stats": stats}


def _is_fresh(cached):
    if (
        not cached
        or cached.get("rating_source") not in TRUSTED_RATING_SOURCES
        or cached.get("imdb_rating") is None
        or float(cached["imdb_rating"]) < MIN_IMDB_RATING
    ):
        return False
    value = cached.get("last_synced_at") if cached else None
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - parsed < timedelta(days=DETAIL_REFRESH_DAYS)
    except (TypeError, ValueError):
        return False


def _cached_title(candidate, cached):
    result = dict(cached)
    result["providers"] = candidate.get("providers") or []
    result["provider_regions"] = candidate.get("provider_regions") or {}
    result["discovery_channels"] = candidate.get("discovery_channels") or []
    result["added_date"] = cached.get("added_date") or candidate.get("added_date")
    result["last_seen_at"] = datetime.now(timezone.utc).isoformat()
    return result


async def _fetch_details(candidate, client):
    title = dict(candidate)
    for field in ("enrichment_error", "last_error", "pending_reason"):
        title.pop(field, None)
    endpoint = f"/{'movie' if title['type'] == 'movie' else 'tv'}/{title['tmdb_id']}"
    try:
        details = await fetch_tmdb(
            endpoint,
            {"append_to_response": "external_ids,images", "include_image_language": "zh,null,en"},
            client=client,
        )
        title["title"] = details.get("title") or details.get("name") or title.get("title") or ""
        title["original_title"] = (
            details.get("original_title") or details.get("original_name")
            or title.get("original_title") or ""
        )
        title["overview"] = details.get("overview") or title.get("overview") or ""
        title["release_date"] = (
            details.get("release_date") or details.get("first_air_date")
            or title.get("release_date") or ""
        )
        title["poster_url"] = _poster_url(_localized_poster_path(details)) or title.get("poster_url")
        title["imdb_id"] = (details.get("external_ids") or {}).get("imdb_id")
        if not title["overview"]:
            english = await fetch_tmdb(endpoint, {"language": "en-US"}, client=client)
            if english.get("overview"):
                title["overview"] = await translate_to_chinese(english["overview"])
        title["last_synced_at"] = datetime.now(timezone.utc).isoformat()
        return title
    except Exception as exc:
        title["enrichment_error"] = f"{type(exc).__name__}: {exc}"
        return title


async def enrich_titles(candidates, cached_titles=None, progress_callback=None):
    cached_titles = cached_titles or {}
    stats = empty_fetch_stats()
    qualified = []
    pending = []
    needs_details = []

    for candidate in candidates:
        key = (candidate["type"], candidate["tmdb_id"])
        cached = cached_titles.get(key)
        if cached and _is_fresh(cached):
            qualified.append(_cached_title(candidate, cached))
            stats["cached"] += 1
        else:
            needs_details.append((candidate, cached))

    semaphore = asyncio.Semaphore(ENRICH_CONCURRENCY)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        async def fetch_one(candidate):
            async with semaphore:
                return await _fetch_details(candidate, client)

        enriched = await asyncio.gather(*(fetch_one(candidate) for candidate, _ in needs_details))
        imdb_ids = {title.get("imdb_id") for title in enriched if title.get("imdb_id")}
        ratings, rating_errors = await get_imdb_ratings(imdb_ids, client=client)

    for (candidate, cached), title in zip(needs_details, enriched):
        if title.get("enrichment_error"):
            stats["request_failed"] += 1
            stats["errors"].append(
                f"detail type={title['type']} tmdb_id={title['tmdb_id']}: {title['enrichment_error']}"
            )
            if cached:
                fallback = _cached_title(candidate, cached)
                fallback["stale_fallback"] = True
                qualified.append(fallback)
            else:
                title["pending_reason"] = "request_failed"
                title["last_error"] = title["enrichment_error"]
                pending.append(title)
            continue

        imdb_id = title.get("imdb_id")
        if not imdb_id:
            title["pending_reason"] = "missing_imdb_id"
            pending.append(title)
            stats["no_rating"] += 1
            continue

        rating_data = ratings.get(imdb_id)
        if not rating_data:
            error = rating_errors.get(imdb_id)
            title["pending_reason"] = "request_failed" if error else "missing_rating"
            title["last_error"] = error
            pending.append(title)
            if error:
                stats["request_failed"] += 1
                stats["errors"].append(f"rating imdb_id={imdb_id}: {error}")
            else:
                stats["no_rating"] += 1
            continue

        rating, votes, source = rating_data
        title["imdb_rating"] = rating
        title["rating_votes"] = votes
        title["rating_source"] = source
        if rating < MIN_IMDB_RATING:
            title["pending_reason"] = "low_rating"
            pending.append(title)
            stats["low_rating"] += 1
            continue
        qualified.append(title)

    stats["qualified"] = len(qualified)
    stats["pending"] = len(pending)
    await _notify_progress(
        progress_callback,
        phase="qualified",
        provider=None,
        provider_index=len(PROVIDERS),
        provider_total=len(PROVIDERS),
        provider_qualified=len(qualified),
        stats=dict(stats),
    )
    return {"titles": qualified, "pending": pending, "stats": stats}


async def enrich_with_imdb(title_data, client=None):
    result = await enrich_titles([title_data])
    return (result["titles"] or result["pending"] or [title_data])[0]


async def fetch_provider_titles(
    provider_name, days_back=1825, max_pages=29, window_days=90,
    provider_index=1, provider_total=1, client=None, progress_callback=None,
):
    discovered = await discover_provider(
        provider_name, days_back=days_back, max_pages=max_pages,
        window_days=window_days, client=client,
    )
    enriched = await enrich_titles(discovered["titles"])
    stats = enriched["stats"]
    stats["discovered"] = len(discovered["titles"])
    stats["unique_discovered"] = len(discovered["titles"])
    stats["errors"].extend(discovered["errors"])
    await _notify_progress(
        progress_callback,
        phase="qualified",
        provider=provider_name,
        provider_index=provider_index,
        provider_total=provider_total,
        provider_discovered=len(discovered["titles"]),
        provider_qualified=len(enriched["titles"]),
        stats=dict(stats),
    )
    return {"provider": provider_name, "titles": enriched["titles"], "pending": enriched["pending"], "stats": stats}


async def fetch_all_providers(days_back=1825, max_pages=29, window_days=90, progress_callback=None):
    discovered = await discover_all_providers(
        days_back=days_back, max_pages=max_pages, window_days=window_days,
        progress_callback=progress_callback,
    )
    enriched = await enrich_titles(discovered["titles"], progress_callback=progress_callback)
    stats = discovered["stats"]
    merge_fetch_stats(stats, enriched["stats"])
    stats["qualified"] = len(enriched["titles"])
    return {"titles": enriched["titles"], "pending": enriched["pending"], "stats": stats}
