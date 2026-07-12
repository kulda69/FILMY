from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

from filmy.paths import PROJECT_ROOT


CONFIG_PATH = PROJECT_ROOT / "config.toml"


@dataclass(frozen=True)
class UiConfig:
    continue_watching_limit: int = 12
    my_lists_selected_limit: int = 50
    recently_watched_days: int = 183
    hot_watchlist_limit: int = 50
    search_recall_limit: int = 500
    tmdb_primary_language: str = "EN"
    runtime_content_state_backend: str = "duckdb"
    runtime_user_ratings_backend: str = "duckdb"
    runtime_watch_events_backend: str = "duckdb"
    runtime_user_lists_backend: str = "duckdb"
    runtime_app_state_backend: str = "duckdb"
    runtime_import_backend: str = "duckdb"
    runtime_catalog_backend: str = "duckdb"
    runtime_tmdb_backend: str = "duckdb"
    runtime_meta_backend: str = "duckdb"

    @property
    def tmdb_primary_locale(self) -> str:
        return "cs-CZ" if self.tmdb_primary_language == "CZ" else "en-US"

    @property
    def tmdb_fallback_locale(self) -> str:
        return "en-US" if self.tmdb_primary_language == "CZ" else "cs-CZ"

    @property
    def tmdb_locale_order(self) -> tuple[str, str]:
        return (self.tmdb_primary_locale, self.tmdb_fallback_locale)


_CONFIG_CACHE: UiConfig | None = None
_CONFIG_MTIME_NS: int | None = None


def load_ui_config(path: Path = CONFIG_PATH) -> UiConfig:
    if not path.exists():
        return UiConfig()

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    primary_language = str(raw.get("tmdb_primary_language", "EN")).strip().upper()
    if primary_language not in {"EN", "CZ"}:
        primary_language = "EN"
    content_state_backend = str(raw.get("runtime_content_state_backend", "duckdb")).strip().lower()
    if content_state_backend not in {"duckdb", "postgres"}:
        content_state_backend = "duckdb"
    user_ratings_backend = str(raw.get("runtime_user_ratings_backend", "duckdb")).strip().lower()
    if user_ratings_backend not in {"duckdb", "postgres"}:
        user_ratings_backend = "duckdb"
    watch_events_backend = str(raw.get("runtime_watch_events_backend", "duckdb")).strip().lower()
    if watch_events_backend not in {"duckdb", "postgres"}:
        watch_events_backend = "duckdb"
    user_lists_backend = str(raw.get("runtime_user_lists_backend", "duckdb")).strip().lower()
    if user_lists_backend not in {"duckdb", "postgres"}:
        user_lists_backend = "duckdb"
    app_state_backend = str(raw.get("runtime_app_state_backend", "duckdb")).strip().lower()
    if app_state_backend not in {"duckdb", "postgres"}:
        app_state_backend = "duckdb"
    import_backend = str(raw.get("runtime_import_backend", "duckdb")).strip().lower()
    if import_backend not in {"duckdb", "postgres"}:
        import_backend = "duckdb"
    catalog_backend = str(raw.get("runtime_catalog_backend", "duckdb")).strip().lower()
    if catalog_backend not in {"duckdb", "postgres"}:
        catalog_backend = "duckdb"
    tmdb_backend = str(raw.get("runtime_tmdb_backend", "duckdb")).strip().lower()
    if tmdb_backend not in {"duckdb", "postgres"}:
        tmdb_backend = "duckdb"
    meta_backend = str(raw.get("runtime_meta_backend", "duckdb")).strip().lower()
    if meta_backend not in {"duckdb", "postgres"}:
        meta_backend = "duckdb"

    return UiConfig(
        continue_watching_limit=max(1, int(raw.get("continue_watching_limit", 12))),
        my_lists_selected_limit=max(1, int(raw.get("my_lists_selected_limit", 50))),
        recently_watched_days=max(1, int(raw.get("recently_watched_days", 183))),
        hot_watchlist_limit=max(1, int(raw.get("hot_watchlist_limit", 50))),
        search_recall_limit=max(1, int(raw.get("search_recall_limit", 500))),
        tmdb_primary_language=primary_language,
        runtime_content_state_backend=content_state_backend,
        runtime_user_ratings_backend=user_ratings_backend,
        runtime_watch_events_backend=watch_events_backend,
        runtime_user_lists_backend=user_lists_backend,
        runtime_app_state_backend=app_state_backend,
        runtime_import_backend=import_backend,
        runtime_catalog_backend=catalog_backend,
        runtime_tmdb_backend=tmdb_backend,
        runtime_meta_backend=meta_backend,
    )

def get_ui_config(path: Path = CONFIG_PATH) -> UiConfig:
    """Return current UI config and reload it when config.toml changes."""

    global _CONFIG_CACHE, _CONFIG_MTIME_NS

    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        mtime_ns = None

    if _CONFIG_CACHE is None or _CONFIG_MTIME_NS != mtime_ns:
        _CONFIG_CACHE = load_ui_config(path)
        _CONFIG_MTIME_NS = mtime_ns

    return _CONFIG_CACHE
