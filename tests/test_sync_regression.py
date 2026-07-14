import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app import database, sync
from app.fetcher import empty_fetch_stats


class SyncRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_compensation_recovers_cook_soldier(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "tracker.db")
            target_date = date(2026, 5, 11)

            async def fake_discover(**kwargs):
                catalog_range = kwargs["catalog_range"]
                self.assertLessEqual(catalog_range[0], target_date)
                self.assertGreaterEqual(catalog_range[1], target_date)
                stats = empty_fetch_stats()
                stats["discovered"] = 1
                stats["unique_discovered"] = 1
                return {
                    "stats": stats,
                    "titles": [{
                        "tmdb_id": 295509,
                        "type": "tv",
                        "title": "菜鸟伙房兵",
                        "original_title": "취사병 전설이 되다",
                        "overview": "",
                        "release_date": target_date.isoformat(),
                        "poster_url": None,
                        "added_date": date.today().isoformat(),
                        "providers": ["max"],
                        "provider_regions": {"max": ["TW", "HK"]},
                        "discovery_channels": ["catalog_compensation"],
                    }],
                }

            async def fake_enrich(candidates, **_kwargs):
                title = dict(candidates[0])
                title.update({
                    "imdb_id": "tt38626513",
                    "imdb_rating": 8.1,
                    "rating_source": "imdb",
                    "rating_votes": 984,
                })
                stats = empty_fetch_stats()
                stats["qualified"] = 1
                return {"titles": [title], "pending": [], "stats": stats}

            with (
                patch.object(database, "DATABASE_URL", db_path),
                patch.object(sync, "TMDB_API_KEY", "test-key"),
                patch.object(sync, "SYNC_CATALOG_SCAN_ENABLED", True),
                patch.object(sync, "SYNC_CATALOG_SCAN_DAYS_BACK", 3650),
                patch.object(sync, "SYNC_CATALOG_WINDOW_DAYS", 365),
                patch.object(sync, "discover_all_providers", side_effect=fake_discover),
                patch.object(sync, "enrich_titles", side_effect=fake_enrich),
            ):
                result = await sync.sync_new_titles(
                    days_back=30, max_pages=5, window_days=0, reason="manual",
                )

            self.assertEqual(result["inserted"], 1)
            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    "SELECT tmdb_id, title FROM titles WHERE tmdb_id=295509 AND type='tv'"
                ).fetchone()
            self.assertEqual(row, (295509, "菜鸟伙房兵"))


if __name__ == "__main__":
    unittest.main()
