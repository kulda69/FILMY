from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from filmy import db


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return self.rows


class _FakeDuckConn:
    def __init__(self, rows):
        self._conn = _FakeConn(rows)

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        return False


class RuntimePostgresRuntimeTargetsTests(unittest.TestCase):
    def test_get_tmdb_enrichment_targets_merges_postgres_runtime_candidates(self) -> None:
        with (
            patch("filmy.db.duckdb.connect", return_value=_FakeDuckConn([])),
            patch("filmy.db._get_tmdb_duckdb_enrichment_items", return_value=[]),
            patch(
                "filmy.db._get_tmdb_postgres_runtime_items",
                return_value=[{"tconst": "tt1", "title_type": "movie", "primary_title": "Alpha", "start_year": 2024, "priority": 1, "reasons": ["watched_title"]}],
            ),
            patch("filmy.db.watch_events_uses_postgres", return_value=True),
            patch("filmy.db.content_state_uses_postgres", return_value=False),
            patch("filmy.db.user_lists_uses_postgres", return_value=False),
        ):
            items = db.get_tmdb_enrichment_targets(limit=5)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tconst"], "tt1")

    def test_get_title_detail_cache_targets_uses_postgres_runtime_candidates(self) -> None:
        with (
            patch("filmy.db.duckdb.connect", return_value=_FakeDuckConn([("tt1", "movie", "Alpha", 2024)])),
            patch("filmy.db._get_tmdb_duckdb_enrichment_items", return_value=[]),
            patch(
                "filmy.db._get_tmdb_postgres_runtime_items",
                return_value=[{"tconst": "tt1", "title_type": "movie", "primary_title": "Alpha", "start_year": 2024, "priority": 1, "reasons": ["watched_title"]}],
            ),
            patch("filmy.db.tmdb_backend_uses_postgres", return_value=False),
            patch("filmy.db.watch_events_uses_postgres", return_value=True),
            patch("filmy.db.content_state_uses_postgres", return_value=False),
            patch("filmy.db.user_lists_uses_postgres", return_value=False),
            patch("filmy.db._title_detail_cache_path", return_value=Path("/definitely-missing-codex-cache.json")),
        ):
            items = db.get_title_detail_cache_targets(limit=5)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tconst"], "tt1")
        self.assertEqual(items[0]["cache_status"], "missing")


if __name__ == "__main__":
    unittest.main()
