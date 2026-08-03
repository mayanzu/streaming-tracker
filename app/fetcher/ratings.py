"""IMDb 评分获取：OMDb API 兜底 + IMDb 数据集本地查询。"""

import asyncio
import logging

import httpx

from app.config import (
    ENRICH_CONCURRENCY,
    HTTP_RETRIES,
    OMDB_API_KEY,
    OMDB_BASE_URL,
)
from app.fetcher.common import RETRYABLE_STATUS_CODES, ExternalRequestError, _retry_delay

logger = logging.getLogger(__name__)


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
        if rating is not None:
            resolved[imdb_id] = (rating, votes, "imdb")

    missing = requested - set(resolved)
    if not missing or not OMDB_API_KEY:
        return resolved, errors

    semaphore = asyncio.Semaphore(ENRICH_CONCURRENCY)

    async def fetch_one(imdb_id):
        try:
            async with semaphore:
                rating, votes = await fetch_omdb(imdb_id, client=client)
            if rating is not None:
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
