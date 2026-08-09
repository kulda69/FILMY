from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from filmy.config import load_ui_config
from filmy import runtime_postgres


class UiConfigTests(unittest.TestCase):
    def test_runtime_backend_switches_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('legacy_backend = "old"\n', encoding="utf-8")
            config = load_ui_config(config_path)
        self.assertFalse(hasattr(config, "legacy_backend"))

    def test_runtime_helpers_are_postgres_only(self) -> None:
        self.assertTrue(runtime_postgres.content_state_uses_postgres())
        self.assertTrue(runtime_postgres.user_ratings_uses_postgres())
        self.assertTrue(runtime_postgres.watch_events_uses_postgres())


class RuntimePostgresTests(unittest.TestCase):
    def test_row_to_content_state_parses_timestamps(self) -> None:
        row = [
            "tt1234567",
            "in_progress",
            "2026-07-11 09:00:00",
            "",
            "2026-07-11 10:30:45.123456",
        ]
        parsed = runtime_postgres._row_to_content_state(row)
        self.assertEqual(parsed["tconst"], "tt1234567")
        self.assertEqual(parsed["interest_state"], "in_progress")
        self.assertEqual(parsed["last_previewed_at"], datetime(2026, 7, 11, 9, 0, 0))
        self.assertIsNone(parsed["last_watched_at"])
        self.assertEqual(parsed["updated_at"], datetime(2026, 7, 11, 10, 30, 45, 123456))

    @patch("filmy.runtime_postgres._connect")
    def test_fetch_content_state_returns_none_for_missing_row(self, connect_mock) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn
        self.assertIsNone(runtime_postgres.fetch_content_state("tt0000001"))

    @patch("filmy.runtime_postgres._connect")
    def test_update_content_state_returns_upserted_row(self, connect_mock) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "tt0000001",
            "watched",
            None,
            datetime(2026, 7, 11, 21, 15, 0),
            datetime(2026, 7, 11, 21, 15, 0),
        )
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn
        result = runtime_postgres.update_content_state("tt0000001", "watched", "2026-07-11 21:15:00")
        self.assertEqual(result["interest_state"], "watched")
        self.assertEqual(result["last_watched_at"], datetime(2026, 7, 11, 21, 15, 0))
        executed_sql = cursor.execute.call_args.args[0]
        self.assertIn("last_previewed_at", executed_sql)
        self.assertIn("last_watched_at", executed_sql)

    @patch("filmy.runtime_postgres._connect")
    def test_fetch_latest_ratings_for_tconsts(self, connect_mock) -> None:
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ("tt1", 8, "silna atmosfera", "pomalejsi konec", datetime(2026, 7, 11, 21, 15, 0), datetime(2026, 7, 11, 21, 15, 0)),
            ("tt2", 6, None, None, None, datetime(2026, 7, 10, 10, 0, 0)),
        ]
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn
        result = runtime_postgres.fetch_latest_ratings_for_tconsts(["tt1", "tt2"])
        self.assertEqual(result["tt1"]["rating"], 8)
        self.assertEqual(result["tt1"]["liked_notes"], "silna atmosfera")
        self.assertEqual(result["tt1"]["disliked_notes"], "pomalejsi konec")
        self.assertEqual(result["tt2"]["updated_at"], datetime(2026, 7, 10, 10, 0, 0))

    @patch("filmy.runtime_postgres._connect")
    def test_ai_watched_titles_builds_non_weakenable_complete_blacklist(self, connect_mock) -> None:
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (
                "tt0133093",
                "The Matrix",
                "The Matrix",
                "movie",
                1999,
                "Action,Sci-Fi",
                8.7,
                2_000_000,
                603,
                9,
                datetime(2026, 8, 8).date(),
                ["strong_positive_list", "user_rating", "watch_event"],
                {"strong_positive_list": 1, "user_rating": 1, "watch_event": 1},
                1,
            )
        ]
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn

        result = runtime_postgres.fetch_ai_watched_title_rows()

        executed_sql = cursor.execute.call_args.args[0]
        self.assertIn("old.trakt_history_events", executed_sql)
        self.assertIn("'{show,ids,imdb}'", executed_sql)
        self.assertIn("substring(tconst FROM '^(tt[0-9]+)')", executed_sql)
        self.assertIn("s.interest_state IN ('watched', 'in_progress')", executed_sql)
        self.assertIn("l.ai_input_role IN ('negative', 'in_progress', 'strong_positive')", executed_sql)
        self.assertNotIn("LIMIT", executed_sql)
        self.assertEqual(result["contract_version"], 2)
        self.assertEqual(result["filters"]["mode"], "complete_hard_blacklist")
        self.assertEqual(result["unresolved_item_count"], 1)
        self.assertEqual(result["items"][0]["tconst"], "tt0133093")

    def test_row_to_user_rating_includes_taste_notes(self) -> None:
        row = [
            "movie|tt1",
            "tt1",
            9,
            "tempo a herci",
            "malo civilni konec",
            "2026-07-18 12:00:00",
            "2026-07-18 12:05:00",
        ]
        result = runtime_postgres._row_to_user_rating(row)
        self.assertEqual(result["liked_notes"], "tempo a herci")
        self.assertEqual(result["disliked_notes"], "malo civilni konec")

    @patch("filmy.runtime_postgres._connect")
    def test_fetch_library_summary_snapshot_includes_rating_notes(self, connect_mock) -> None:
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (0, None),
            (False,),
            (8, "dobry svet", "dlouhe sceny", datetime(2026, 7, 18, 15, 40, 0)),
        ]
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn
        result = runtime_postgres.fetch_library_summary_snapshot("tt5464086", "tvSeries")
        self.assertEqual(result["rating"]["value"], 8)
        self.assertEqual(result["rating"]["liked_notes"], "dobry svet")
        self.assertEqual(result["rating"]["disliked_notes"], "dlouhe sceny")

    @patch("filmy.runtime_postgres._connect")
    def test_fetch_watch_stats_for_tconsts(self, connect_mock) -> None:
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ("tt1", 3, datetime(2026, 7, 11, 21, 0, 0)),
            ("tt2", 1, None),
        ]
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn
        result = runtime_postgres.fetch_watch_stats_for_tconsts(["tt1", "tt2"])
        self.assertEqual(result["tt1"]["watched_count"], 3)
        self.assertEqual(result["tt1"]["last_watched_at"], datetime(2026, 7, 11, 21, 0, 0))

    @patch("filmy.runtime_postgres._connect")
    def test_archive_user_list_group_calls_server_function(self, connect_mock) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (True, 2)
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn

        result = runtime_postgres.archive_user_list_group(
            list_id="list-a",
            display_tconst="tt1",
            now="2026-07-21T10:00:00",
        )

        self.assertEqual(result, {"list_found": True, "archived_items": 2})
        executed_sql = cursor.execute.call_args.args[0]
        self.assertIn("app.archive_user_list_group", executed_sql)
        conn.commit.assert_called_once_with()

    @patch("filmy.runtime_postgres._connect")
    def test_fetch_list_action_rules_parses_jsonb_payload(self, connect_mock) -> None:
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (
                "rule-1",
                "watchlist",
                "set_rating",
                None,
                "derive_watched",
                "immediate",
                10,
                True,
                None,
                None,
                {"threshold": 7},
                datetime(2026, 7, 29, 10, 0, 0),
                datetime(2026, 7, 29, 10, 5, 0),
            )
        ]
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn

        result = runtime_postgres.fetch_list_action_rules(
            source_list_id="watchlist",
            trigger_action="set_rating",
        )

        self.assertEqual(result[0]["rule_id"], "rule-1")
        self.assertEqual(result[0]["effect_params"], {"threshold": 7})
        executed_sql = cursor.execute.call_args.args[0]
        self.assertIn("FROM app.list_action_rules", executed_sql)

    @patch("filmy.runtime_postgres._connect")
    def test_fetch_list_action_rules_can_match_exact_target_or_wildcard(self, connect_mock) -> None:
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn

        runtime_postgres.fetch_list_action_rules(
            source_list_id="watchlist",
            trigger_action="move_to_list",
            target_list_id="stahnout",
            target_match_mode="exact_or_wildcard",
        )

        executed_sql, parameters = cursor.execute.call_args.args
        self.assertIn("target_list_id IS NULL OR target_list_id = %s::text", executed_sql)
        self.assertEqual(parameters[3], "exact_or_wildcard")
        self.assertEqual(parameters[7], "stahnout")

    @patch("filmy.runtime_postgres._connect")
    def test_upsert_title_session_returns_stored_row(self, connect_mock) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "session-1",
            "tt0133093",
            "open",
            "title_detail",
            "/titles/tt0133093",
            "watchlist",
            "title_detail",
            datetime(2026, 7, 29, 11, 0, 0),
            datetime(2026, 7, 29, 11, 0, 0),
            None,
        )
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn

        result = runtime_postgres.upsert_title_session(
            session_id="session-1",
            tconst="tt0133093",
            status="open",
            opened_from="title_detail",
            return_to_url="/titles/tt0133093",
            source_list_id="watchlist",
            session_scope="title_detail",
            started_at="2026-07-29T11:00:00",
        )

        self.assertEqual(result["session_id"], "session-1")
        self.assertEqual(result["source_list_id"], "watchlist")
        executed_sql = cursor.execute.call_args.args[0]
        self.assertIn("INSERT INTO app.title_sessions", executed_sql)
        self.assertIn("ON CONFLICT (session_id) DO UPDATE", executed_sql)
        conn.commit.assert_called_once_with()

    @patch("filmy.runtime_postgres._connect")
    def test_insert_title_session_action_returns_payload(self, connect_mock) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "action-1",
            "session-1",
            "tt0133093",
            "watchlist",
            "set_rating",
            None,
            9,
            "skvely finale",
            {"source": "manual"},
            1,
            datetime(2026, 7, 29, 11, 5, 0),
        )
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn

        result = runtime_postgres.insert_title_session_action(
            action_id="action-1",
            session_id="session-1",
            tconst="tt0133093",
            source_list_id="watchlist",
            trigger_action="set_rating",
            target_list_id=None,
            rating_value=9,
            notes_text="skvely finale",
            action_payload={"source": "manual"},
            action_order=1,
            created_at="2026-07-29T11:05:00",
        )

        self.assertEqual(result["rating_value"], 9)
        self.assertEqual(result["action_payload"], {"source": "manual"})
        executed_sql = cursor.execute.call_args.args[0]
        self.assertIn("INSERT INTO app.title_session_actions", executed_sql)
        self.assertIn("%s::jsonb", executed_sql)
        conn.commit.assert_called_once_with()

    @patch("filmy.runtime_postgres._title_session_store.insert_effect_rows")
    @patch("filmy.runtime_postgres._title_session_store.fetch_effect_queue")
    @patch("filmy.runtime_postgres._title_session_store.fetch_rules")
    @patch("filmy.runtime_postgres._title_session_store.fetch_action")
    def test_queue_title_session_action_effects_builds_immediate_and_finalize_rows(
        self,
        fetch_action_mock,
        fetch_rules_mock,
        fetch_effect_queue_mock,
        insert_effect_rows_mock,
    ) -> None:
        fetch_action_mock.return_value = {
            "action_id": "action-1",
            "session_id": "session-1",
            "tconst": "tt0133093",
            "source_list_id": "watchlist",
            "trigger_action": "set_rating",
            "target_list_id": None,
            "rating_value": 9,
            "notes_text": "silny finale",
            "action_payload": {"canonical_key": "title:tt0133093", "media_type": "title"},
            "action_order": 1,
            "created_at": datetime(2026, 7, 29, 12, 0, 0),
        }
        fetch_rules_mock.return_value = [
            {
                "rule_id": "rule-1",
                "effect_type": "write_rating",
                "phase": "immediate",
                "target_list_id": None,
            },
            {
                "rule_id": "rule-2",
                "effect_type": "write_watched",
                "phase": "finalize_only",
                "target_list_id": None,
            },
        ]
        fetch_effect_queue_mock.return_value = []

        result = runtime_postgres.queue_title_session_action_effects(
            "action-1",
            queued_at="2026-07-29T12:01:00",
        )

        self.assertEqual(result["queued_count"], 2)
        self.assertEqual(result["immediate_count"], 1)
        self.assertEqual(result["finalize_only_count"], 1)
        queued_rows = insert_effect_rows_mock.call_args.args[0]
        self.assertEqual(queued_rows[0]["effect_order"], 10)
        self.assertEqual(queued_rows[1]["effect_order"], 20)
        self.assertEqual(queued_rows[0]["effect_payload"]["rating_value"], 9)

    @patch("filmy.runtime_postgres._title_session_store.update_effect_status")
    @patch("filmy.runtime_postgres.archive_user_list_item")
    @patch("filmy.runtime_postgres.record_watched")
    @patch("filmy.runtime_postgres._title_session_store.fetch_effect_queue")
    def test_apply_title_session_effects_executes_finalize_rows(
        self,
        fetch_effect_queue_mock,
        record_watched_mock,
        archive_item_mock,
        update_effect_status_mock,
    ) -> None:
        fetch_effect_queue_mock.return_value = [
            {
                "effect_id": "effect-1",
                "session_id": "session-1",
                "action_id": "action-1",
                "rule_id": "rule-1",
                "tconst": "tt0133093",
                "effect_type": "write_watched",
                "phase": "finalize_only",
                "source_list_id": "watchlist",
                "target_list_id": None,
                "effect_status": "pending",
                "effect_order": 10,
                "effect_payload": {"tconst": "tt0133093", "event_scope": "title", "watched_on": "2026-07-29"},
                "created_at": datetime(2026, 7, 29, 12, 2, 0),
                "executed_at": None,
            },
            {
                "effect_id": "effect-2",
                "session_id": "session-1",
                "action_id": "action-1",
                "rule_id": "rule-2",
                "tconst": "tt0133093",
                "effect_type": "deactivate_source_membership",
                "phase": "finalize_only",
                "source_list_id": "watchlist",
                "target_list_id": None,
                "effect_status": "pending",
                "effect_order": 20,
                "effect_payload": {"canonical_key": "title:tt0133093"},
                "created_at": datetime(2026, 7, 29, 12, 2, 0),
                "executed_at": None,
            },
        ]
        update_effect_status_mock.side_effect = lambda effect_id, **kwargs: {"effect_id": effect_id, **kwargs}

        result = runtime_postgres.apply_title_session_effects(
            "session-1",
            phase="finalize_only",
            executed_at="2026-07-29T12:03:00",
        )

        self.assertEqual(result["applied_count"], 2)
        record_watched_mock.assert_called_once()
        archive_item_mock.assert_called_once_with("watchlist", "title:tt0133093", "2026-07-29T12:03:00")
        self.assertEqual(update_effect_status_mock.call_count, 2)

    @patch("filmy.runtime_postgres._title_session_store.update_effect_status")
    @patch("filmy.runtime_postgres.upsert_user_list_item")
    @patch("filmy.runtime_postgres._title_session_store.fetch_effect_queue")
    def test_apply_title_session_effects_adds_group_items_to_target_list(
        self,
        fetch_effect_queue_mock,
        upsert_item_mock,
        update_effect_status_mock,
    ) -> None:
        fetch_effect_queue_mock.return_value = [
            {
                "effect_id": "effect-1",
                "session_id": "session-1",
                "action_id": "action-1",
                "rule_id": "rule-1",
                "tconst": "tt0133093",
                "effect_type": "add_target_membership",
                "phase": "immediate",
                "source_list_id": "watchlist",
                "target_list_id": "stahnout",
                "effect_status": "pending",
                "effect_order": 10,
                "effect_payload": {
                    "group_items": [
                        {"canonical_key": "title:tt0133093", "media_type": "title", "tconst": "tt0133093", "source_ref": "one"},
                        {"canonical_key": "title:tt0234215", "media_type": "title", "tconst": "tt0234215", "source_ref": "two"},
                    ]
                },
                "created_at": datetime(2026, 7, 29, 12, 2, 0),
                "executed_at": None,
            }
        ]
        update_effect_status_mock.side_effect = lambda effect_id, **kwargs: {"effect_id": effect_id, **kwargs}

        result = runtime_postgres.apply_title_session_effects(
            "session-1",
            phase="immediate",
            executed_at="2026-07-29T12:03:00",
        )

        self.assertEqual(result["applied_count"], 1)
        self.assertEqual(upsert_item_mock.call_count, 2)

    @patch("filmy.runtime_postgres._title_session_store.update_session_status")
    @patch("filmy.runtime_postgres._title_session_orchestrator.apply_effects")
    def test_finalize_title_session_updates_session_state(self, apply_effects_mock, update_session_status_mock) -> None:
        update_session_status_mock.side_effect = [
            {"session_id": "session-1", "status": "finalizing"},
            {"session_id": "session-1", "status": "finalized", "finalized_at": datetime(2026, 7, 29, 12, 5, 0)},
        ]
        apply_effects_mock.return_value = {"applied_count": 2, "failed_count": 0, "skipped_count": 0, "effects": []}

        result = runtime_postgres.finalize_title_session(
            "session-1",
            finalized_at="2026-07-29T12:05:00",
        )

        self.assertEqual(result["session"]["status"], "finalized")
        self.assertEqual(result["finalize"]["applied_count"], 2)
        self.assertEqual(update_session_status_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
