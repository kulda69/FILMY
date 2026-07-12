from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from filmy import db_library


class _FakeConn:
    def execute(self, sql, params=None):
        raise AssertionError(f"Unexpected SQL in test: {sql}")


class _FakeOpenConnection:
    def __enter__(self):
        return _FakeConn()

    def __exit__(self, exc_type, exc, tb):
        return False


class UserListsPostgresOverlayTests(unittest.TestCase):
    def test_create_user_list_routes_to_postgres_and_resolves_slug_suffix(self) -> None:
        fake_db = SimpleNamespace(
            _now_iso=lambda: "2026-07-11T10:00:00",
            _slugify=lambda value: "moje-listina",
            uuid=SimpleNamespace(uuid4=lambda: "uuid-1"),
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.user_lists_uses_postgres", return_value=True),
            patch("filmy.db_library.slug_exists", side_effect=[True, False]),
            patch("filmy.db_library.create_user_list_postgres") as create_mock,
        ):
            create_mock.return_value = {"id": "custom-list-uuid-1", "slug": "moje-listina-2", "name": "Moje listina"}
            result = db_library.create_user_list("Moje listina")

        create_mock.assert_called_once_with(
            list_id="custom-list-uuid-1",
            slug="moje-listina-2",
            name="Moje listina",
            description=None,
            now="2026-07-11T10:00:00",
        )
        self.assertEqual(result["slug"], "moje-listina-2")

    def test_get_user_list_items_page_assembles_postgres_groups(self) -> None:
        fake_db = SimpleNamespace(
            _poster_url_from_local_path=lambda path: f"/assets/{path}",
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.user_lists_uses_postgres", return_value=True),
            patch("filmy.db_library.user_ratings_uses_postgres", return_value=True),
            patch("filmy.db_library.open_duckdb_connection", return_value=_FakeOpenConnection()),
            patch(
                "filmy.db_library._group_postgres_list_items",
                return_value=(
                    {"id": "watchlist", "slug": "watchlist", "name": "Watchlist", "description": None, "list_kind": "watchlist"},
                    [{"display_tconst": "tt1", "media_type": "title", "parent_title": None, "season_number": None, "episode_number": None, "rank": None, "added_at": None, "notes": None, "list_name": "Watchlist", "list_kind": "watchlist"}],
                ),
            ),
            patch(
                "filmy.db_library._load_group_cards",
                return_value=[
                    {
                        "display_tconst": "tt1",
                        "media_type": "title",
                        "parent_title": None,
                        "season_number": None,
                        "episode_number": None,
                        "rank": None,
                        "added_at": None,
                        "notes": None,
                        "list_name": "Watchlist",
                        "list_kind": "watchlist",
                        "title_type": "movie",
                        "year": 2021,
                        "resolved_title": "Alpha",
                        "poster_relative_path": "poster.jpg",
                        "poster_local_path": None,
                    }
                ],
            ),
            patch("filmy.db_library.fetch_latest_ratings_for_tconsts", return_value={"tt1": {"rating": 8}}),
        ):
            result = db_library.get_user_list_items_page("watchlist", limit=10, offset=0)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "Alpha")
        self.assertEqual(result["items"][0]["user_rating"], 8)

    def test_record_watch_event_archives_watchlist_in_postgres_mode(self) -> None:
        fake_db = SimpleNamespace(
            get_content_detail=lambda tconst: {"kind": "title", "tconst": tconst, "tmdb": {"tmdb_id": 123}},
            _now_iso=lambda: "2026-07-11T10:00:00",
            uuid=SimpleNamespace(uuid4=lambda: "event-1"),
            datetime=__import__("datetime").datetime,
            _canonical_media_key=lambda *args: "title:tt1",
            clear_title_presentation_cache=lambda: None,
            _get_library_summary_for_tconst=lambda tconst: {"tconst": tconst},
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.watch_events_uses_postgres", return_value=True),
            patch("filmy.db_library.user_lists_uses_postgres", return_value=True),
            patch("filmy.db_library.insert_watch_event_postgres") as insert_mock,
            patch("filmy.db_library.archive_user_list_item") as archive_mock,
        ):
            result = db_library.record_watch_event("tt1")

        insert_mock.assert_called_once()
        archive_mock.assert_called_once_with("watchlist", "title:tt1", "2026-07-11T10:00:00")
        self.assertEqual(result["tconst"], "tt1")

    def test_delete_group_from_user_list_archives_postgres_items(self) -> None:
        fake_db = SimpleNamespace(
            _now_iso=lambda: "2026-07-11T10:00:00",
            clear_title_presentation_cache=lambda: None,
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.user_lists_uses_postgres", return_value=True),
            patch("filmy.db_library.open_duckdb_connection", return_value=_FakeOpenConnection()),
            patch(
                "filmy.db_library._get_postgres_group_items_for_list",
                return_value=(
                    {"id": "list-a", "name": "List A", "list_kind": "custom"},
                    [{"canonical_key": "title:tt1"}, {"canonical_key": "title:tt2"}],
                ),
            ),
            patch("filmy.db_library.archive_user_list_item") as archive_mock,
        ):
            result = db_library.delete_group_from_user_list("list-a", "tt1")

        self.assertEqual(result["affected_rows"], 2)
        self.assertEqual(archive_mock.call_count, 2)

    def test_move_group_between_user_lists_moves_postgres_items(self) -> None:
        fake_db = SimpleNamespace(
            _now_iso=lambda: "2026-07-11T10:00:00",
            clear_title_presentation_cache=lambda: None,
            uuid=SimpleNamespace(uuid4=lambda: "new-id"),
        )
        item = {
            "canonical_key": "title:tt1",
            "tconst": "tt1",
            "media_type": "title",
            "imdb_id": "tt1",
            "tmdb_id": 11,
            "trakt_id": None,
            "parent_tconst": None,
            "parent_title": None,
            "title": "Alpha",
            "season_number": None,
            "episode_number": None,
            "rank": 3,
            "added_at": datetime(2026, 7, 10, 12, 0, 0),
            "notes": "x",
            "source_origin": "local_app",
            "source_ref": "manual",
        }
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.user_lists_uses_postgres", return_value=True),
            patch("filmy.db_library.open_duckdb_connection", return_value=_FakeOpenConnection()),
            patch(
                "filmy.db_library._get_postgres_group_items_for_list",
                return_value=({"id": "list-a", "name": "List A", "list_kind": "custom"}, [item]),
            ),
            patch("filmy.db_library.fetch_user_list", return_value={"id": "list-b", "name": "List B", "list_kind": "custom"}),
            patch("filmy.db_library.upsert_user_list_item") as upsert_mock,
            patch("filmy.db_library.archive_user_list_item") as archive_mock,
        ):
            result = db_library.move_group_between_user_lists("list-a", "list-b", "tt1")

        self.assertEqual(result["moved_rows"], 1)
        upsert_mock.assert_called_once()
        archive_mock.assert_called_once_with("list-a", "title:tt1", "2026-07-11T10:00:00")

    def test_copy_group_to_user_list_copies_postgres_items(self) -> None:
        fake_db = SimpleNamespace(
            _now_iso=lambda: "2026-07-11T10:00:00",
            clear_title_presentation_cache=lambda: None,
            uuid=SimpleNamespace(uuid4=lambda: "new-id"),
        )
        item = {
            "canonical_key": "title:tt1",
            "tconst": "tt1",
            "media_type": "title",
            "imdb_id": "tt1",
            "tmdb_id": None,
            "trakt_id": None,
            "parent_tconst": None,
            "parent_title": None,
            "title": "Alpha",
            "season_number": None,
            "episode_number": None,
            "rank": None,
            "added_at": None,
            "notes": None,
            "source_origin": "local_app",
            "source_ref": "manual",
        }
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.user_lists_uses_postgres", return_value=True),
            patch("filmy.db_library.open_duckdb_connection", return_value=_FakeOpenConnection()),
            patch(
                "filmy.db_library._get_postgres_group_items_for_list",
                return_value=({"id": "list-a", "name": "List A", "list_kind": "custom"}, [item]),
            ),
            patch("filmy.db_library.fetch_user_list", return_value={"id": "list-b", "name": "List B", "list_kind": "custom"}),
            patch("filmy.db_library.upsert_user_list_item") as upsert_mock,
            patch("filmy.db_library.archive_user_list_item") as archive_mock,
        ):
            result = db_library.copy_group_to_user_list("list-a", "list-b", "tt1")

        self.assertEqual(result["copied_rows"], 1)
        upsert_mock.assert_called_once()
        archive_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
