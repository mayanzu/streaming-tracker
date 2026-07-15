import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import fetcher, imdb_data


class FetcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_adaptive_split_avoids_multi_day_truncation(self):
        async def fake_fetch(_endpoint, params=None, **_kwargs):
            start = date.fromisoformat(params["release_date.gte"])
            end = date.fromisoformat(params["release_date.lte"])
            if start != end:
                return {"total_pages": 10, "results": []}
            return {
                "total_pages": 1,
                "results": [{"id": start.toordinal(), "title": start.isoformat()}],
            }

        with patch.object(fetcher, "fetch_tmdb", side_effect=fake_fetch):
            start = date(2026, 5, 10)
            end = date(2026, 5, 12)
            titles, errors = await fetcher._discover_range(
                object(), __import__("asyncio").Semaphore(2), "max", 1899, "TW",
                "movie", "release_date", "release_date.desc", start, end, 5,
                "movie_release",
            )
        self.assertEqual(len(titles), 3)
        self.assertEqual(errors, [])

    async def test_tv_air_date_and_catalog_channels_are_queried(self):
        calls = []

        async def fake_range(*args):
            calls.append(args[-1])
            return [], []

        target = date(2026, 5, 11)
        with (
            patch.object(fetcher, "TMDB_API_KEY", "test-key"),
            patch.object(fetcher, "PROVIDERS", {"max": 1899}),
            patch.object(fetcher, "PROVIDER_REGIONS", {"max": ("TW",)}),
            patch.object(fetcher, "_discover_range", side_effect=fake_range),
        ):
            await fetcher.discover_provider(
                "max", days_back=1, max_pages=1, client=object(),
                catalog_range=(target, target),
            )
        self.assertIn("tv_current_airing", calls)
        self.assertEqual(calls.count("catalog_compensation"), 2)

    async def test_global_dedupe_merges_providers_before_enrichment(self):
        async def fake_provider(name, **_kwargs):
            return {
                "provider": name,
                "errors": [],
                "titles": [{
                    "tmdb_id": 295509, "type": "tv", "title": "菜鸟伙房兵",
                    "providers": [name], "provider_regions": {name: ["TW"]},
                    "discovery_channels": ["catalog_compensation"],
                }],
            }

        with (
            patch.object(fetcher, "PROVIDERS", {"max": 1, "disney": 2}),
            patch.object(fetcher, "discover_provider", side_effect=fake_provider),
        ):
            result = await fetcher.discover_all_providers()
        self.assertEqual(len(result["titles"]), 1)
        self.assertEqual(set(result["titles"][0]["providers"]), {"max", "disney"})

    def test_posterless_candidate_is_retained(self):
        candidate = fetcher._candidate_from_item(
            {"id": 1, "name": "无海报作品", "poster_path": None},
            "tv", "max", "TW", "tv_current_airing",
        )
        self.assertIsNone(candidate["poster_url"])
        self.assertEqual(candidate["tmdb_id"], 1)

    def test_tv_candidate_preserves_origin_country(self):
        candidate = fetcher._candidate_from_item(
            {"id": 2, "name": "日剧", "origin_country": ["jp", "US"]},
            "tv", "max", "TW", "tv_current_airing",
        )
        self.assertEqual(candidate["origin_countries"], ["JP", "US"])

    def test_detail_country_prefers_tv_origin_country(self):
        details = {
            "origin_country": ["JP"],
            "production_countries": [{"iso_3166_1": "US"}],
        }
        self.assertEqual(fetcher._origin_countries_from_details(details), ["JP"])

    def test_detail_country_falls_back_to_movie_production_country(self):
        details = {
            "production_countries": [
                {"iso_3166_1": "kr"},
                {"iso_3166_1": "US"},
            ],
        }
        self.assertEqual(fetcher._origin_countries_from_details(details), ["KR", "US"])

    async def test_imdb_scan_keeps_only_requested_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ratings.tsv"
            path.write_text(
                "tconst\taverageRating\tnumVotes\n"
                "tt0000001\t5.0\t10\n"
                "tt38626513\t8.1\t984\n"
                "tt9999999\t9.0\t9999\n",
                encoding="utf-8",
            )
            with patch.object(imdb_data, "_ensure_dataset_sync", return_value=path):
                result = await imdb_data.get_ratings({"tt38626513"})
        self.assertEqual(result, {"tt38626513": (8.1, 984)})


if __name__ == "__main__":
    unittest.main()
