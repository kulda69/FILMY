from __future__ import annotations

import unittest
from unittest.mock import patch

from filmy import db


class SuggestionCardSummaryTests(unittest.TestCase):
    def test_get_title_card_summaries_for_tconsts_builds_pg_preview(self) -> None:
        with (
            patch("filmy.db.catalog_backend_uses_postgres", return_value=True),
            patch(
                "filmy.db.fetch_title_card_detail_rows",
                return_value=[
                    ("tt1", "movie", 1999, "The Matrix", "Matrix", 136, "Action,Sci-Fi", 8.7, 2000000, "posters/matrix.jpg", None),
                ],
            ),
            patch(
                "filmy.db.fetch_title_people_preview_rows",
                return_value=[
                    ("tt1", "director", 1, "Lana Wachowski"),
                    ("tt1", "director", 2, "Lilly Wachowski"),
                    ("tt1", "cast", 1, "Keanu Reeves"),
                    ("tt1", "cast", 2, "Carrie-Anne Moss"),
                ],
            ),
        ):
            result = db.get_title_card_summaries_for_tconsts(["tt1"])

        self.assertEqual(result["tt1"]["title"], "The Matrix")
        self.assertEqual(result["tt1"]["kind_label"], "Movie")
        self.assertEqual(result["tt1"]["original_title"], "Matrix")
        self.assertEqual(result["tt1"]["runtime_minutes"], 136)
        self.assertEqual(result["tt1"]["genres"], ["Action", "Sci-Fi"])
        self.assertEqual(result["tt1"]["imdb_rating"], 8.7)
        self.assertEqual(result["tt1"]["imdb_votes"], 2000000)
        self.assertEqual(result["tt1"]["directed_by_line"], "Lana Wachowski, Lilly Wachowski")
        self.assertEqual(result["tt1"]["main_cast_line"], "Keanu Reeves, Carrie-Anne Moss")
        self.assertEqual(result["tt1"]["poster_url"], "/assets/tmdb/posters/matrix.jpg")


if __name__ == "__main__":
    unittest.main()
