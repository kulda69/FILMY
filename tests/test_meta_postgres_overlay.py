from __future__ import annotations

import unittest
from unittest.mock import patch

from filmy import db


class MetaPostgresOverlayTests(unittest.TestCase):
    def test_get_imdb_manifest_reads_postgres(self) -> None:
        with (
            patch("filmy.db.fetch_imdb_manifest_rows", return_value=[{"source_key": "title_basics"}]),
        ):
            rows = db.get_imdb_manifest()
        self.assertEqual(rows[0]["source_key"], "title_basics")

    def test_title_cache_fingerprint_reads_postgres(self) -> None:
        with (
            patch("filmy.db.fetch_catalog_refresh_fingerprint", return_value="a=1|b=2"),
        ):
            value = db._title_cache_source_fingerprint(
                None,
                "tt1",
                detail={"tconst": "tt1", "primary_title": "Alpha", "genres": [], "tmdb": {}, "library": {}, "content_state": {}, "aliases": []},
            )
        self.assertIsInstance(value, str)


if __name__ == "__main__":
    unittest.main()
