import unittest
from unittest.mock import AsyncMock, patch

from app import importer


class ImporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_qualified_title_is_persisted(self):
        candidate = {
            "tmdb_id": 292121,
            "imdb_id": "tt39245629",
            "type": "tv",
            "title": "稻草人",
            "providers": ["others"],
            "provider_regions": {"others": ["KR", "SG", "US", "CA"]},
        }
        qualified = dict(candidate, imdb_rating=8.3, rating_votes=1285, rating_source="imdb")
        with (
            patch.object(importer, "discover_imdb_title", new=AsyncMock(return_value=candidate)),
            patch.object(
                importer,
                "enrich_titles",
                new=AsyncMock(return_value={"titles": [qualified], "pending": []}),
            ),
            patch.object(
                importer,
                "persist_sync_batch",
                return_value={
                    "processed": 1, "skipped": 0, "inserted": 1,
                    "updated": 0, "unchanged": 0, "errors": [],
                },
            ) as persist,
        ):
            result = await importer.import_title_by_imdb(
                "https://www.imdb.com/title/tt39245629/"
            )

        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["action"], "inserted")
        self.assertEqual(result["imdb_rating"], 8.3)
        persist.assert_called_once_with([qualified], [])

    async def test_import_unrated_title_is_saved_as_pending(self):
        candidate = {
            "tmdb_id": 1, "imdb_id": "tt0000001", "type": "movie",
            "title": "Pending", "providers": [], "provider_regions": {},
        }
        pending = dict(candidate, pending_reason="missing_rating")
        with (
            patch.object(importer, "discover_imdb_title", new=AsyncMock(return_value=candidate)),
            patch.object(
                importer,
                "enrich_titles",
                new=AsyncMock(return_value={"titles": [], "pending": [pending]}),
            ),
            patch.object(
                importer,
                "persist_sync_batch",
                return_value={
                    "processed": 0, "skipped": 0, "inserted": 0,
                    "updated": 0, "unchanged": 0, "errors": [],
                },
            ) as persist,
        ):
            result = await importer.import_title_by_imdb("tt0000001")

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["pending_reason"], "missing_rating")
        persist.assert_called_once_with([], [pending])

    async def test_import_reports_tmdb_miss(self):
        with patch.object(
            importer, "discover_imdb_title", new=AsyncMock(return_value=None),
        ):
            with self.assertRaises(importer.TitleImportError) as context:
                await importer.import_title_by_imdb("tt39245629")
        self.assertEqual(context.exception.code, "tmdb_not_found")


if __name__ == "__main__":
    unittest.main()
