from __future__ import annotations

import unittest
from unittest.mock import patch

from filmy import db


class AppStatePostgresOverlayTests(unittest.TestCase):
    def test_get_favorite_genres_reads_postgres_when_enabled(self) -> None:
        with (
            patch("filmy.db.app_state_uses_postgres", return_value=True),
            patch("filmy.db.fetch_favorite_genres_postgres", return_value=[{"genre": "Drama"}]),
        ):
            rows = db.get_favorite_genres(active_only=True)
        self.assertEqual(rows[0]["genre"], "Drama")

    def test_replace_favorite_traits_writes_postgres_when_enabled(self) -> None:
        with (
            patch("filmy.db.app_state_uses_postgres", return_value=True),
            patch("filmy.db._now_iso", return_value="2026-07-11T12:00:00"),
            patch("filmy.db.replace_favorite_traits_postgres") as write_mock,
        ):
            result = db.replace_favorite_traits(["moody", "tense"])
        write_mock.assert_called_once()
        self.assertEqual(result["count"], 2)

    def test_latest_genre_scores_reads_postgres_when_enabled(self) -> None:
        with (
            patch("filmy.db.app_state_uses_postgres", return_value=True),
            patch("filmy.db.fetch_latest_genre_scores_postgres", return_value={"count": 1, "items": [{"genre": "Action"}]}),
        ):
            result = db.get_latest_genre_scores()
        self.assertEqual(result["items"][0]["genre"], "Action")


if __name__ == "__main__":
    unittest.main()
