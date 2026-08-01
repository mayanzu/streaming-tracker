"""聚合发现入口：IMDb 单条导入解析 + 全平台发现编排。"""

import httpx

from app.config import DISCOVER_ALL_PROVIDERS, PROVIDERS, TMDB_API_KEY
from app.fetcher.common import (
    _base_candidate_from_item,
    _merge_candidate,
    _notify_progress,
    _provider_availability,
    empty_fetch_stats,
    normalize_imdb_id,
)
from app.fetcher.providers import discover_provider, discover_unconfigured_providers
from app.fetcher.tmdb import fetch_tmdb


async def discover_imdb_title(imdb_reference, client=None):
    """Resolve one IMDb title through TMDB without relying on provider discovery."""
    imdb_id = normalize_imdb_id(imdb_reference)
    if not TMDB_API_KEY:
        raise ValueError("TMDB_API_KEY is required")

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    try:
        found = await fetch_tmdb(
            f"/find/{imdb_id}",
            {"external_source": "imdb_id"},
            client=client,
        )
        matches = [
            ("tv", item) for item in (found.get("tv_results") or [])
        ] + [
            ("movie", item) for item in (found.get("movie_results") or [])
        ]
        if not matches:
            return None

        media_type, item = max(
            matches,
            key=lambda match: float(match[1].get("popularity") or 0),
        )
        candidate = _base_candidate_from_item(item, media_type, "imdb_import")
        candidate["imdb_id"] = imdb_id
        watch = await fetch_tmdb(
            f"/{media_type}/{candidate['tmdb_id']}/watch/providers",
            {"language": "en-US"},
            client=client,
        )
        providers, provider_regions = _provider_availability(watch)
        candidate["providers"] = providers
        candidate["provider_regions"] = provider_regions
        return candidate
    finally:
        if owns_client:
            await client.aclose()


async def discover_all_providers(
    days_back=30, max_pages=5, window_days=0, catalog_range=None, progress_callback=None,
):
    merged = {}
    stats = empty_fetch_stats()
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        provider_total = len(PROVIDERS) + int(DISCOVER_ALL_PROVIDERS)
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
        if DISCOVER_ALL_PROVIDERS:
            result = await discover_unconfigured_providers(
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
                provider="all_providers",
                provider_index=provider_total,
                provider_total=provider_total,
                provider_discovered=len(result["titles"]),
                stats=dict(stats),
            )
    return {"titles": list(merged.values()), "stats": stats}
