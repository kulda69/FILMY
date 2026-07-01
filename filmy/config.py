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

    return UiConfig(
        continue_watching_limit=max(1, int(raw.get("continue_watching_limit", 12))),
        my_lists_selected_limit=max(1, int(raw.get("my_lists_selected_limit", 50))),
        recently_watched_days=max(1, int(raw.get("recently_watched_days", 183))),
        hot_watchlist_limit=max(1, int(raw.get("hot_watchlist_limit", 50))),
        search_recall_limit=max(1, int(raw.get("search_recall_limit", 500))),
        tmdb_primary_language=primary_language,
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
