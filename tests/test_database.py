import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import database


def title_payload(**overrides):
    payload = {
        "tmdb_id": 100,
        "imdb_id": "tt0000100",
        "title": "测试作品",
        "original_title": "Test Title",
        "type": "tv",
        "overview": "overview",
        "release_date": "2026-05-11",
        "poster_url": None,
        "imdb_rating": 8.1,
        "rating_source": "imdb",
        "rating_votes": 984,
        "added_date": "2026-06-01",
        "providers": ["max"],
        "provider_regions": {"max": ["TW", "HK"]},
        "origin_countries": ["JP"],
        "countries_synced_at": "2026-07-15T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "tracker.db")
        self.db_patch = patch.object(database, "DATABASE_URL", self.db_path)
        self.db_patch.start()
        database.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def test_update_preserves_original_added_date(self):
        database.insert_title(title_payload())
        database.insert_title(title_payload(title="更新后的标题", added_date="2026-07-14"))

        with self.connect() as connection:
            row = connection.execute(
                "SELECT title, added_date FROM titles WHERE tmdb_id=100 AND type='tv'"
            ).fetchone()
        self.assertEqual(row["title"], "更新后的标题")
        self.assertEqual(row["added_date"], "2026-06-01")

    def test_batch_persistence_and_provider_expiry_are_atomic(self):
        outcome = database.persist_sync_batch([title_payload()], [])
        self.assertEqual(outcome["inserted"], 1)
        self.assertEqual(outcome["skipped"], 0)

        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        with self.connect() as connection:
            connection.execute(
                "UPDATE title_provider_availability SET last_seen_at=?", (old,)
            )
        outcome = database.persist_sync_batch([], [], provider_stale_days=45)
        self.assertEqual(outcome["provider_expired"], 2)

        with self.connect() as connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM title_provider_availability WHERE is_active=1"
            ).fetchone()[0]
        self.assertEqual(active, 0)

    def test_pending_title_is_scheduled_and_later_promoted(self):
        pending = title_payload(
            imdb_rating=None,
            rating_source=None,
            rating_votes=None,
            pending_reason="missing_rating",
        )
        outcome = database.persist_sync_batch([], [pending])
        self.assertEqual(outcome["skipped"], 0)

        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempt_count, next_retry_at FROM pending_titles"
            ).fetchone()
        self.assertEqual(row["attempt_count"], 1)
        self.assertGreater(datetime.fromisoformat(row["next_retry_at"]), datetime.now(timezone.utc))

        outcome = database.persist_sync_batch([title_payload()], [])
        self.assertEqual(outcome["inserted"], 1)
        with self.connect() as connection:
            pending_count = connection.execute("SELECT COUNT(*) FROM pending_titles").fetchone()[0]
        self.assertEqual(pending_count, 0)

    def test_catalog_window_starts_immediately_before_recent_range(self):
        range_start, range_end = database.claim_catalog_window(3650, 365, 30)
        target = datetime(2026, 5, 11).date()
        self.assertLessEqual(range_start, target)
        self.assertGreaterEqual(range_end, target)

    def test_provider_filter_uses_correlated_exists_without_catalog_join(self):
        where_sql, params = database._build_title_filters(provider="max")

        self.assertIn("EXISTS", where_sql)
        self.assertIn("provider_filter.title_id = t.id", where_sql)
        self.assertNotIn("tp.provider_name", where_sql)
        self.assertEqual(params, ["max"])

    def test_title_listing_filters_provider_without_duplicate_catalog_rows(self):
        database.insert_title(title_payload())
        database.insert_title(title_payload(
            tmdb_id=101,
            imdb_id="tt0000101",
            title="另一个作品",
            providers=["netflix"],
            provider_regions={"netflix": ["US", "GB", "JP"]},
        ))

        all_titles = database.get_titles(limit=20)
        max_titles = database.get_titles(limit=20, provider="max")

        self.assertEqual(all_titles["total"], 2)
        self.assertEqual(len(all_titles["titles"]), 2)
        self.assertEqual(max_titles["total"], 1)
        self.assertEqual(max_titles["titles"][0]["tmdb_id"], 100)

    def test_country_rows_are_replaced_when_title_is_updated(self):
        database.insert_title(title_payload(origin_countries=["JP", "US"]))
        database.insert_title(title_payload(origin_countries=["KR"]))

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT country_code FROM title_countries ORDER BY country_code"
            ).fetchall()
        self.assertEqual([row["country_code"] for row in rows], ["KR"])

    def test_region_filter_and_stats_use_normalized_country_table(self):
        database.insert_title(title_payload(origin_countries=["JP"]))
        database.insert_title(title_payload(
            tmdb_id=101,
            imdb_id="tt0000101",
            title="韩国作品",
            origin_countries=["KR"],
        ))

        japanese = database.get_titles(limit=20, region="jp")
        stats = database.get_stats()
        region_counts = {
            item["country_code"]: item["count"] for item in stats["regions"]
        }

        self.assertEqual(japanese["total"], 1)
        self.assertEqual(japanese["titles"][0]["tmdb_id"], 100)
        self.assertEqual(japanese["titles"][0]["origin_countries"], ["JP"])
        self.assertEqual(region_counts, {"JP": 1, "KR": 1})

    def test_region_filter_uses_correlated_exists(self):
        where_sql, params = database._build_title_filters(region="jp")

        self.assertIn("EXISTS", where_sql)
        self.assertIn("country_filter.title_id = t.id", where_sql)
        self.assertEqual(params, ["JP"])


if __name__ == "__main__":
    unittest.main()
