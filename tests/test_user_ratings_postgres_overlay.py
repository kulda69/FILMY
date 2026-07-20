from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from filmy import db
from filmy import db_library


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
    def test_genre_score_source_rows_uses_postgres_rating(self) -> None:
        conn = FakeConn(
            [
                ("tt1", "Alpha", 2020, "Drama,Thriller", 5, 2, None, None),
                ("tt2", "Beta", 2021, "Comedy", None, 1, None, None),
            ]
        )
        with (
            patch("filmy.db.fetch_latest_ratings_for_tconsts") as ratings_mock,
            patch("filmy.db.fetch_positive_person_affinities", return_value={}),
        ):
            ratings_mock.return_value = {
                "tt1": {"tconst": "tt1", "rating": 9, "rated_at": None, "updated_at": None}
            }
            rows = db._get_genre_score_source_rows(conn, ratings_in_postgres=True)

        self.assertEqual(rows[0]["rating"], 9)
        self.assertIsNone(rows[1]["rating"])

    def test_plain_rating_update_preserves_existing_notes(self) -> None:
        detail = {
            "tconst": "tt4034228",
            "kind": "title",
            "title_type": "movie",
            "primary_title": "Test title",
            "library": {
                "rating": {
                    "value": 8,
                    "liked_notes": "silný příběh",
                    "disliked_notes": "pomalejší tempo",
                }
            },
        }
        media = {
            "media_type": "title",
            "tconst": "tt4034228",
            "imdb_id": "tt4034228",
            "tmdb_id": 1,
            "parent_tconst": None,
            "parent_title": None,
            "title": "Test title",
            "season_number": None,
            "episode_number": None,
        }
        fake_db = SimpleNamespace(
            get_content_detail=lambda tconst: detail,
            _now_iso=lambda: "2026-07-19T20:00:00",
            _build_local_media_identity=lambda _detail: media,
            _canonical_media_key=lambda *args: "title:tconst:tt4034228",
            _get_library_summary_for_tconst=lambda tconst: detail["library"],
            invalidate_title_presentation_cache=lambda tconst: None,
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.upsert_user_rating_postgres") as upsert_mock,
        ):
            db_library.set_user_rating("tt4034228", 9)

        self.assertEqual(upsert_mock.call_args.kwargs["liked_notes"], "silný příběh")
        self.assertEqual(upsert_mock.call_args.kwargs["disliked_notes"], "pomalejší tempo")


if __name__ == "__main__":
    unittest.main()
