from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from filmy import db


class _FakeDuckConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ImportPostgresOverlayTests(unittest.TestCase):
    def test_create_import_preview_writes_postgres_when_enabled(self) -> None:
        fake_conn = _FakeDuckConn()
        with (
            patch("filmy.db.import_backend_uses_postgres", return_value=True),
            patch("filmy.db.duckdb.connect", return_value=fake_conn),
            patch("filmy.db._build_resolution_context", return_value={}),
            patch("filmy.db._parse_import_rows", return_value=[{"parsed_title": "Alpha", "parsed_watched_on": "2026-07-10"}]),
            patch("filmy.db._resolve_import_row", return_value={"status": "resolved", "tconst": "tt1", "confidence": 0.9, "note": "ok"}),
            patch("filmy.db.create_import_batch_record") as create_batch,
            patch("filmy.db.insert_import_rows") as insert_rows,
        ):
            result = db.create_import_preview("netflix", "netflix.csv", b"title")

        create_batch.assert_called_once()
        insert_rows.assert_called_once()
        self.assertEqual(result["rows_total"], 1)
        self.assertEqual(result["rows_resolved"], 1)

    def test_get_import_batch_reads_postgres_when_enabled(self) -> None:
        with (
            patch("filmy.db.import_backend_uses_postgres", return_value=True),
            patch(
                "filmy.db.fetch_import_batch_record",
                return_value={"id": "b1", "source": "netflix", "filename": "n.csv", "checksum": "x", "status": "previewed", "created_at": "2026-07-11T12:00:00"},
            ),
            patch(
                "filmy.db.fetch_import_batch_rows",
                return_value=[{"row_number": 1, "parsed_title": "Alpha", "resolution_status": "resolved"}],
            ),
        ):
            batch = db.get_import_batch("b1")

        assert batch is not None
        self.assertEqual(batch["id"], "b1")
        self.assertEqual(batch["rows"][0]["parsed_title"], "Alpha")

    def test_commit_import_batch_writes_postgres_watch_events_when_enabled(self) -> None:
        with (
            patch("filmy.db.import_backend_uses_postgres", return_value=True),
            patch(
                "filmy.db.fetch_import_batch_record",
                return_value={"id": "b1", "status": "previewed"},
            ),
            patch(
                "filmy.db.commit_import_batch_postgres",
                return_value={"inserted_events": 1, "skipped_events": 0, "batch_status": "committed"},
            ) as commit_batch,
        ):
            result = db.commit_import_batch("b1")

        commit_batch.assert_called_once()
        self.assertEqual(result["committed"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["status"], "committed")


if __name__ == "__main__":
    unittest.main()
