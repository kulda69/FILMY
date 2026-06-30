from __future__ import annotations

from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
ASSETS_DIR = DATA_DIR / "assets" / "tmdb"
PEOPLE_ASSETS_DIR = DATA_DIR / "assets" / "people"
DB_PATH = DATA_DIR / "filmy.duckdb"
IMDB_DIR = PROJECT_ROOT / "imdb"
IMDB_LISTS_DIR = PROJECT_ROOT / "imdb_lists"
TRAKT_EXPORT_DIR = PROJECT_ROOT / "trakt-export"
ENV_PATH = PROJECT_ROOT / ".env"
