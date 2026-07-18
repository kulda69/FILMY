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

    def test_replace_favorite_genres_normalizes_payload_for_postgres(self) -> None:
        with (
            patch("filmy.db.app_state_uses_postgres", return_value=True),
            patch("filmy.db._now_iso", return_value="2026-07-11T12:00:00"),
            patch("filmy.db.replace_favorite_genres_postgres") as write_mock,
        ):
            result = db.replace_favorite_genres(
                [
                    "Drama",
                    {"genre": "Sci-Fi", "weight": 2, "preference_rank": 5, "notes": "core", "is_active": True},
                ],
                source_ref="ui:test",
            )
        write_mock.assert_called_once_with(
            items=[
                {
                    "genre": "Drama",
                    "weight": 1.0,
                    "preference_rank": 1,
                    "notes": None,
                    "is_active": True,
                },
                {
                    "genre": "Sci-Fi",
                    "weight": 2.0,
                    "preference_rank": 5,
                    "notes": "core",
                    "is_active": True,
                },
            ],
            source_origin="local_app",
            source_ref="ui:test",
            archive_missing=True,
            now="2026-07-11T12:00:00",
        )
        self.assertEqual(result["genres"], ["Drama", "Sci-Fi"])

    def test_latest_genre_scores_reads_postgres_when_enabled(self) -> None:
        with (
            patch("filmy.db.app_state_uses_postgres", return_value=True),
            patch("filmy.db.fetch_latest_genre_scores_postgres", return_value={"count": 1, "items": [{"genre": "Action"}]}),
        ):
            result = db.get_latest_genre_scores()
        self.assertEqual(result["items"][0]["genre"], "Action")

    def test_ai_context_returns_full_preferences_and_rating_scales(self) -> None:
        with (
            patch("filmy.db.get_favorite_genres", return_value=[{"genre": "Drama", "is_active": False}]) as genres_mock,
            patch("filmy.db.get_favorite_traits", return_value=[{"trait": "slow-burn", "is_active": True}]) as traits_mock,
        ):
            result = db.get_ai_context()

        genres_mock.assert_called_once_with(active_only=False)
        traits_mock.assert_called_once_with(active_only=False)
        self.assertEqual(result["contract_version"], 1)
        self.assertEqual(result["rating_scales"]["user_rating"]["min"], 1)
        self.assertEqual(result["rating_scales"]["user_rating"]["max"], 10)
        self.assertEqual(result["rating_scales"]["person_affinity_rating"]["min"], 0)
        self.assertEqual(result["favorite_genres"][0]["genre"], "Drama")
        self.assertEqual(result["favorite_traits"][0]["trait"], "slow-burn")


if __name__ == "__main__":
    unittest.main()
