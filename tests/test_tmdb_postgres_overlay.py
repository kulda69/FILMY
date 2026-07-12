from __future__ import annotations

import unittest
from unittest.mock import patch

from filmy import db


class TmdbPostgresOverlayTests(unittest.TestCase):
    def test_get_tmdb_mapping_reads_postgres_when_enabled(self) -> None:
        with (
            patch("filmy.db.tmdb_backend_uses_postgres", return_value=True),
            patch(
                "filmy.db.fetch_tmdb_mapping_record",
                return_value={"tconst": "tt1", "tmdb_media_type": "movie", "tmdb_id": 10, "matched_by": "imdb_id", "matched_at": None, "sync_status": "synced", "last_error": None},
            ),
        ):
            row = db.get_tmdb_mapping("tt1")
        self.assertEqual(row["tmdb_id"], 10)

    def test_store_tmdb_payloads_writes_postgres_when_enabled(self) -> None:
        with (
            patch("filmy.db.tmdb_backend_uses_postgres", return_value=True),
            patch("filmy.db.store_tmdb_payload_bundle") as store_mock,
            patch("filmy.db.clear_title_presentation_cache"),
        ):
            db.store_tmdb_payloads(
                "tt1",
                "en-US",
                {"title": "Alpha", "overview": "x", "genres": [], "poster_path": "/p.jpg", "backdrop_path": "/b.jpg"},
                {"results": {"CZ": {"flatrate": [{"provider_id": 1, "provider_name": "Netflix", "logo_path": "/l.png", "display_priority": 10}]}}},
            )
        store_mock.assert_called_once()

    def test_fetch_tmdb_reads_postgres_when_enabled(self) -> None:
        fake_snapshot = {
            "mapping": {"tmdb_media_type": "movie", "tmdb_id": 10, "matched_by": "imdb_id", "matched_at": None, "sync_status": "synced", "last_error": None},
            "details": {"locale": "en-US", "display_title": "Alpha"},
            "detail_locales": ["en-US", "cs-CZ"],
            "providers": [{"provider_type": "flatrate", "provider_name": "Netflix", "logo_path": "/x"}],
            "assets": [{"asset_kind": "poster"}],
        }
        with (
            patch("filmy.db.tmdb_backend_uses_postgres", return_value=True),
            patch("filmy.db.fetch_tmdb_payload_snapshot", return_value=fake_snapshot),
        ):
            row = db._fetch_tmdb(None, "tt1")
        self.assertEqual(row["tmdb_id"], 10)
        self.assertEqual(row["detail_locales"][0], "en-US")

    def test_tmdb_targets_filter_postgres_completion_when_enabled(self) -> None:
        class _FakeDuckConn:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with (
            patch("filmy.db.tmdb_backend_uses_postgres", return_value=True),
            patch("filmy.db.duckdb.connect", return_value=_FakeDuckConn()),
            patch("filmy.db.content_state_uses_postgres", return_value=False),
            patch("filmy.db.watch_events_uses_postgres", return_value=False),
            patch("filmy.db.user_lists_uses_postgres", return_value=False),
            patch(
                "filmy.db._get_tmdb_duckdb_enrichment_items",
                return_value=[{"tconst": "tt1", "title_type": "movie", "primary_title": "Alpha", "start_year": 2024, "priority": 1, "reasons": ["watchlist"]}],
            ),
            patch("filmy.db._tmdb_status_is_complete", return_value=False),
            patch("filmy.db.get_tmdb_mapping", return_value={"sync_status": "synced"}),
        ):
            items = db.get_tmdb_enrichment_targets(include_complete=False)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tconst"], "tt1")


if __name__ == "__main__":
    unittest.main()
