import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import fetcher, imdb_data


class FetcherTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _enrich_candidates(count):
        return [
            {
                "tmdb_id": index,
                "type": "movie",
                "title": f"Title {index}",
                "providers": ["netflix"],
            }
            for index in range(1, count + 1)
        ]

    async def test_enrich_titles_processes_batches_and_reports_progress(self):
        rating_batches = []
        progress_updates = []
        detail_calls = []

        async def fake_details(candidate, _client):
            detail_calls.append(candidate["tmdb_id"])
            title = dict(candidate)
            title["imdb_id"] = f"tt{candidate['tmdb_id']:07d}"
            return title

        async def fake_ratings(imdb_ids, client=None):
            rating_batches.append(sorted(imdb_ids))
            return ({imdb_id: (8.0, 1000, "imdb") for imdb_id in imdb_ids}, {})

        with (
            patch.object(fetcher, "ENRICH_BATCH_SIZE", 2),
            patch.object(fetcher, "_fetch_details", side_effect=fake_details),
            patch.object(fetcher, "get_imdb_ratings", side_effect=fake_ratings),
        ):
            result = await fetcher.enrich_titles(
                self._enrich_candidates(5),
                progress_callback=progress_updates.append,
            )

        self.assertEqual([len(batch) for batch in rating_batches], [5])
        self.assertEqual(
            [item["enrich_completed"] for item in progress_updates],
            [2, 4, 5],
        )
        self.assertEqual(detail_calls, [1, 2, 3, 4, 5])
        self.assertEqual([item["enrich_total"] for item in progress_updates], [5, 5, 5])
        self.assertEqual(
            [item["phase"] for item in progress_updates],
            ["enriching", "enriching", "qualified"],
        )
        self.assertEqual([item["tmdb_id"] for item in result["titles"]], [1, 2, 3, 4, 5])

    async def test_enrich_titles_exact_batch_reports_once(self):
        progress_updates = []

        async def fake_details(candidate, _client):
            title = dict(candidate)
            title["imdb_id"] = f"tt{candidate['tmdb_id']:07d}"
            return title

        async def fake_ratings(imdb_ids, client=None):
            return ({imdb_id: (8.0, 1000, "imdb") for imdb_id in imdb_ids}, {})

        with (
            patch.object(fetcher, "ENRICH_BATCH_SIZE", 2),
            patch.object(fetcher, "_fetch_details", side_effect=fake_details),
            patch.object(fetcher, "get_imdb_ratings", side_effect=fake_ratings),
        ):
            await fetcher.enrich_titles(
                self._enrich_candidates(2),
                progress_callback=progress_updates.append,
            )

        self.assertEqual(len(progress_updates), 1)
        self.assertEqual(progress_updates[0]["enrich_completed"], 2)
        self.assertEqual(progress_updates[0]["phase"], "qualified")

    async def test_enrich_titles_empty_input_does_not_report_progress(self):
        progress_updates = []

        result = await fetcher.enrich_titles([], progress_callback=progress_updates.append)

        self.assertEqual(result["titles"], [])
        self.assertEqual(result["pending"], [])
        self.assertEqual(progress_updates, [])

    async def test_enrich_titles_without_progress_callback_remains_supported(self):
        async def fake_details(candidate, _client):
            title = dict(candidate)
            title["imdb_id"] = "tt0000001"
            return title

        with (
            patch.object(fetcher, "_fetch_details", side_effect=fake_details),
            patch.object(
                fetcher,
                "get_imdb_ratings",
                new=AsyncMock(return_value=({"tt0000001": (8.0, 1000, "imdb")}, {})),
            ),
        ):
            result = await fetcher.enrich_titles(self._enrich_candidates(1))

        self.assertEqual(len(result["titles"]), 1)

    def test_normalize_imdb_id_accepts_id_and_url(self):
        self.assertEqual(fetcher.normalize_imdb_id("TT39245629"), "tt39245629")
        self.assertEqual(
            fetcher.normalize_imdb_id("https://www.imdb.com/title/tt39245629/"),
            "tt39245629",
        )
        with self.assertRaises(ValueError):
            fetcher.normalize_imdb_id("https://www.imdb.com/name/nm0000001/")

    def test_provider_availability_collapses_to_primary_and_others(self):
        providers, regions = fetcher._provider_availability({
            "results": {
                "US": {"flatrate": [
                    {"provider_id": 119, "provider_name": "Amazon Prime Video"},
                    {"provider_id": 9999, "provider_name": "Local Stream"},
                ]},
                "KR": {"flatrate": [
                    {"provider_id": 1883, "provider_name": "TVING"},
                ]},
            },
        })
        self.assertEqual(set(providers), {"amazon", "others"})
        self.assertEqual(regions["amazon"], ["US"])
        self.assertEqual(set(regions["others"]), {"US", "KR"})

    async def test_direct_imdb_discovery_resolves_regional_providers(self):
        calls = []

        async def fake_fetch(endpoint, params=None, **_kwargs):
            calls.append((endpoint, params))
            if endpoint == "/find/tt39245629":
                return {
                    "movie_results": [],
                    "tv_results": [{
                        "id": 292121,
                        "name": "稻草人",
                        "original_name": "허수아비",
                        "first_air_date": "2026-04-20",
                        "popularity": 50,
                    }],
                }
            if endpoint == "/tv/292121/watch/providers":
                return {
                    "results": {
                        "KR": {"flatrate": [{"provider_id": 1883}]},
                        "SG": {"ads": [{"provider_id": 158}]},
                        "US": {"flatrate": [{"provider_id": 344}]},
                    },
                }
            self.fail(f"unexpected endpoint: {endpoint}")

        with (
            patch.object(fetcher, "TMDB_API_KEY", "test-key"),
            patch.object(fetcher, "fetch_tmdb", side_effect=fake_fetch),
        ):
            candidate = await fetcher.discover_imdb_title(
                "https://www.imdb.com/title/tt39245629/",
                client=object(),
            )

        self.assertEqual(candidate["tmdb_id"], 292121)
        self.assertEqual(candidate["imdb_id"], "tt39245629")
        self.assertEqual(candidate["providers"], ["others"])
        self.assertEqual(
            set(candidate["provider_regions"]["others"]),
            {"KR", "SG", "US"},
        )
        self.assertEqual(calls[0][1]["external_source"], "imdb_id")

    async def test_detail_lookup_preserves_seeded_imdb_id(self):
        async def fake_fetch(_endpoint, params=None, **_kwargs):
            return {
                "id": 292121,
                "name": "稻草人",
                "original_name": "허수아비",
                "first_air_date": "2026-04-20",
                "external_ids": {},
                "images": {},
                "watch/providers": {
                    "results": {
                        "KR": {"flatrate": [{
                            "provider_id": 9999,
                            "provider_name": "Local Stream",
                        }]},
                    },
                },
                "origin_country": ["KR"],
                "overview": "已有简介",
            }

        candidate = {
            "tmdb_id": 292121,
            "imdb_id": "tt39245629",
            "type": "tv",
            "title": "稻草人",
            "providers": ["tving"],
        }
        with patch.object(fetcher, "fetch_tmdb", side_effect=fake_fetch):
            title = await fetcher._fetch_details(candidate, object())
        self.assertEqual(title["imdb_id"], "tt39245629")
        self.assertIn("others", title["providers"])
        self.assertEqual(title["provider_regions"]["others"], ["KR"])

    async def test_dynamic_discovery_queries_all_unconfigured_provider_ids(self):
        range_calls = []

        async def fake_fetch(endpoint, params=None, **_kwargs):
            self.assertTrue(endpoint.startswith("/watch/providers/"))
            self.assertEqual(params["watch_region"], "KR")
            return {"results": [
                {"provider_id": 8, "provider_name": "Netflix"},
                {"provider_id": 9999, "provider_name": "Local Stream"},
            ]}

        async def fake_range(*args):
            range_calls.append(args)
            media_type = args[5]
            return [fetcher._base_candidate_from_item(
                {"id": 42, "name": "Dynamic"}, media_type, "all_providers",
            )], []

        with (
            patch.object(fetcher, "TMDB_API_KEY", "test-key"),
            patch.object(fetcher, "PROVIDERS", {"netflix": 8}),
            patch.object(fetcher, "ALL_PROVIDER_WATCH_REGIONS", ("KR",)),
            patch.object(fetcher, "fetch_tmdb", side_effect=fake_fetch),
            patch.object(fetcher, "_discover_range", side_effect=fake_range),
        ):
            result = await fetcher.discover_unconfigured_providers(
                days_back=1, max_pages=1, client=object(),
            )

        self.assertEqual(len(range_calls), 3)
        self.assertTrue(all(call[2] is None for call in range_calls))
        self.assertTrue(all(call[3] == "9999" for call in range_calls))
        self.assertEqual(len(result["titles"]), 2)

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

    async def test_discover_uses_supported_monetization_parameter(self):
        observed = []

        async def fake_fetch(_endpoint, params=None, **_kwargs):
            observed.append(params)
            return {"total_pages": 1, "results": []}

        target = date(2026, 7, 1)
        with patch.object(fetcher, "fetch_tmdb", side_effect=fake_fetch):
            await fetcher._discover_range(
                object(), __import__("asyncio").Semaphore(1), "tving", 1883, "KR",
                "tv", "first_air_date", "first_air_date.desc", target, target, 1,
                "tv_premiere",
            )
        self.assertIn("with_watch_monetization_types", observed[0])
        self.assertNotIn("watch_monetization_types", observed[0])

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
            patch.object(fetcher, "DISCOVER_ALL_PROVIDERS", False),
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
