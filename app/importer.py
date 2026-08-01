"""Idempotent title import workflow for IMDb IDs and title URLs."""

import asyncio

from app.db import persist_sync_batch
from app.fetcher import (
    ExternalRequestError,
    discover_imdb_title,
    enrich_titles,
    normalize_imdb_id,
)


class TitleImportError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_import_lock = asyncio.Lock()


async def import_title_by_imdb(imdb_reference):
    """Resolve, enrich and persist one title independently of provider discovery."""
    try:
        imdb_id = normalize_imdb_id(imdb_reference)
    except ValueError as exc:
        raise TitleImportError("invalid_imdb_id", "IMDb ID 或作品链接格式无效") from exc

    async with _import_lock:
        try:
            candidate = await discover_imdb_title(imdb_id)
        except ValueError as exc:
            raise TitleImportError("missing_tmdb_api_key", str(exc)) from exc
        except ExternalRequestError as exc:
            raise TitleImportError(
                "external_request_failed",
                f"TMDB 请求失败：{exc}",
            ) from exc

        if not candidate:
            raise TitleImportError(
                "tmdb_not_found",
                f"TMDB 尚未收录或尚未关联 {imdb_id}",
            )

        enriched = await enrich_titles([candidate])
        qualified = enriched["titles"]
        pending = enriched["pending"]
        persistence = await asyncio.to_thread(
            persist_sync_batch,
            qualified,
            pending,
        )
        if persistence["skipped"] or persistence["errors"]:
            detail = "; ".join(persistence["errors"]) or "unknown persistence error"
            raise TitleImportError("persistence_failed", f"作品保存失败：{detail}")

        if not qualified and not pending:
            raise TitleImportError(
                "persistence_failed", "作品既未入库也未进入待处理队列，请稍后重试"
            )
        title = (qualified or pending)[0]
        if qualified:
            action = next(
                (
                    name for name in ("inserted", "updated", "unchanged")
                    if persistence.get(name)
                ),
                "unchanged",
            )
            status = "imported"
        else:
            action = "pending"
            status = "pending"

        return {
            "status": status,
            "action": action,
            "imdb_id": imdb_id,
            "tmdb_id": title["tmdb_id"],
            "type": title["type"],
            "title": title.get("title") or "",
            "imdb_rating": title.get("imdb_rating"),
            "rating_votes": title.get("rating_votes"),
            "providers": title.get("providers") or [],
            "provider_regions": title.get("provider_regions") or {},
            "pending_reason": title.get("pending_reason"),
        }
