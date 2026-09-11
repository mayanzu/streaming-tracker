"""作品富化：详情抓取、评分合并、缓存判断与整体编排（discover → enrich → 结果）。"""

import asyncio
import json
from datetime import datetime, timezone

import httpx

from app.config import (
    ENRICH_BATCH_SIZE,
    ENRICH_CONCURRENCY,
    MIN_IMDB_RATING,
    TMDB_API_KEY,
)
from app.fetcher.common import (
    _cached_title,
    _contains_cjk,
    _is_fresh,
    _is_in_grace_period,
    _is_recent,
    _localized_poster_path,
    _min_votes_for,
    _notify_progress,
    _origin_countries_from_details,
    _poster_url,
    _provider_availability,
    empty_fetch_stats,
    merge_fetch_stats,
    translate_to_chinese,
)
from app.fetcher.discover import discover_all_providers
from app.fetcher.providers import discover_provider
from app.fetcher.ratings import get_imdb_ratings
from app.fetcher.tmdb import fetch_tmdb


async def _fetch_details(candidate, client):
    title = dict(candidate)
    for field in ("enrichment_error", "last_error", "pending_reason"):
        title.pop(field, None)
    endpoint = f"/{'movie' if title['type'] == 'movie' else 'tv'}/{title['tmdb_id']}"
    try:
        details = await fetch_tmdb(
            endpoint,
            {
                "append_to_response": "external_ids,images,watch/providers,credits,videos",
                "include_image_language": "zh,null,en",
            },
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
        title["imdb_id"] = (
            (details.get("external_ids") or {}).get("imdb_id")
            or title.get("imdb_id")
        )
        providers, provider_regions = _provider_availability(
            details.get("watch/providers") or {}
        )
        if providers:
            title["providers"] = list(dict.fromkeys(
                (title.get("providers") or []) + providers
            ))
            regions = title.setdefault("provider_regions", {})
            for provider, values in provider_regions.items():
                regions[provider] = list(dict.fromkeys(
                    (regions.get(provider) or []) + values
                ))
        title["origin_countries"] = _origin_countries_from_details(details)
        title["director"], title["cast_json"] = _credits_from_details(details)
        title["genres_json"] = _genres_from_details(details)
        title["runtime"], title["seasons"], title["episodes"] = _runtime_from_details(details, title["type"])
        title["trailer_key"] = _trailer_from_details(details)
        if not _contains_cjk(title["overview"]):
            # TMDB 无中文译文时会回退原文；这里对原文做机翻兜底，保证展示中文简介。
            if not title["overview"]:
                english = await fetch_tmdb(endpoint, {"language": "en-US"}, client=client)
                title["overview"] = english.get("overview") or ""
            if title["overview"]:
                title["overview"] = await translate_to_chinese(title["overview"])
        if not _contains_cjk(title["title"]) and not _contains_cjk(title["original_title"]):
            # 标题同理：原生中文作品（original_title 含 CJK）不翻译，避免中文名被二次翻译。
            title["title"] = await translate_to_chinese(title["title"])
        synced_at = datetime.now(timezone.utc).isoformat()
        title["last_synced_at"] = synced_at
        title["countries_synced_at"] = synced_at
        return title
    except Exception as exc:
        title["enrichment_error"] = f"{type(exc).__name__}: {exc}"
        return title


def _credits_from_details(details):
    """返回 (director, cast_json)：导演取 credits.crew 中 job 为 Director 的第一人，演员取 cast 前8名。"""
    credits = details.get("credits") or {}
    director = ""
    for person in credits.get("crew") or []:
        if str(person.get("job") or "").lower() == "director":
            director = person.get("name") or ""
            break
    cast = [p.get("name") for p in (credits.get("cast") or []) if p.get("name")][:8]
    return director, json.dumps(cast, ensure_ascii=False)


def _genres_from_details(details):
    genres = [g.get("name") for g in (details.get("genres") or []) if g.get("name")]
    return json.dumps(genres, ensure_ascii=False)


def _runtime_from_details(details, media_type):
    runtime, seasons, episodes = None, None, None
    if media_type == "movie":
        runtime = details.get("runtime") or None
    else:
        runtimes = details.get("episode_run_time") or []
        runtime = runtimes[0] if runtimes else None
        seasons = details.get("number_of_seasons") or None
        episodes = details.get("number_of_episodes") or None
    return runtime, seasons, episodes


def _trailer_from_details(details):
    for video in (details.get("videos") or {}).get("results") or []:
        if video.get("type") == "Trailer" and video.get("site") == "YouTube" and video.get("key"):
            return video["key"]
    return None


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

    if not needs_details:
        stats["qualified"] = len(qualified)
        if candidates:
            await _notify_progress(
                progress_callback,
                phase="qualified",
                provider=None,
                provider_index=0,
                provider_total=0,
                enrich_completed=0,
                enrich_total=0,
                provider_qualified=len(qualified),
                stats=dict(stats),
            )
        return {"titles": qualified, "pending": pending, "stats": stats}

    semaphore = asyncio.Semaphore(ENRICH_CONCURRENCY)
    enrich_total = len(needs_details)
    enriched = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        async def fetch_one(candidate):
            async with semaphore:
                return await _fetch_details(candidate, client)

        for batch_start in range(0, enrich_total, ENRICH_BATCH_SIZE):
            batch = needs_details[batch_start:batch_start + ENRICH_BATCH_SIZE]
            enriched.extend(
                await asyncio.gather(*(fetch_one(candidate) for candidate, _ in batch))
            )
            enrich_completed = batch_start + len(batch)
            if enrich_completed < enrich_total:
                await _notify_progress(
                    progress_callback,
                    phase="enriching",
                    provider=None,
                    provider_index=enrich_completed,
                    provider_total=enrich_total,
                    enrich_completed=enrich_completed,
                    enrich_total=enrich_total,
                    provider_qualified=len(qualified),
                    stats=dict(stats),
                )

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
            if _is_in_grace_period(title):
                qualified.append(title)
                continue
            title["pending_reason"] = "missing_imdb_id"
            pending.append(title)
            stats["no_rating"] += 1
            continue

        rating_data = ratings.get(imdb_id)
        if not rating_data:
            error = rating_errors.get(imdb_id)
            if error:
                title["pending_reason"] = "request_failed"
                title["last_error"] = error
                pending.append(title)
                stats["request_failed"] += 1
                stats["errors"].append(f"rating imdb_id={imdb_id}: {error}")
            elif _is_in_grace_period(title):
                qualified.append(title)
            elif _is_recent(title):
                title["pending_reason"] = "awaiting_rating"
                pending.append(title)
                stats["no_rating"] += 1
            else:
                title["pending_reason"] = "missing_rating"
                pending.append(title)
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
        min_votes = _min_votes_for(title)
        if votes < min_votes:
            title["pending_reason"] = "insufficient_votes"
            pending.append(title)
            stats["no_rating"] += 1
            continue
        qualified.append(title)

    stats["qualified"] = len(qualified)
    stats["pending"] = len(pending)
    await _notify_progress(
        progress_callback,
        phase="qualified",
        provider=None,
        provider_index=enrich_total,
        provider_total=enrich_total,
        enrich_completed=enrich_total,
        enrich_total=enrich_total,
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
