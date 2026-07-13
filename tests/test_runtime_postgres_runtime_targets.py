from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from filmy import db


class RuntimePostgresRuntimeTargetsTests(unittest.TestCase):
    def test_get_tmdb_enrichment_targets_merges_postgres_runtime_candidates(self) -> None:
        candidate = {"tconst": "tt1", "title_type": "movie", "primary_title": "Alpha", "start_year": 2024, "priority": 1, "reasons": ["watched_title"]}
        with (
            patch("filmy.db._get_runtime_postgres_candidate_items", return_value=[candidate]),
            patch("filmy.db.fetch_tmdb_completion_flags", return_value={}),
        ):
            items = db.get_tmdb_enrichment_targets(limit=5)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tconst"], "tt1")

    def test_get_title_detail_cache_targets_uses_postgres_runtime_candidates(self) -> None:
        candidate = {"tconst": "tt1", "title_type": "movie", "primary_title": "Alpha", "start_year": 2024, "priority": 1, "reasons": ["watched_title"]}
        complete_flags = {"tt1": {"has_primary": True, "has_fallback": True, "poster_path": None, "backdrop_path": None}}
        with (
            patch("filmy.db._get_runtime_postgres_candidate_items", return_value=[candidate]),
            patch("filmy.db.fetch_tmdb_completion_flags", return_value=complete_flags),
            patch("filmy.db._title_detail_cache_path", return_value=Path("/definitely-missing-codex-cache.json")),
        ):
            items = db.get_title_detail_cache_targets(limit=5)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tconst"], "tt1")
        self.assertEqual(items[0]["cache_status"], "missing")


if __name__ == "__main__":
    unittest.main()
