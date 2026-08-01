"""外部 API 基础调用：TMDB 通用请求（含重试与鉴权）。"""

import asyncio

import httpx

from app.config import HTTP_RETRIES, TMDB_API_KEY, TMDB_BASE_URL
from app.fetcher.common import RETRYABLE_STATUS_CODES, ExternalRequestError, _retry_delay


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
