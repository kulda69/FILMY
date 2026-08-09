from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from filmy import db_library


class UserListsPostgresOverlayTests(unittest.TestCase):
    def test_create_user_list_routes_to_postgres_and_resolves_slug_suffix(self) -> None:
        fake_db = SimpleNamespace(
            _now_iso=lambda: "2026-07-11T10:00:00",
            _slugify=lambda value: "moje-listina",
            uuid=SimpleNamespace(uuid4=lambda: "uuid-1"),
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
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

    def test_update_user_list_description_updates_ai_input_role(self) -> None:
        fake_db = SimpleNamespace(_now_iso=lambda: "2026-07-19T10:00:00")
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch(
                "filmy.db_library.fetch_user_list",
                return_value={"id": "list-a", "name": "List A", "list_kind": "custom"},
            ),
            patch(
                "filmy.db_library.update_user_list_description_postgres",
                return_value={
                    "id": "list-a",
                    "description": "updated",
                    "ai_input_role": "negative",
                },
            ) as update_mock,
        ):
            result = db_library.update_user_list_description(
                "list-a",
                " updated ",
                ai_input_role="negative",
            )

        update_mock.assert_called_once_with(
            "list-a",
            "updated",
            "negative",
            "2026-07-19T10:00:00",
        )
        self.assertEqual(result["ai_input_role"], "negative")

    def test_update_user_list_description_rejects_unknown_ai_input_role(self) -> None:
        fake_db = SimpleNamespace(_now_iso=lambda: "2026-07-19T10:00:00")
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch(
                "filmy.db_library.fetch_user_list",
                return_value={"id": "list-a", "name": "List A", "list_kind": "custom"},
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Neznámá role"):
                db_library.update_user_list_description(
                    "list-a",
                    "updated",
                    ai_input_role="wat",
                )

    def test_set_title_role_signal_upserts_stable_character_signal(self) -> None:
        fake_db = SimpleNamespace(
            _now_iso=lambda: "2026-07-19T11:00:00",
            _slugify=lambda value: "ephram-brown",
            clear_title_presentation_cache=lambda: None,
        )
        expected = {
            "signal_key": "role-signal:tt0379623:nm0395777:ephram-brown:dialogue",
            "tconst": "tt0379623",
            "nconst": "nm0395777",
            "character_name": "Ephram Brown",
            "signal_type": "dialogue",
            "polarity": "positive",
            "strength": 10,
            "notes": "dialogy a chování",
        }
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.fetch_title_card_rows", return_value=[("tt0379623",)]),
            patch("filmy.db_library.fetch_person_catalog_row", return_value=("nm0395777", "Gregory Smith", None, None)),
            patch("filmy.db_library.upsert_title_role_signal", return_value=expected) as upsert_mock,
        ):
            result = db_library.set_title_role_signal(
                " tt0379623 ",
                nconst=" nm0395777 ",
                character_name=" Ephram Brown ",
                signal_type="dialogue",
                polarity="positive",
                strength="10",
                notes=" dialogy a chování ",
            )

        upsert_mock.assert_called_once_with(
            signal_key="role-signal:tt0379623:nm0395777:ephram-brown:dialogue",
            tconst="tt0379623",
            nconst="nm0395777",
            character_name="Ephram Brown",
            signal_type="dialogue",
            polarity="positive",
            strength=10,
            notes="dialogy a chování",
            source_ref="manual_role_signal:tt0379623",
            now="2026-07-19T11:00:00",
        )
        self.assertEqual(result["signal_key"], expected["signal_key"])

    def test_set_title_role_signal_rejects_unknown_signal_type(self) -> None:
        fake_db = SimpleNamespace(
            _slugify=lambda value: "ephram-brown",
            _now_iso=lambda: "2026-07-19T11:00:00",
        )
        with patch("filmy.db_library._db", return_value=fake_db):
            with self.assertRaisesRegex(ValueError, "Neznámý typ"):
                db_library.set_title_role_signal(
                    "tt0379623",
                    character_name="Ephram Brown",
                    signal_type="seed",
                )

    def test_get_title_role_signals_reads_postgres_rows(self) -> None:
        rows = [{"signal_key": "role-signal:tt0379623:nm0395777:ephram-brown:character"}]
        with patch("filmy.db_library.fetch_title_role_signals_postgres", return_value=rows) as fetch_mock:
            result = db_library.get_title_role_signals(" tt0379623 ")

        fetch_mock.assert_called_once_with("tt0379623")
        self.assertEqual(result, rows)

    def test_get_user_list_items_page_assembles_postgres_groups(self) -> None:
        fake_db = SimpleNamespace(
            _poster_url_from_local_path=lambda path: f"/assets/{path}",
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch(
                "filmy.db_library.fetch_user_list_page_rows",
                return_value=(
                    {"id": "watchlist", "slug": "watchlist", "name": "Watchlist", "description": None, "list_kind": "watchlist"},
                    1,
                    [("tt1", "title", None, None, None, None, None, None, None, "Watchlist", "watchlist", "movie", 2021, "poster.jpg", None, "Alpha")],
                ),
            ),
            patch("filmy.db_library.fetch_latest_ratings_for_tconsts", return_value={"tt1": {"rating": 8}}),
        ):
            result = db_library.get_user_list_items_page("watchlist", limit=10, offset=0)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "Alpha")
        self.assertEqual(result["items"][0]["user_rating"], 8)
        self.assertEqual(result["filters"], {"available_in_cz": False})

    def test_record_watch_event_archives_watchlist_in_postgres_mode(self) -> None:
        fake_db = SimpleNamespace(
            get_content_detail=lambda tconst: {"kind": "title", "tconst": tconst, "tmdb": {"tmdb_id": 123}},
            _now_iso=lambda: "2026-07-11T10:00:00",
            uuid=SimpleNamespace(uuid4=lambda: "event-1"),
            datetime=__import__("datetime").datetime,
            _build_local_media_identity=lambda detail: {
                "media_type": "title",
                "tconst": detail["tconst"],
                "imdb_id": detail["tconst"],
                "tmdb_id": detail["tmdb"]["tmdb_id"],
                "parent_tconst": None,
                "parent_title": None,
                "title": "Alpha",
                "season_number": None,
                "episode_number": None,
            },
            _canonical_media_key=lambda *args: "title:tt1",
            clear_title_presentation_cache=lambda: None,
            _get_library_summary_for_tconst=lambda tconst: {"tconst": tconst},
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.fetch_user_list", return_value={"id": "watchlist", "name": "Watchlist", "list_kind": "watchlist"}),
            patch("filmy.db_library.fetch_list_action_rules", return_value=[]),
            patch(
                "filmy.db_library.record_watched_postgres",
                return_value={"event_id": "event-1", "content_state_changed": True, "archived_items": 1},
            ) as record_mock,
        ):
            result = db_library.record_watch_event("tt1")

        record_mock.assert_called_once_with(
            event_id="event-1",
            tconst="tt1",
            event_scope="title",
            watched_on="2026-07-11",
            notes=None,
            created_at="2026-07-11T10:00:00",
            archive_from_list_id="watchlist",
            archive_canonical_key="title:tt1",
            archive_display_tconst=None,
        )
        self.assertEqual(result["tconst"], "tt1")
        self.assertEqual(result["archived_items"], 1)

    def test_set_user_rating_uses_title_session_when_source_list_inferred_from_return_to(self) -> None:
        fake_db = SimpleNamespace(
            get_content_detail=lambda tconst: {"kind": "title", "tconst": tconst, "tmdb": {"tmdb_id": 123}, "library": {}},
            _now_iso=lambda: "2026-07-29T09:00:00",
            uuid=SimpleNamespace(uuid4=lambda: "uuid-1"),
            _build_local_media_identity=lambda detail: {
                "media_type": "title",
                "tconst": detail["tconst"],
                "imdb_id": detail["tconst"],
                "tmdb_id": detail["tmdb"]["tmdb_id"],
                "parent_tconst": None,
                "parent_title": None,
                "title": "Alpha",
                "season_number": None,
                "episode_number": None,
            },
            _canonical_media_key=lambda *args: "title:tt1",
            clear_title_presentation_cache=lambda: None,
            _get_library_summary_for_tconst=lambda tconst: {"tconst": tconst},
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.fetch_user_list", return_value={"id": "watchlist", "name": "Watchlist", "list_kind": "watchlist"}),
            patch("filmy.db_library.fetch_list_action_rules", return_value=[{"rule_id": "r1"}]) as rules_mock,
            patch("filmy.db_library.upsert_title_session", return_value={"session_id": "title-session:uuid-1"}) as session_mock,
            patch("filmy.db_library.insert_title_session_action", return_value={"action_id": "title-session-action:uuid-1"}) as action_mock,
            patch("filmy.db_library.queue_title_session_action_effects", return_value={"queued_count": 4}) as queue_mock,
            patch("filmy.db_library.apply_title_session_effects", return_value={"applied_count": 2, "effects": []}) as immediate_mock,
            patch(
                "filmy.db_library.finalize_title_session",
                return_value={
                    "session": {"session_id": "title-session:uuid-1"},
                    "finalize": {"effects": [{"result": "applied", "effect": {"effect_type": "write_watched"}}]},
                },
            ) as finalize_mock,
            patch("filmy.db_library.upsert_user_rating_postgres") as direct_rating_mock,
        ):
            result = db_library.set_user_rating(
                "tt1",
                8,
                return_to_url="/titles/tt1?return_to=%2F%3Flist_id%3Dwatchlist",
            )

        rules_mock.assert_called_once_with(
            source_list_id="watchlist",
            trigger_action="set_rating",
            target_list_id=None,
            target_match_mode="exact_or_wildcard",
            enabled_only=True,
        )
        session_mock.assert_called_once()
        action_mock.assert_called_once()
        queue_mock.assert_called_once_with("title-session-action:uuid-1", queued_at="2026-07-29T09:00:00")
        immediate_mock.assert_called_once_with(
            "title-session:uuid-1",
            phase="immediate",
            executed_at="2026-07-29T09:00:00",
            effect_status="pending",
        )
        finalize_mock.assert_called_once_with("title-session:uuid-1", finalized_at="2026-07-29T09:00:00")
        direct_rating_mock.assert_not_called()
        self.assertEqual(result["workflow"], "title_session")
        self.assertEqual(result["session_id"], "title-session:uuid-1")

    def test_set_user_rating_falls_back_to_direct_write_without_rules(self) -> None:
        fake_db = SimpleNamespace(
            get_content_detail=lambda tconst: {"kind": "title", "tconst": tconst, "tmdb": {"tmdb_id": 123}, "library": {}},
            _now_iso=lambda: "2026-07-29T09:00:00",
            uuid=SimpleNamespace(uuid4=lambda: "uuid-1"),
            _build_local_media_identity=lambda detail: {
                "media_type": "title",
                "tconst": detail["tconst"],
                "imdb_id": detail["tconst"],
                "tmdb_id": detail["tmdb"]["tmdb_id"],
                "parent_tconst": None,
                "parent_title": None,
                "title": "Alpha",
                "season_number": None,
                "episode_number": None,
            },
            _canonical_media_key=lambda *args: "title:tt1",
            clear_title_presentation_cache=lambda: None,
            _get_library_summary_for_tconst=lambda tconst: {"tconst": tconst},
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.fetch_user_list", return_value={"id": "watchlist", "name": "Watchlist", "list_kind": "watchlist"}),
            patch("filmy.db_library.fetch_list_action_rules", return_value=[]),
            patch("filmy.db_library.upsert_user_rating_postgres") as direct_rating_mock,
        ):
            result = db_library.set_user_rating(
                "tt1",
                8,
                return_to_url="/lists/watchlist",
            )

        direct_rating_mock.assert_called_once()
        self.assertNotIn("workflow", result)

    def test_record_watch_event_uses_title_session_when_source_list_is_available(self) -> None:
        fake_db = SimpleNamespace(
            get_content_detail=lambda tconst: {"kind": "title", "tconst": tconst, "tmdb": {"tmdb_id": 123}},
            _now_iso=lambda: "2026-07-29T09:15:00",
            uuid=SimpleNamespace(uuid4=lambda: "event-1"),
            datetime=__import__("datetime").datetime,
            _build_local_media_identity=lambda detail: {
                "media_type": "title",
                "tconst": detail["tconst"],
                "imdb_id": detail["tconst"],
                "tmdb_id": detail["tmdb"]["tmdb_id"],
                "parent_tconst": None,
                "parent_title": None,
                "title": "Alpha",
                "season_number": None,
                "episode_number": None,
            },
            _canonical_media_key=lambda *args: "title:tt1",
            clear_title_presentation_cache=lambda: None,
            _get_library_summary_for_tconst=lambda tconst: {"tconst": tconst},
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch("filmy.db_library.fetch_user_list", return_value={"id": "watchlist", "name": "Watchlist", "list_kind": "watchlist"}),
            patch("filmy.db_library.fetch_list_action_rules", return_value=[{"rule_id": "r1"}]),
            patch("filmy.db_library.upsert_title_session", return_value={"session_id": "title-session:event-1"}),
            patch("filmy.db_library.insert_title_session_action", return_value={"action_id": "title-session-action:event-1"}),
            patch("filmy.db_library.queue_title_session_action_effects", return_value={"queued_count": 2}),
            patch("filmy.db_library.apply_title_session_effects", return_value={"applied_count": 0, "effects": []}),
            patch(
                "filmy.db_library.finalize_title_session",
                return_value={
                    "session": {"session_id": "title-session:event-1"},
                    "finalize": {
                        "effects": [
                            {"result": "applied", "effect": {"effect_type": "write_watched"}},
                            {"result": "applied", "effect": {"effect_type": "deactivate_source_membership"}},
                        ]
                    },
                },
            ),
            patch("filmy.db_library.record_watched_postgres") as direct_watch_mock,
        ):
            result = db_library.record_watch_event("tt1", archive_from_list_id="watchlist")

        direct_watch_mock.assert_not_called()
        self.assertEqual(result["workflow"], "title_session")
        self.assertEqual(result["archived_items"], 1)

    def test_delete_group_from_user_list_archives_postgres_items(self) -> None:
        fake_db = SimpleNamespace(
            _now_iso=lambda: "2026-07-11T10:00:00",
            clear_title_presentation_cache=lambda: None,
        )
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch(
                "filmy.db_library.archive_user_list_group",
                return_value={"list_found": True, "archived_items": 2},
            ) as archive_group_mock,
        ):
            result = db_library.delete_group_from_user_list("list-a", "tt1")

        self.assertEqual(result["affected_rows"], 2)
        archive_group_mock.assert_called_once_with(
            list_id="list-a",
            display_tconst="tt1",
            now="2026-07-11T10:00:00",
        )

    def test_clear_ai_suggestions_list_items_hard_deletes_list_items(self) -> None:
        fake_db = SimpleNamespace(clear_title_presentation_cache=lambda: None)
        with (
            patch("filmy.db_library._db", return_value=fake_db),
            patch(
                "filmy.db_library.fetch_user_list",
                return_value={
                    "id": "ai-suggestions",
                    "name": "AI návrhy",
                    "list_kind": "custom",
                    "ai_input_role": "external_suggestion",
                },
            ),
            patch(
                "filmy.db_library.clear_ai_suggestions_list_items_postgres",
                return_value={
                    "id": "ai-suggestions",
                    "name": "AI návrhy",
                    "list_kind": "custom",
                    "ai_input_role": "external_suggestion",
                    "deleted_items": 17,
                },
            ) as clear_mock,
        ):
            result = db_library.clear_ai_suggestions_list_items()

        clear_mock.assert_called_once_with()
        self.assertEqual(result["deleted_items"], 17)

    def test_clear_ai_suggestions_list_items_rejects_wrong_role(self) -> None:
        with patch(
            "filmy.db_library.fetch_user_list",
            return_value={
                "id": "ai-suggestions",
                "name": "AI návrhy",
                "list_kind": "custom",
                "ai_input_role": "strong_positive",
            },
        ):
            with self.assertRaisesRegex(ValueError, "Vyčistit lze jen seznam AI návrhy"):
                db_library.clear_ai_suggestions_list_items()

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
            patch(
                "filmy.db_library._get_postgres_group_items_for_list",
                return_value=({"id": "list-a", "name": "List A", "list_kind": "custom"}, [item]),
            ),
            patch("filmy.db_library.fetch_user_list", side_effect=[{"id": "list-b", "name": "List B", "list_kind": "custom"}, {"id": "list-a", "name": "List A", "list_kind": "custom"}]),
            patch("filmy.db_library.fetch_list_action_rules", return_value=[]),
            patch("filmy.db_library.upsert_user_list_item") as upsert_mock,
            patch("filmy.db_library.archive_user_list_item") as archive_mock,
        ):
            result = db_library.move_group_between_user_lists("list-a", "list-b", "tt1")

        self.assertEqual(result["moved_rows"], 1)
        upsert_mock.assert_called_once()
        archive_mock.assert_called_once_with("list-a", "title:tt1", "2026-07-11T10:00:00")

    def test_move_group_between_user_lists_uses_title_session_when_rules_exist(self) -> None:
        fake_db = SimpleNamespace(
            _now_iso=lambda: "2026-07-29T10:00:00",
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
            patch(
                "filmy.db_library._get_postgres_group_items_for_list",
                return_value=({"id": "list-a", "name": "List A", "list_kind": "custom"}, [item]),
            ),
            patch("filmy.db_library.fetch_user_list", side_effect=[{"id": "list-b", "name": "List B", "list_kind": "custom"}, {"id": "list-a", "name": "List A", "list_kind": "custom"}]),
            patch("filmy.db_library.fetch_list_action_rules", return_value=[{"rule_id": "r1"}]),
            patch("filmy.db_library.upsert_title_session", return_value={"session_id": "title-session:new-id"}),
            patch("filmy.db_library.insert_title_session_action", return_value={"action_id": "title-session-action:new-id"}),
            patch("filmy.db_library.queue_title_session_action_effects", return_value={"queued_count": 2}),
            patch("filmy.db_library.apply_title_session_effects", return_value={"applied_count": 1, "effects": []}),
            patch("filmy.db_library.finalize_title_session", return_value={"session": {"session_id": "title-session:new-id"}, "finalize": {"effects": []}}),
            patch("filmy.db_library.upsert_user_list_item") as upsert_mock,
            patch("filmy.db_library.archive_user_list_item") as archive_mock,
        ):
            result = db_library.move_group_between_user_lists("list-a", "list-b", "tt1")

        upsert_mock.assert_not_called()
        archive_mock.assert_not_called()
        self.assertEqual(result["workflow"], "title_session")

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
            patch(
                "filmy.db_library._get_postgres_group_items_for_list",
                return_value=({"id": "list-a", "name": "List A", "list_kind": "custom"}, [item]),
            ),
            patch("filmy.db_library.fetch_user_list", side_effect=[{"id": "list-b", "name": "List B", "list_kind": "custom"}, {"id": "list-a", "name": "List A", "list_kind": "custom"}]),
            patch("filmy.db_library.fetch_list_action_rules", return_value=[]),
            patch("filmy.db_library.upsert_user_list_item") as upsert_mock,
            patch("filmy.db_library.archive_user_list_item") as archive_mock,
        ):
            result = db_library.copy_group_to_user_list("list-a", "list-b", "tt1")

        self.assertEqual(result["copied_rows"], 1)
        upsert_mock.assert_called_once()
        archive_mock.assert_not_called()

    def test_copy_group_to_user_list_uses_title_session_when_rules_exist(self) -> None:
        fake_db = SimpleNamespace(
            _now_iso=lambda: "2026-07-29T10:05:00",
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
            patch(
                "filmy.db_library._get_postgres_group_items_for_list",
                return_value=({"id": "list-a", "name": "List A", "list_kind": "custom"}, [item]),
            ),
            patch("filmy.db_library.fetch_user_list", side_effect=[{"id": "list-b", "name": "List B", "list_kind": "custom"}, {"id": "list-a", "name": "List A", "list_kind": "custom"}]),
            patch("filmy.db_library.fetch_list_action_rules", return_value=[{"rule_id": "r1"}]),
            patch("filmy.db_library.upsert_title_session", return_value={"session_id": "title-session:new-id"}),
            patch("filmy.db_library.insert_title_session_action", return_value={"action_id": "title-session-action:new-id"}),
            patch("filmy.db_library.queue_title_session_action_effects", return_value={"queued_count": 1}),
            patch("filmy.db_library.apply_title_session_effects", return_value={"applied_count": 1, "effects": []}),
            patch("filmy.db_library.finalize_title_session", return_value={"session": {"session_id": "title-session:new-id"}, "finalize": {"effects": []}}),
            patch("filmy.db_library.upsert_user_list_item") as upsert_mock,
            patch("filmy.db_library.archive_user_list_item") as archive_mock,
        ):
            result = db_library.copy_group_to_user_list("list-a", "list-b", "tt1")

        upsert_mock.assert_not_called()
        archive_mock.assert_not_called()
        self.assertEqual(result["workflow"], "title_session")


if __name__ == "__main__":
    unittest.main()
