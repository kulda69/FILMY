from __future__ import annotations

import unittest
from unittest.mock import patch

from filmy import db


class FakeCursorResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return FakeCursorResult(self._rows)


class UserRatingsPostgresOverlayTests(unittest.TestCase):
    def test_genre_score_source_rows_overlay_postgres_rating(self) -> None:
        conn = FakeConn(
            [
                ("tt1", "Alpha", 2020, "Drama,Thriller", 5, 2, None, None),
                ("tt2", "Beta", 2021, "Comedy", None, 1, None, None),
            ]
        )
        with (
            patch("filmy.db.fetch_latest_ratings_for_tconsts") as ratings_mock,
            patch("filmy.db.app_state_uses_postgres", return_value=False),
        ):
            ratings_mock.return_value = {
                "tt1": {"tconst": "tt1", "rating": 9, "rated_at": None, "updated_at": None}
            }
            rows = db._get_genre_score_source_rows(conn, ratings_in_postgres=True)

        self.assertEqual(rows[0]["rating"], 9)
        self.assertIsNone(rows[1]["rating"])


if __name__ == "__main__":
    unittest.main()
