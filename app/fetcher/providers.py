"""平台发现：按提供商/地区发现近期上线作品（discover 分页 + 递归切窗）。"""

import asyncio
import logging
from datetime import timedelta

import httpx

from app.config import (
    ALL_PROVIDER_WATCH_REGIONS,
    DEFAULT_PROVIDER_REGIONS,
    DISCOVER_CONCURRENCY,
    PROVIDER_REGIONS,
    PROVIDERS,
    TMDB_API_KEY,
    WATCH_MONETIZATION_TYPES,
)
from app.fetcher.common import _candidate_from_item, _date_ranges, _merge_candidate
from app.fetcher.tmdb import fetch_tmdb

logger = logging.getLogger(__name__)


async def _discover_range(
    client, semaphore, provider_name, provider_id, region, media_type,
    date_field, sort_field, range_start, range_end, max_pages, channel,
):
    async def fetch_page(page):
        params = {
            "watch_region": region,
            "with_watch_providers": provider_id,
            "with_watch_monetization_types": WATCH_MONETIZATION_TYPES,
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


async def discover_unconfigured_providers(
    days_back=30, max_pages=5, window_days=0, client=None, catalog_range=None,
):
    """Discover titles carried by any TMDB watch provider not in the static catalog."""
    if not TMDB_API_KEY:
        raise ValueError("TMDB_API_KEY is required")

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    semaphore = asyncio.Semaphore(DISCOVER_CONCURRENCY)
    recent_ranges = _date_ranges(days_back, window_days)
    media_specs = (
        ("movie", "release_date", "release_date.desc", "movie_release"),
        ("tv", "first_air_date", "first_air_date.desc", "tv_premiere"),
        ("tv", "air_date", "popularity.desc", "tv_current_airing"),
    )

    async def provider_ids_for(region, media_type):
        statically_covered_ids = {
            provider_id for provider_name, provider_id in PROVIDERS.items()
            if region in (
                PROVIDER_REGIONS.get(provider_name)
                or DEFAULT_PROVIDER_REGIONS[provider_name]
            )
        }
        async with semaphore:
            payload = await fetch_tmdb(
                f"/watch/providers/{media_type}",
                {"watch_region": region, "language": "en-US"},
                client=client,
            )
        return [
            int(item["provider_id"])
            for item in (payload.get("results") or [])
            if item.get("provider_id") is not None
            and int(item["provider_id"]) not in statically_covered_ids
        ]

    try:
        directory_tasks = {
            (region, media_type): asyncio.create_task(provider_ids_for(region, media_type))
            for region in ALL_PROVIDER_WATCH_REGIONS
            for media_type in ("movie", "tv")
        }
        directories = {}
        errors = []
        for key, task in directory_tasks.items():
            try:
                directories[key] = await task
            except Exception as exc:
                region, media_type = key
                errors.append(
                    f"provider_directory region={region} type={media_type}: "
                    f"{type(exc).__name__}: {exc}"
                )
                directories[key] = []

        tasks = []
        labels = []
        for region in ALL_PROVIDER_WATCH_REGIONS:
            for media_type, date_field, sort_field, channel in media_specs:
                provider_ids = directories.get((region, media_type)) or []
                if not provider_ids:
                    continue
                provider_filter = "|".join(str(value) for value in sorted(set(provider_ids)))
                for range_start, range_end in recent_ranges:
                    tasks.append(_discover_range(
                        client, semaphore, None, provider_filter, region, media_type,
                        date_field, sort_field, range_start, range_end, max_pages,
                        f"all_providers_{channel}",
                    ))
                    labels.append((region, media_type, channel, range_start, range_end))
                if catalog_range and channel in {"movie_release", "tv_premiere"}:
                    range_start, range_end = catalog_range
                    tasks.append(_discover_range(
                        client, semaphore, None, provider_filter, region, media_type,
                        date_field, sort_field, range_start, range_end, max_pages,
                        "all_providers_catalog_compensation",
                    ))
                    labels.append((region, media_type, "catalog_compensation", range_start, range_end))

        merged = {}
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for label, result in zip(labels, results):
            if isinstance(result, Exception):
                region, media_type, channel, range_start, range_end = label
                errors.append(
                    f"all_providers region={region} type={media_type} channel={channel} "
                    f"dates={range_start}:{range_end}: {type(result).__name__}: {result}"
                )
                continue
            candidates, query_errors = result
            errors.extend(query_errors)
            for candidate in candidates:
                key = (candidate["type"], candidate["tmdb_id"])
                if key in merged:
                    _merge_candidate(merged[key], candidate)
                else:
                    merged[key] = candidate
        return {"provider": "all_providers", "titles": list(merged.values()), "errors": errors}
    finally:
        if owns_client:
            await client.aclose()
