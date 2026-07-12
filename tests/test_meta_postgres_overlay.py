from __future__ import annotations

import unittest
from unittest.mock import patch

from filmy import db
from filmy.db_bootstrap import seed_local_library


class _FakeConn:
    def execute(self, sql, params=None):
        raise AssertionError("DuckDB path should not be used in this test")


class MetaPostgresOverlayTests(unittest.TestCase):
    def test_get_imdb_manifest_reads_postgres_when_enabled(self) -> None:
        with (
            patch("filmy.db.meta_backend_uses_postgres", return_value=True),
            patch("filmy.db.fetch_imdb_manifest_rows", return_value=[{"source_key": "title_basics"}]),
        ):
            rows = db.get_imdb_manifest()
        self.assertEqual(rows[0]["source_key"], "title_basics")

    def test_title_cache_fingerprint_reads_postgres_when_enabled(self) -> None:
        with (
            patch("filmy.db.meta_backend_uses_postgres", return_value=True),
            patch("filmy.db.fetch_catalog_refresh_fingerprint", return_value="a=1|b=2"),
        ):
            value = db._title_cache_source_fingerprint(
                None,
                "tt1",
                detail={"tconst": "tt1", "primary_title": "Alpha", "genres": [], "tmdb": {}, "library": {}, "content_state": {}, "aliases": []},
            )
        self.assertIsInstance(value, str)

    def test_seed_local_library_uses_postgres_seed_guard_when_enabled(self) -> None:
        with (
            patch("filmy.db_bootstrap._db") as db_mock,
        ):
            db_mock.return_value.meta_backend_uses_postgres.return_value = True
            db_mock.return_value.local_seed_exists.return_value = True
            seed_local_library(_FakeConn())
        db_mock.return_value.local_seed_exists.assert_called_once_with("initial_import_unification")


if __name__ == "__main__":
    unittest.main()
