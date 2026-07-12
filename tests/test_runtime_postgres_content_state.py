from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from filmy.config import load_ui_config
from filmy import runtime_postgres


class UiConfigTests(unittest.TestCase):
    def test_runtime_content_state_backend_defaults_to_duckdb(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("", encoding="utf-8")
            config = load_ui_config(config_path)
        self.assertEqual(config.runtime_content_state_backend, "duckdb")

    def test_runtime_content_state_backend_accepts_postgres(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('runtime_content_state_backend = "postgres"\n', encoding="utf-8")
            config = load_ui_config(config_path)
        self.assertEqual(config.runtime_content_state_backend, "postgres")

    def test_runtime_user_ratings_backend_accepts_postgres(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('runtime_user_ratings_backend = "postgres"\n', encoding="utf-8")
            config = load_ui_config(config_path)
        self.assertEqual(config.runtime_user_ratings_backend, "postgres")

    def test_runtime_watch_events_backend_accepts_postgres(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('runtime_watch_events_backend = "postgres"\n', encoding="utf-8")
            config = load_ui_config(config_path)
        self.assertEqual(config.runtime_watch_events_backend, "postgres")


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
            ("tt1", 8, datetime(2026, 7, 11, 21, 15, 0), datetime(2026, 7, 11, 21, 15, 0)),
            ("tt2", 6, None, datetime(2026, 7, 10, 10, 0, 0)),
        ]
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        connect_mock.return_value.__enter__.return_value = conn
        result = runtime_postgres.fetch_latest_ratings_for_tconsts(["tt1", "tt2"])
        self.assertEqual(result["tt1"]["rating"], 8)
        self.assertEqual(result["tt2"]["updated_at"], datetime(2026, 7, 10, 10, 0, 0))

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


if __name__ == "__main__":
    unittest.main()
