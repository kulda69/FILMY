from __future__ import annotations

import unittest

from filmy.db import _merge_tmdb_target_items, _tmdb_target_sort_key


class TmdbTargetSelectionTests(unittest.TestCase):
    def test_merge_tmdb_target_items_keeps_lower_priority_and_unions_reasons(self) -> None:
        primary = [
            {
                "tconst": "tt1",
                "title_type": "movie",
                "primary_title": "Alpha",
                "start_year": 2020,
                "priority": 3,
                "reasons": ["watchlist"],
            }
        ]
        secondary = [
            {
                "tconst": "tt1",
                "title_type": "movie",
                "primary_title": "Alpha",
                "start_year": 2020,
                "priority": 2,
                "reasons": ["in_progress_title"],
            }
        ]

        merged = _merge_tmdb_target_items(primary, secondary)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["priority"], 2)
        self.assertEqual(merged[0]["reasons"], ["in_progress_title", "watchlist"])

    def test_tmdb_target_sort_key_prefers_priority_then_newer_year(self) -> None:
        items = [
            {"tconst": "tt-old", "priority": 2, "start_year": 1999, "primary_title": "Zulu"},
            {"tconst": "tt-new", "priority": 2, "start_year": 2024, "primary_title": "Alpha"},
            {"tconst": "tt-priority", "priority": 1, "start_year": 2005, "primary_title": "Beta"},
        ]

        ordered = sorted(items, key=_tmdb_target_sort_key)

        self.assertEqual([item["tconst"] for item in ordered], ["tt-priority", "tt-new", "tt-old"])


if __name__ == "__main__":
    unittest.main()
