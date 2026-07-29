"""Centralni facade nad PostgreSQL runtime, katalogem a rozdelenymi DB moduly."""

from __future__ import annotations
import csv
import difflib
import hashlib
import io
import json
import logging
from math import sqrt
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Sequence
from filmy.config import get_ui_config
from filmy.runtime_postgres import _connect as _pg_connect, create_import_batch_record, commit_import_batch as commit_import_batch_postgres, fetch_catalog_brief_rows, fetch_catalog_genres as fetch_catalog_genres_postgres, fetch_catalog_search_rows, fetch_active_user_list_items, fetch_all_watch_events, fetch_catalog_episode_row, fetch_content_state as fetch_content_state_postgres, fetch_episode_series_map, fetch_catalog_primary_title, fetch_catalog_stats_row as fetch_catalog_stats_row_postgres, fetch_favorite_genres as fetch_favorite_genres_postgres, fetch_favorite_traits as fetch_favorite_traits_postgres, fetch_ai_noted_title_rows, fetch_ai_rated_title_rows, fetch_ai_taste_seed_rows, fetch_ai_watched_title_rows, fetch_latest_ai_recommendation_for_title as fetch_latest_ai_recommendation_for_title_postgres, fetch_genre_score_source_rows as fetch_genre_score_source_rows_postgres, fetch_home_suggestion_candidate_rows as fetch_home_suggestion_candidate_rows_postgres, fetch_import_batch_record, fetch_import_batch_rows, fetch_known_for_title_rows, fetch_latest_rating_for_tconst as fetch_latest_rating_for_tconst_postgres, fetch_latest_ratings_for_tconsts, fetch_latest_genre_scores as fetch_latest_genre_scores_postgres, fetch_latest_tmdb_assets_for_title, fetch_catalog_refresh_fingerprint, fetch_catalog_refresh_rows, fetch_catalog_title_row, fetch_imdb_manifest_rows, fetch_library_summary_snapshot, fetch_person_catalog_row, fetch_person_credit_rows, fetch_person_lookup_row, fetch_person_affinity_rating as fetch_person_affinity_rating_postgres, fetch_person_episode_series_credit_rows, fetch_people_for_lookup_fuzzy_rows, fetch_people_for_lookup_levenshtein_rows, fetch_people_for_lookup_rows, fetch_positive_person_affinities, fetch_relevant_people_candidate_rows, fetch_search_recall_match, fetch_series_episode_rows, fetch_title_alias_rows, fetch_title_alias_lookup_matches, fetch_title_by_primary_title_year, fetch_title_lookup_primary_key_matches, fetch_title_overviews, fetch_title_card_detail_rows, fetch_title_people_rows, fetch_title_people_preview_rows, fetch_tconst_for_tmdb_id, fetch_tmdb_completion_flags, fetch_tmdb_mapping_record, fetch_tmdb_payload_snapshot, fetch_user_lists, fetch_watch_view_page_rows, fetch_watch_history as fetch_watch_history_postgres, fetch_watch_stats_for_tconsts, fetch_primary_title_matches, insert_import_rows, insert_tmdb_asset_record, local_seed_exists, list_in_progress_content_states, insert_genre_score_snapshot, record_local_seed_meta, record_search_recall_entry as record_search_recall_entry_postgres, replace_catalog_refresh_meta_rows, replace_favorite_genres as replace_favorite_genres_postgres, replace_favorite_traits as replace_favorite_traits_postgres, replace_imdb_manifest_rows, store_tmdb_payload_bundle, upsert_tmdb_mapping_record
from filmy.genre_scoring import compute_genre_scores
from filmy.integrations.plex import get_library_sections, get_metadata_snapshot, get_primary_server, iter_section_items
from filmy.paths import ASSETS_DIR, IMDB_DIR, PEOPLE_ASSETS_DIR, POSTGRES_DATABASE_NAME, PROJECT_ROOT
from filmy.suggestion_engine import evaluate_new_imdb_candidate, evaluate_trait_candidate
BASE_DIR = PROJECT_ROOT
TITLE_PRESENTATION_CACHE_VERSION = 3
logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class SourceFile:
    """Popis jednoho zdrojoveho IMDb souboru pro startup drift kontrolu."""

    key: str
    path: Path

    @property
    def stat_signature(self) -> str:
        """Vrati kompakni podpis `mtime:size` pro rychle porovnani souboru."""

        stat = self.path.stat()
        return f'{int(stat.st_mtime)}:{stat.st_size}'

    @property
    def stat_mtime(self) -> int:
        """Vrati `mtime` zdrojoveho souboru jako cele sekundy."""

        return int(self.path.stat().st_mtime)

    @property
    def stat_size(self) -> int:
        """Vrati velikost zdrojoveho souboru v bajtech."""

        return self.path.stat().st_size

    @property
    def sha256(self) -> str:
        """Spocita SHA-256 hash celeho zdrojoveho souboru."""

        digest = hashlib.sha256()
        with self.path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
SOURCE_FILES = (SourceFile('title_basics', IMDB_DIR / 'title.basics.tsv'), SourceFile('title_ratings', IMDB_DIR / 'title.ratings.tsv'), SourceFile('title_episode', IMDB_DIR / 'title.episode.tsv'), SourceFile('title_akas', IMDB_DIR / 'title.akas.tsv'), SourceFile('title_crew', IMDB_DIR / 'title.crew.tsv'), SourceFile('title_principals', IMDB_DIR / 'title.principals.tsv'), SourceFile('name_basics', IMDB_DIR / 'name.basics.tsv'))

def ensure_database() -> None:
    """Inicializuje aktivní katalogový backend a případně provede lehký startup refresh check."""
    ASSETS_DIR.parent.parent.mkdir(exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    PEOPLE_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_postgres_catalog_startup()
    return

def _ensure_postgres_catalog_startup() -> None:
    """Overi startup stav katalogu a pri realnem driftu spusti rebuild."""

    from filmy.scripts.rebuild_catalog_postgresql import rebuild_catalog_from_current_imdb
    with _pg_connect() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('app.catalog_titles')")
        catalog_relation = cursor.fetchone()[0]
    if catalog_relation is None:
        rebuild_catalog_from_current_imdb(force=True)
        return
    stored = {row['source_key']: {'path': row['source_path'], 'mtime': row['source_mtime'], 'size': row['source_size'], 'sha256': row['source_sha256']} for row in fetch_imdb_manifest_rows()}
    meta_rows = fetch_catalog_refresh_rows()
    source_files_present = all(source.path.exists() for source in SOURCE_FILES)
    if not stored or not meta_rows:
        if not source_files_present:
            logger.info('PostgreSQL catalog startup: local IMDb TSV files are missing, but catalog already exists; skipping rebuild check.')
            return
        rebuild_catalog_from_current_imdb(force=False)
        return
    if not source_files_present:
        logger.info('PostgreSQL catalog startup: local IMDb TSV files are missing, but catalog metadata already exists; skipping file drift check.')
        return
    manifest_needs_update = False
    for source in SOURCE_FILES:
        current_mtime = source.stat_mtime
        current_size = source.stat_size
        current_path = source.path.as_posix()
        stored_row = stored.get(source.key)
        if stored_row is None:
            rebuild_catalog_from_current_imdb(force=False)
            return
        if stored_row['size'] != current_size:
            rebuild_catalog_from_current_imdb(force=False)
            return
        path_changed = stored_row['path'] != current_path
        mtime_changed = stored_row['mtime'] != current_mtime
        if path_changed or mtime_changed:
            if stored_row['sha256'] != source.sha256:
                rebuild_catalog_from_current_imdb(force=False)
                return
            manifest_needs_update = True
    if manifest_needs_update:
        _store_imdb_file_manifest(None)
        _store_catalog_refresh_meta(None)

def _normalize_search_query_text(value: str | None) -> str:
    """Normalizuje uzivatelsky dotaz pro search-recall vrstvu."""

    return ' '.join(str(value or '').strip().split())

def _search_recall_entry_id(entity_type: str, query_text_fold: str, target_id: str) -> str:
    """Sestavi deterministicke ID zaznamu search recall tabulky."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'search-recall|{entity_type}|{query_text_fold}|{target_id}'))

def _prune_search_recall_entries(conn, limit: int) -> None:
    """Orizne recall tabulku na maximalni pocet nejcerstvejsich zaznamu."""

    conn.execute('\n        DELETE FROM app.search_recall\n        WHERE id IN (\n            SELECT id\n            FROM app.search_recall\n            ORDER BY last_searched_at DESC, hit_count DESC, first_searched_at DESC, id DESC\n            OFFSET ?\n        )\n        ', [max(limit, 0)])

def _record_search_recall_entry(*, entity_type: str, query: str, target_id: str, target_label: str | None, target_title_type: str | None=None, matched_alias_title: str | None=None, fuzzy_score: float | None=None) -> None:
    """Remember one successful query-to-target mapping for fast repeated lookup.

    The table is intentionally small and lossy: it is not meant to be a full
    search log, only a shortcut layer for repeated searches that recently led
    to a concrete IMDb title or person.
    """
    query_text = _normalize_search_query_text(query)
    query_key = _normalize_match_key(query)
    if not query_text or not query_key or (not target_id):
        return
    now = _now_iso()
    query_text_fold = query_text.casefold()
    recall_id = _search_recall_entry_id(entity_type, query_text_fold, target_id)
    recall_limit = get_ui_config().search_recall_limit
    record_search_recall_entry_postgres(entry_id=recall_id, entity_type=entity_type, query_text=query_text, query_text_fold=query_text_fold, query_key=query_key, target_id=target_id, target_label=target_label, target_title_type=target_title_type, matched_alias_title=matched_alias_title, fuzzy_score=fuzzy_score, now=now, recall_limit=recall_limit)
    return

def clear_title_presentation_cache() -> None:
    """Vymaze in-memory cache title presentation."""

    _get_title_presentation_cached.cache_clear()

def invalidate_title_presentation_cache(tconst: str | None = None) -> None:
    """Clear cached title presentation and drop one disk snapshot when possible."""
    clear_title_presentation_cache()
    cleaned_tconst = (tconst or '').strip()
    if not cleaned_tconst:
        return
    try:
        _title_detail_cache_path(cleaned_tconst).unlink(missing_ok=True)
    except OSError:
        return

def get_catalog_stats() -> dict[str, int | str | None]:
    """Vrati souhrnne statistiky katalogu doplnene o lokalni metadata."""

    stats = fetch_catalog_stats_row_postgres()
    return {**stats, 'database': POSTGRES_DATABASE_NAME, 'assets_path': ASSETS_DIR.as_posix()}

def get_imdb_manifest() -> list[dict[str, Any]]:
    """Vrati ulozeny manifest lokalnich IMDb zdrojovych souboru."""

    return fetch_imdb_manifest_rows()

def search_catalog(query: str | None, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Vyhleda tituly v katalogu a doplni k nim lokalni library souhrn."""

    rows = fetch_catalog_search_rows(query=query, title_type=title_type, limit=limit)
    items = [_catalog_row_to_dict(row) for row in rows]
    for item in items:
        item['library'] = _fetch_library_summary(None, item['tconst'], item['title_type'])
    return items

def get_content_detail(tconst: str) -> dict[str, Any] | None:
    """Slozi zakladni detail titulu nebo epizody z katalogu a lokalnich dat."""

    title = fetch_catalog_title_row(tconst)
    if title is None:
        episode = fetch_catalog_episode_row(tconst)
        if episode is None:
            return None
        return {'tconst': episode[0], 'kind': 'episode', 'series_tconst': episode[1], 'season_number': episode[2], 'episode_number': episode[3], 'primary_title': episode[4], 'original_title': episode[5], 'start_year': episode[6], 'runtime_minutes': episode[7], 'aliases': _fetch_aliases(None, tconst), 'tmdb': _fetch_tmdb(None, tconst), 'content_state': _fetch_content_state(None, tconst), 'library': _fetch_library_summary(None, tconst, 'tvEpisode')}
    detail = {'kind': 'title', **_catalog_row_to_dict(title), 'aliases': _fetch_aliases(None, tconst), 'tmdb': _fetch_tmdb(None, tconst), 'content_state': _fetch_content_state(None, tconst), 'library': _fetch_library_summary(None, tconst, title[1])}
    if title[1] in ('tvSeries', 'tvMiniSeries'):
        episode_rows = fetch_series_episode_rows(tconst)
        episode_tconsts = [str(row[0]) for row in episode_rows]
        ratings_by_tconst = fetch_latest_ratings_for_tconsts(episode_tconsts)
        watch_stats_by_tconst = fetch_watch_stats_for_tconsts(episode_tconsts)
        detail['episodes'] = [(row[0], row[1], row[2], row[3], row[4], ratings_by_tconst.get(str(row[0]), {}).get('rating'), watch_stats_by_tconst.get(str(row[0]), {}).get('watched_count', 0)) for row in episode_rows]
    return detail

def describe_title_by_query(query: str, title_type: str | None=None) -> dict[str, Any] | None:
    """Kompatibilni facade pro title lookup popis z `db_lookup`."""

    from filmy.db_lookup import describe_title_by_query as _impl
    return _impl(query, title_type=title_type)

def describe_person_by_query(query: str) -> dict[str, Any] | None:
    """Kompatibilni facade pro person lookup popis z `db_lookup`."""

    from filmy.db_lookup import describe_person_by_query as _impl
    return _impl(query)

def _title_candidate_from_presentation(presentation: dict[str, Any], *, fuzzy_score: float | None=None, matched_alias_title: str | None=None) -> dict[str, Any]:
    """Prevede title presentation na lookup kandidata."""

    from filmy.db_lookup import _title_candidate_from_presentation as _impl
    return _impl(presentation, fuzzy_score=fuzzy_score, matched_alias_title=matched_alias_title)

def _person_candidate_from_presentation(presentation: dict[str, Any], *, fuzzy_score: float | None=None) -> dict[str, Any]:
    """Prevede person presentation na lookup kandidata."""

    from filmy.db_lookup import _person_candidate_from_presentation as _impl
    return _impl(presentation, fuzzy_score=fuzzy_score)

def _lookup_title_from_search_recall(query: str, *, title_type: str | None, candidates_limit: int) -> dict[str, Any] | None:
    """Zkusi vratit title lookup primo z recall vrstvy."""

    from filmy.db_lookup import _lookup_title_from_search_recall as _impl
    return _impl(query, title_type=title_type, candidates_limit=candidates_limit)

def _remember_title_lookup(query: str, selected: dict[str, Any]) -> None:
    """Ulozi uspesny title lookup do recall vrstvy."""

    from filmy.db_lookup import _remember_title_lookup as _impl
    return _impl(query, selected)

def lookup_title_by_query(query: str, title_type: str | None=None, candidates_limit: int=5, allow_expensive_fallback: bool=False) -> dict[str, Any] | None:
    """Vyhleda titul podle textu a vrati rozhodnuty lookup vysledek."""

    from filmy.db_lookup import lookup_title_by_query as _impl
    return _impl(query, title_type=title_type, candidates_limit=candidates_limit, allow_expensive_fallback=allow_expensive_fallback)

def lookup_person_by_query(query: str, candidates_limit: int=5) -> dict[str, Any] | None:
    """Vyhleda osobu podle textu a vrati lookup vysledek."""

    from filmy.db_lookup import lookup_person_by_query as _impl
    return _impl(query, candidates_limit=candidates_limit)

def get_person_presentation(nconst: str) -> dict[str, Any] | None:
    """Kompatibilni facade pro person presentation."""

    from filmy.db_people import get_person_presentation as _impl
    return _impl(nconst)

def get_person_portrait_summary(nconst: str) -> dict[str, Any]:
    """Vrati souhrn lokalne dostupneho person portraitu."""

    from filmy.db_presentation import get_person_portrait_summary as _impl
    return _impl(nconst)

def _fetch_known_for_items(conn, known_for_titles: str | None) -> list[dict[str, Any]]:
    """Vrati `known for` tituly pro person presentation cache vrstvy."""

    from filmy.db_presentation import _fetch_known_for_items as _impl
    return _impl(conn, known_for_titles)

def render_person_presentation(presentation: dict[str, Any]) -> str:
    """Vyrenderuje person presentation do HTML fragmentu."""

    from filmy.db_people import render_person_presentation as _impl
    return _impl(presentation)

@lru_cache(maxsize=256)
def _get_title_presentation_cached(tconst: str) -> dict[str, Any] | None:
    """Vrati title presentation pres sdilenou LRU cache."""

    from filmy.db_presentation import _get_title_presentation_cached as _impl
    return _impl(tconst)

def get_title_presentation(tconst: str) -> dict[str, Any] | None:
    """Vrati plnou title presentation pro detail titulu."""

    from filmy.db_presentation import get_title_presentation as _impl
    return _impl(tconst)

def get_title_people_panel(tconst: str) -> dict[str, Any] | None:
    """Vrati lehky people panel pro title detail a partialy."""

    from filmy.db_presentation import get_title_people_panel as _impl
    return _impl(tconst)

def get_title_overviews_for_tconsts(tconsts: Sequence[str]) -> dict[str, str]:
    """Return best available overview texts keyed by tconst."""
    normalized = [str(tconst).strip() for tconst in tconsts if str(tconst).strip()]
    if not normalized:
        return {}
    return fetch_title_overviews(normalized)

def get_title_card_summaries_for_tconsts(tconsts: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Return lightweight card summaries for many titles without full detail assembly."""
    normalized = [str(tconst).strip() for tconst in tconsts if str(tconst).strip()]
    unique_tconsts = [tconst for tconst in dict.fromkeys(normalized) if tconst]
    if not unique_tconsts:
        return {}
    card_rows = fetch_title_card_detail_rows(unique_tconsts)
    preview_rows = fetch_title_people_preview_rows(unique_tconsts)
    preview_by_tconst: dict[str, dict[str, list[str]]] = {}
    for row in preview_rows:
        tconst = str(row[0] or '').strip()
        credit_group = str(row[1] or '').strip()
        primary_name = str(row[3] or '').strip()
        if not tconst or not primary_name:
            continue
        grouped = preview_by_tconst.setdefault(tconst, {'director': [], 'cast': []})
        names = grouped.get(credit_group)
        if names is None or primary_name in names:
            continue
        if credit_group == 'director' and len(names) < 4:
            names.append(primary_name)
        elif credit_group == 'cast' and len(names) < 5:
            names.append(primary_name)
    summaries: dict[str, dict[str, Any]] = {}
    for row in card_rows:
        tconst = str(row[0] or '').strip()
        if not tconst:
            continue
        grouped = preview_by_tconst.get(tconst) or {'director': [], 'cast': []}
        summaries[tconst] = {'tconst': tconst, 'title': row[3], 'original_title': row[4], 'kind_label': _title_type_label(row[1]), 'year': row[2], 'runtime_minutes': row[5], 'genres': [part.strip() for part in str(row[6] or '').split(',') if part.strip()], 'imdb_rating': row[7], 'imdb_votes': row[8], 'poster_url': _poster_url_from_local_path(row[9] or row[10]), 'directed_by_line': ', '.join(grouped['director']) or None, 'main_cast_line': ', '.join(grouped['cast']) or None}
    return summaries

def render_title_presentation(presentation: dict[str, Any]) -> str:
    """Vyrenderuje title presentation do HTML fragmentu."""

    from filmy.db_presentation import render_title_presentation as _impl
    return _impl(presentation)

def _title_detail_cache_path(tconst: str) -> Path:
    """Vrati cestu k disk cache souboru title detailu."""

    from filmy.db_presentation import _title_detail_cache_path as _impl
    return _impl(tconst)

def _person_detail_cache_path(nconst: str) -> Path:
    """Vrati cestu k disk cache souboru person detailu."""

    from filmy.db_presentation import _person_detail_cache_path as _impl
    return _impl(nconst)

def _person_portrait_path(nconst: str) -> Path | None:
    """Vrati lokalni cestu k portrait assetu osoby."""

    from filmy.db_presentation import _person_portrait_path as _impl
    return _impl(nconst)

def _person_portrait_url(nconst: str) -> str | None:
    """Vrati lokalni URL k portrait assetu osoby."""

    from filmy.db_presentation import _person_portrait_url as _impl
    return _impl(nconst)

def _person_biography_path(nconst: str) -> Path:
    """Vrati cestu k lokalni biography cache osoby."""

    from filmy.db_presentation import _person_biography_path as _impl
    return _impl(nconst)

def _person_biography_meta(nconst: str) -> dict[str, Any] | None:
    """Vrati metadata biography cache osoby."""

    from filmy.db_presentation import _person_biography_meta as _impl
    return _impl(nconst)

def _person_biography_payload(nconst: str) -> dict[str, Any] | None:
    """Vrati payload biography cache osoby."""

    from filmy.db_presentation import _person_biography_payload as _impl
    return _impl(nconst)

def _title_detail_cache_status(tconst: str, source_fingerprint: str) -> str:
    """Vyhodnoti stav title detail cache proti aktualnimu fingerprintu."""

    from filmy.db_presentation import _title_detail_cache_status as _impl
    return _impl(tconst, source_fingerprint)

def _person_detail_cache_status(nconst: str, source_fingerprint: str) -> str:
    """Vyhodnoti stav person detail cache proti aktualnimu fingerprintu."""

    from filmy.db_presentation import _person_detail_cache_status as _impl
    return _impl(nconst, source_fingerprint)

def _get_person_affinity_rating(conn, nconst: str) -> int:
    """Vrati affinity rating osoby z PostgreSQL runtime vrstvy."""

    return fetch_person_affinity_rating_postgres(nconst)

def _load_cached_title_presentation(conn, tconst: str) -> dict[str, Any] | None:
    """Nacte title presentation z disk cache."""

    from filmy.db_presentation import _load_cached_title_presentation as _impl
    return _impl(conn, tconst)

def _load_cached_person_presentation(conn, nconst: str) -> dict[str, Any] | None:
    """Nacte person presentation z disk cache."""

    from filmy.db_presentation import _load_cached_person_presentation as _impl
    return _impl(conn, nconst)

def _store_cached_title_presentation(tconst: str, presentation: dict[str, Any], source_fingerprint: str | None) -> None:
    """Ulozi title presentation do disk cache."""

    from filmy.db_presentation import _store_cached_title_presentation as _impl
    return _impl(tconst, presentation, source_fingerprint)

def _store_cached_person_presentation(nconst: str, presentation: dict[str, Any], source_fingerprint: str | None) -> None:
    """Ulozi person presentation do disk cache."""

    from filmy.db_presentation import _store_cached_person_presentation as _impl
    return _impl(nconst, presentation, source_fingerprint)

def _title_cache_source_fingerprint(conn, tconst: str, detail: dict[str, Any] | None=None) -> str | None:
    """Spocita fingerprint zdroju pro title presentation cache."""

    from filmy.db_presentation import _title_cache_source_fingerprint as _impl
    return _impl(conn, tconst, detail)

def _person_cache_source_fingerprint(conn, nconst: str, presentation: dict[str, Any] | None=None) -> str | None:
    """Spocita fingerprint zdroju pro person presentation cache."""

    from filmy.db_presentation import _person_cache_source_fingerprint as _impl
    return _impl(conn, nconst, presentation)

def _fetch_person_cache_source_detail(conn, nconst: str) -> dict[str, Any] | None:
    """Vrati zdrojovy detail, z nehoz se odvozuje person cache fingerprint."""

    from filmy.db_presentation import _fetch_person_cache_source_detail as _impl
    return _impl(conn, nconst)

def _fetch_person_episode_series_credits(conn, nconst: str, *, existing_tconsts: set[str] | None=None) -> list[dict[str, Any]]:
    """Vrati serialove kredity osoby prepojene z epizod na serie."""

    from filmy.db_presentation import _fetch_person_episode_series_credits as _impl
    return _impl(conn, nconst, existing_tconsts=existing_tconsts)

def _fetch_title_cache_source_detail(conn, tconst: str) -> dict[str, Any] | None:
    """Vrati zdrojovy detail, z nehoz se odvozuje title cache fingerprint."""

    from filmy.db_presentation import _fetch_title_cache_source_detail as _impl
    return _impl(conn, tconst)

def _tmdb_asset_summary_signature(tmdb: dict[str, Any]) -> list[dict[str, Any]]:
    """Vrati stabilni podpis TMDB asset summary pro cache kontrolu."""

    from filmy.db_presentation import _tmdb_asset_summary_signature as _impl
    return _impl(tmdb)

def _tmdb_detail_is_cache_ready(tmdb: dict[str, Any] | None) -> bool:
    """Rozhodne, jestli ma TMDB payload dost dat pro validni cache hit."""

    from filmy.db_presentation import _tmdb_detail_is_cache_ready as _impl
    return _impl(tmdb)

def _jsonify_for_cache(value: Any) -> Any:
    """Prevede Python hodnotu na JSON-safe tvar pro cache soubory."""

    from filmy.db_presentation import _jsonify_for_cache as _impl
    return _impl(value)

def update_content_state(tconst: str, interest_state: str) -> dict[str, Any]:
    """Kompatibilni facade pro update content state."""

    from filmy.db_library import update_content_state as _impl
    return _impl(tconst, interest_state)

def set_watchlist_state(tconst: str, *, in_watchlist: bool, notes: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro pridani nebo odebrani z watchlistu."""

    from filmy.db_library import set_watchlist_state as _impl
    return _impl(tconst, in_watchlist=in_watchlist, notes=notes)

def add_title_to_user_list(tconst: str, list_id: str, *, notes: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro pridani titulu do uzivatelskeho seznamu."""

    from filmy.db_library import add_title_to_user_list as _impl
    return _impl(tconst, list_id, notes=notes)

def set_user_rating(tconst: str, rating: int, *, liked_notes: str | None=None, disliked_notes: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro zapis uzivatelskeho ratingu titulu."""

    from filmy.db_library import set_user_rating as _impl
    return _impl(tconst, rating, liked_notes=liked_notes, disliked_notes=disliked_notes)

def set_person_affinity_rating(nconst: str, rating: int) -> dict[str, Any]:
    """Kompatibilni facade pro affinity rating osoby."""

    from filmy.db_library import set_person_affinity_rating as _impl
    return _impl(nconst, rating)

def clear_user_rating(tconst: str) -> dict[str, Any]:
    """Kompatibilni facade pro smazani uzivatelskeho ratingu."""

    from filmy.db_library import clear_user_rating as _impl
    return _impl(tconst)

def get_ai_taste_seed(source_list: str='kouknout-znovu', limit: int=50) -> dict[str, Any]:
    """Kompatibilni facade pro AI taste seed payload."""

    from filmy.db_ai import get_ai_taste_seed as _impl
    return _impl(source_list=source_list, limit=limit)

def get_ai_taste_inputs(limit_per_list: int=25) -> dict[str, Any]:
    """Kompatibilni facade pro AI vstupni seznamy a preference."""

    from filmy.db_ai import get_ai_taste_inputs as _impl
    return _impl(limit_per_list=limit_per_list)

def get_ai_rated_titles(*, min_user_rating: int=8, limit: int=50, title_type: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro AI export pozitivne hodnocenych titulu."""

    from filmy.db_ai import get_ai_rated_titles as _impl
    return _impl(min_user_rating=min_user_rating, limit=limit, title_type=title_type)

def get_ai_noted_titles(*, notes: str='any', min_user_rating: int | None=None, limit: int=50) -> dict[str, Any]:
    """Kompatibilni facade pro AI export titulu s textovymi poznamkami."""

    from filmy.db_ai import get_ai_noted_titles as _impl
    return _impl(notes=notes, min_user_rating=min_user_rating, limit=limit)

def get_ai_watched_titles(*, include_rated: bool=True, include_negative: bool=True) -> dict[str, Any]:
    """Kompatibilni facade pro AI export sledovanych titulu."""

    from filmy.db_ai import get_ai_watched_titles as _impl
    return _impl(include_rated=include_rated, include_negative=include_negative)

def import_ai_recommendations_file(path: str | Path) -> dict[str, Any]:
    """Kompatibilni facade pro import jednoho AI recommendation souboru."""

    from filmy.db_ai import import_ai_recommendations_file as _impl
    return _impl(path)

def list_ai_recommendation_files() -> list[dict[str, Any]]:
    """Kompatibilni facade pro seznam importovatelnych AI JSON souboru."""

    from filmy.db_ai import list_ai_recommendation_files as _impl
    return _impl()

def delete_ai_recommendation_file(filename: str) -> dict[str, Any]:
    """Kompatibilni facade pro smazani AI JSON souboru."""

    from filmy.db_ai import delete_ai_recommendation_file as _impl
    return _impl(filename)

def get_latest_ai_recommendation_for_title(tconst: str) -> dict[str, Any] | None:
    """Kompatibilni facade pro posledni AI doporuceni k titulu."""

    from filmy.db_ai import get_latest_ai_recommendation_for_title as _impl
    return _impl(tconst)

def get_ai_context() -> dict[str, Any]:
    """Kompatibilni facade pro AI kontextovy payload."""

    from filmy.db_ai import get_ai_context as _impl
    return _impl()

def get_ai_scoring_explainer() -> dict[str, Any]:
    """Kompatibilni facade pro vysvetleni AI scoringu."""

    from filmy.db_ai import get_ai_scoring_explainer as _impl
    return _impl()

def get_favorite_genres(active_only: bool=True) -> list[dict[str, Any]]:
    """Kompatibilni facade pro oblibene zanry."""

    from filmy.db_ai import get_favorite_genres as _impl
    return _impl(active_only=active_only)

def get_catalog_genres() -> list[dict[str, Any]]:
    """Kompatibilni facade pro seznam zanru z katalogu."""

    from filmy.db_ai import get_catalog_genres as _impl
    return _impl()

def get_favorite_traits(active_only: bool=True) -> list[dict[str, Any]]:
    """Kompatibilni facade pro oblibene traity."""

    from filmy.db_ai import get_favorite_traits as _impl
    return _impl(active_only=active_only)

def get_genre_score_source_rows() -> list[dict[str, Any]]:
    """Kompatibilni facade pro zdrojove radky genre scoringu."""

    from filmy.db_ai import get_genre_score_source_rows as _impl
    return _impl()

def get_home_suggestion_sections(*, limit_per_section: int | None=4) -> dict[str, Any]:
    """Kompatibilni facade pro homepage suggestion sekce."""

    from filmy.db_ai import get_home_suggestion_sections as _impl
    return _impl(limit_per_section=limit_per_section)

def get_genre_suggestion_candidates(genre: str, *, limit: int | None=24) -> dict[str, Any]:
    """Kompatibilni facade pro suggestion kandidaty jednoho zanru."""

    from filmy.db_ai import get_genre_suggestion_candidates as _impl
    return _impl(genre, limit=limit)

def replace_favorite_genres(genres: Sequence[str | dict[str, Any]], *, source_origin: str='local_app', source_ref: str | None=None, archive_missing: bool=True) -> dict[str, Any]:
    """Kompatibilni facade pro prepis oblibenych zanru."""

    from filmy.db_ai import replace_favorite_genres as _impl
    return _impl(genres, source_origin=source_origin, source_ref=source_ref, archive_missing=archive_missing)

def replace_favorite_traits(traits: Sequence[str | dict[str, Any]], *, source_origin: str='local_app', source_ref: str | None=None, archive_missing: bool=True) -> dict[str, Any]:
    """Kompatibilni facade pro prepis oblibenych traitu."""

    from filmy.db_ai import replace_favorite_traits as _impl
    return _impl(traits, source_origin=source_origin, source_ref=source_ref, archive_missing=archive_missing)

def record_genre_score_snapshot(scores: Sequence[dict[str, Any]], *, score_scope: str='default', algorithm_version: str | None=None, source_origin: str='local_app', source_ref: str | None=None, generated_at: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro ulozeni jednoho behu genre scoringu."""

    from filmy.db_ai import record_genre_score_snapshot as _impl
    return _impl(scores, score_scope=score_scope, algorithm_version=algorithm_version, source_origin=source_origin, source_ref=source_ref, generated_at=generated_at)

def compute_and_record_genre_scores(*, score_scope: str='default', algorithm_version: str | None=None, source_origin: str='local_app', source_ref: str | None=None, generated_at: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro vypocet a ulozeni genre scoringu."""

    from filmy.db_ai import compute_and_record_genre_scores as _impl
    return _impl(score_scope=score_scope, algorithm_version=algorithm_version, source_origin=source_origin, source_ref=source_ref, generated_at=generated_at)

def get_latest_genre_scores(*, score_scope: str | None=None, limit: int | None=None) -> dict[str, Any] | None:
    """Kompatibilni facade pro posledni ulozeny beh genre scoringu."""

    from filmy.db_ai import get_latest_genre_scores as _impl
    return _impl(score_scope=score_scope, limit=limit)

def _get_favorite_genres(conn, *, active_only: bool) -> list[dict[str, Any]]:
    """Nacte oblibene zanry primo z runtime tabulky."""

    rows = conn.execute('\n        SELECT\n            genre,\n            weight,\n            preference_rank,\n            source_origin,\n            source_ref,\n            notes,\n            is_active,\n            created_at,\n            updated_at\n        FROM app.favorite_genres\n        WHERE (? = FALSE OR is_active = TRUE)\n        ORDER BY preference_rank ASC NULLS LAST, weight DESC, genre ASC\n        ', [active_only]).fetchall()
    return [{'genre': row[0], 'weight': row[1], 'preference_rank': row[2], 'source_origin': row[3], 'source_ref': row[4], 'notes': row[5], 'is_active': row[6], 'created_at': row[7], 'updated_at': row[8]} for row in rows]

def _get_favorite_traits(conn, *, active_only: bool) -> list[dict[str, Any]]:
    """Nacte oblibene traity primo z runtime tabulky."""

    rows = conn.execute('\n        SELECT\n            trait,\n            weight,\n            preference_rank,\n            source_origin,\n            source_ref,\n            notes,\n            is_active,\n            created_at,\n            updated_at\n        FROM app.favorite_traits\n        WHERE (? = FALSE OR is_active = TRUE)\n        ORDER BY preference_rank ASC NULLS LAST, weight DESC, trait ASC\n        ', [active_only]).fetchall()
    return [{'trait': row[0], 'weight': row[1], 'preference_rank': row[2], 'source_origin': row[3], 'source_ref': row[4], 'notes': row[5], 'is_active': row[6], 'created_at': row[7], 'updated_at': row[8]} for row in rows]

def _get_catalog_genres(conn) -> list[dict[str, Any]]:
    """Rozbali zanry z katalogu a vrati jejich agregovane pocty."""

    rows = conn.execute("\n        WITH exploded AS (\n            SELECT\n                trim(unnest(string_split(genres, ','))) AS genre\n            FROM app.catalog_titles\n            WHERE genres IS NOT NULL AND genres <> ''\n        )\n        SELECT genre, COUNT(*) AS title_count\n        FROM exploded\n        WHERE genre IS NOT NULL AND genre <> ''\n        GROUP BY genre\n        ORDER BY genre ASC\n        ").fetchall()
    return [{'genre': row[0], 'title_count': row[1]} for row in rows]

def _get_genre_score_source_rows(conn, *, ratings_in_postgres: bool=False, watch_events_in_postgres: bool=False) -> list[dict[str, Any]]:
    """Sestavi zdrojove radky pro vypocet genre scoringu."""

    state_in_postgres = True
    ratings_cte = '\n        latest_title_ratings AS (\n            SELECT\n                tconst,\n                rating,\n                row_number() OVER (\n                    PARTITION BY tconst\n                    ORDER BY COALESCE(rated_at, updated_at, created_at) DESC, canonical_key\n                ) AS rn\n            FROM app.user_ratings\n            WHERE tconst IS NOT NULL\n        ),\n    ' if not ratings_in_postgres else '\n        latest_title_ratings AS (\n            SELECT NULL AS tconst, NULL AS rating, NULL AS rn\n            WHERE FALSE\n        ),\n    '
    watch_ctes = '\n        title_watch_events AS (\n            SELECT\n                w.tconst,\n                COALESCE(w.created_at, CAST(w.watched_on AS TIMESTAMP)) AS watched_at\n            FROM app.watch_events AS w\n            WHERE w.tconst IN (SELECT tconst FROM app.catalog_titles)\n\n            UNION ALL\n\n            SELECT\n                e.series_tconst AS tconst,\n                COALESCE(w.created_at, CAST(w.watched_on AS TIMESTAMP)) AS watched_at\n            FROM app.watch_events AS w\n            JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst\n            WHERE e.series_tconst IS NOT NULL\n        ),\n        title_watch_stats AS (\n            SELECT\n                tconst,\n                COUNT(*) AS watch_count,\n                MAX(watched_at) AS last_watched_at\n            FROM title_watch_events\n            GROUP BY tconst\n        ),\n    ' if not watch_events_in_postgres else '\n        title_watch_events AS (\n            SELECT NULL AS tconst, NULL AS watched_at\n            WHERE FALSE\n        ),\n        title_watch_stats AS (\n            SELECT NULL AS tconst, NULL AS watch_count, NULL AS last_watched_at\n            WHERE FALSE\n        ),\n    '
    rows = conn.execute(f"\n        WITH {ratings_cte}\n        {watch_ctes}\n        rated_cast_affinity AS (\n            SELECT\n                c.tconst,\n                SUM(CAST(p.affinity_rating AS DOUBLE) * CASE\n                    WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0\n                    ELSE 1.0 / sqrt(CAST(c.ordering AS DOUBLE))\n                END)\n                / NULLIF(\n                    SUM(CASE\n                        WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0\n                        ELSE 1.0 / sqrt(CAST(c.ordering AS DOUBLE))\n                    END),\n                    0.0\n                ) AS actor_affinity_rating\n            FROM app.title_credits AS c\n            JOIN app.user_people AS p ON p.nconst = c.nconst\n            WHERE c.credit_group = 'cast'\n              AND p.affinity_rating > 0\n              AND (c.ordering IS NULL OR c.ordering <= 8)\n            GROUP BY c.tconst\n        )\n        SELECT\n            t.tconst,\n            t.primary_title,\n            t.start_year,\n            t.genres,\n            r.rating,\n            w.watch_count,\n            w.last_watched_at,\n            a.actor_affinity_rating\n        FROM app.catalog_titles AS t\n        LEFT JOIN latest_title_ratings AS r ON r.tconst = t.tconst AND r.rn = 1\n        LEFT JOIN title_watch_stats AS w ON w.tconst = t.tconst\n        LEFT JOIN rated_cast_affinity AS a ON a.tconst = t.tconst\n        WHERE t.genres IS NOT NULL\n          AND t.genres <> ''\n          AND (r.rating IS NOT NULL OR w.watch_count IS NOT NULL OR a.actor_affinity_rating IS NOT NULL)\n        ORDER BY t.primary_title ASC\n        ").fetchall()
    items = [{'tconst': row[0], 'title': row[1], 'year': row[2], 'genres': [part.strip() for part in (row[3] or '').split(',') if part.strip()], 'rating': row[4], 'watch_count': row[5] or 0, 'last_watched_at': row[6], 'actor_affinity_rating': row[7]} for row in rows]
    if ratings_in_postgres:
        ratings_by_tconst = fetch_latest_ratings_for_tconsts([str(item['tconst']) for item in items])
        for item in items:
            latest_rating = ratings_by_tconst.get(str(item['tconst']))
            if latest_rating is not None:
                item['rating'] = latest_rating['rating']
    if watch_events_in_postgres:
        events = fetch_all_watch_events()
        raw_stats: dict[str, dict[str, Any]] = {}
        raw_tconsts = sorted({str(event['tconst']) for event in events if event.get('tconst')})
        series_by_episode: dict[str, str] = {}
        if raw_tconsts:
            episode_map_rows = conn.execute(f"\n                SELECT episode_tconst, series_tconst\n                FROM app.catalog_episodes\n                WHERE episode_tconst IN ({', '.join(('?' for _ in raw_tconsts))})\n                ", raw_tconsts).fetchall()
            series_by_episode = {str(row[0]): str(row[1]) for row in episode_map_rows if row[1] is not None}
        for event in events:
            event_tconst = str(event['tconst'])
            current = raw_stats.setdefault(event_tconst, {'watch_count': 0, 'last_watched_at': None})
            current['watch_count'] += 1
            if current['last_watched_at'] is None or (event.get('created_at') is not None and event['created_at'] > current['last_watched_at']):
                current['last_watched_at'] = event.get('created_at')
            series_tconst = series_by_episode.get(event_tconst)
            if series_tconst:
                series_current = raw_stats.setdefault(series_tconst, {'watch_count': 0, 'last_watched_at': None})
                series_current['watch_count'] += 1
                if series_current['last_watched_at'] is None or (event.get('created_at') is not None and event['created_at'] > series_current['last_watched_at']):
                    series_current['last_watched_at'] = event.get('created_at')
        for item in items:
            stats = raw_stats.get(str(item['tconst']))
            if stats is not None:
                item['watch_count'] = stats['watch_count']
                item['last_watched_at'] = stats['last_watched_at']
    if state_in_postgres:
        for item in items:
            item['actor_affinity_rating'] = None
        affinity_by_tconst = _compute_actor_affinity_scores(conn, [str(item['tconst']) for item in items])
        for item in items:
            if str(item['tconst']) in affinity_by_tconst:
                item['actor_affinity_rating'] = affinity_by_tconst[str(item['tconst'])]
    return items

def _get_home_suggestion_candidate_rows(conn) -> list[dict[str, Any]]:
    """Return a compact unwatched candidate pool for homepage suggestions.

    Pool je zamerne omezeny na tituly, ktere maji aspon TMDB detail nebo jsou
    relativne nove. Tj. nechceme pro homepage prochazet cely katalog. Prioritou
    je rychly shortlist, nad kterym se pak uz jen dopocte trait/new scoring.
    """
    ui_config = get_ui_config()
    current_year = datetime.now(UTC).year
    watch_events_in_postgres = True
    state_in_postgres = True
    primary_locale, fallback_locale = ui_config.tmdb_locale_order
    if state_in_postgres and watch_events_in_postgres:
        rows = fetch_home_suggestion_candidate_rows_postgres(min_start_year=current_year - 2, primary_locale=primary_locale, fallback_locale=fallback_locale)
        return [{'tconst': row[0], 'title_type': row[1], 'primary_title': row[2], 'start_year': row[3], 'genres': [part.strip() for part in str(row[4] or '').split(',') if part.strip()], 'average_rating': row[5], 'num_votes': row[6], 'overview': row[7], 'release_date': row[8], 'cz_provider_count': row[9], 'watch_count': row[10], 'actor_affinity_rating': row[11]} for row in rows]
    watch_ctes = '\n        title_watch_events AS (\n            SELECT\n                w.tconst,\n                COALESCE(w.created_at, CAST(w.watched_on AS TIMESTAMP)) AS watched_at\n            FROM app.watch_events AS w\n            WHERE w.tconst IN (SELECT tconst FROM app.catalog_titles)\n\n            UNION ALL\n\n            SELECT\n                e.series_tconst AS tconst,\n                COALESCE(w.created_at, CAST(w.watched_on AS TIMESTAMP)) AS watched_at\n            FROM app.watch_events AS w\n            JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst\n            WHERE e.series_tconst IS NOT NULL\n        ),\n        title_watch_stats AS (\n            SELECT\n                tconst,\n                COUNT(*) AS watch_count\n            FROM title_watch_events\n            GROUP BY tconst\n        ),\n    ' if not watch_events_in_postgres else '\n        title_watch_events AS (\n            SELECT NULL AS tconst, NULL AS watched_at\n            WHERE FALSE\n        ),\n        title_watch_stats AS (\n            SELECT NULL AS tconst, NULL AS watch_count\n            WHERE FALSE\n        ),\n    '
    watched_filter = 'COALESCE(w.watch_count, 0) = 0' if not watch_events_in_postgres else 'TRUE'
    rows = conn.execute(f"\n        WITH latest_tmdb_details AS (\n            SELECT\n                tconst,\n                overview,\n                release_date,\n                row_number() OVER (\n                    PARTITION BY tconst\n                    ORDER BY\n                        CASE locale\n                            WHEN ? THEN 0\n                            WHEN ? THEN 1\n                            ELSE 2\n                        END,\n                        synced_at DESC\n                ) AS rn\n            FROM app.tmdb_title_details\n        ),\n        cz_provider_stats AS (\n            SELECT\n                tconst,\n                COUNT(*) AS cz_provider_count\n            FROM app.tmdb_watch_providers\n            WHERE country_code = 'CZ'\n            GROUP BY tconst\n        ),\n        {watch_ctes}\n        rated_cast_affinity AS (\n            SELECT\n                c.tconst,\n                SUM(CAST(p.affinity_rating AS DOUBLE) * CASE\n                    WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0\n                    ELSE 1.0 / sqrt(CAST(c.ordering AS DOUBLE))\n                END)\n                / NULLIF(\n                    SUM(CASE\n                        WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0\n                        ELSE 1.0 / sqrt(CAST(c.ordering AS DOUBLE))\n                    END),\n                    0.0\n                ) AS actor_affinity_rating\n            FROM app.title_credits AS c\n            JOIN app.user_people AS p ON p.nconst = c.nconst\n            WHERE c.credit_group = 'cast'\n              AND p.affinity_rating > 0\n              AND (c.ordering IS NULL OR c.ordering <= 8)\n            GROUP BY c.tconst\n        )\n        SELECT\n            t.tconst,\n            t.title_type,\n            t.primary_title,\n            t.start_year,\n            t.genres,\n            t.average_rating,\n            t.num_votes,\n            d.overview,\n            d.release_date,\n            COALESCE(p.cz_provider_count, 0) AS cz_provider_count,\n            COALESCE(w.watch_count, 0) AS watch_count,\n            a.actor_affinity_rating\n        FROM app.catalog_titles AS t\n        LEFT JOIN latest_tmdb_details AS d ON d.tconst = t.tconst AND d.rn = 1\n        LEFT JOIN cz_provider_stats AS p ON p.tconst = t.tconst\n        LEFT JOIN title_watch_stats AS w ON w.tconst = t.tconst\n        LEFT JOIN rated_cast_affinity AS a ON a.tconst = t.tconst\n        WHERE {watched_filter}\n          AND (\n                COALESCE(length(trim(d.overview)), 0) > 0\n                OR COALESCE(TRY_CAST(d.release_date AS DATE) >= current_date - INTERVAL 540 DAY, FALSE)\n                OR COALESCE(t.start_year, 0) >= ?\n              )\n        ORDER BY\n            COALESCE(t.start_year, 0) DESC,\n            COALESCE(t.num_votes, 0) DESC,\n            COALESCE(t.average_rating, 0.0) DESC,\n            t.primary_title\n        LIMIT 3000\n        ", [primary_locale, fallback_locale, current_year - 2]).fetchall()
    items = [{'tconst': row[0], 'title_type': row[1], 'primary_title': row[2], 'start_year': row[3], 'genres': [part.strip() for part in str(row[4] or '').split(',') if part.strip()], 'average_rating': row[5], 'num_votes': row[6], 'overview': row[7], 'release_date': row[8], 'cz_provider_count': row[9], 'watch_count': row[10], 'actor_affinity_rating': row[11]} for row in rows]
    if state_in_postgres:
        for item in items:
            item['actor_affinity_rating'] = None
        affinity_by_tconst = _compute_actor_affinity_scores(conn, [str(item['tconst']) for item in items])
        for item in items:
            if str(item['tconst']) in affinity_by_tconst:
                item['actor_affinity_rating'] = affinity_by_tconst[str(item['tconst'])]
    if watch_events_in_postgres:
        watched_tconsts = {str(item['tconst']) for item in _get_runtime_postgres_candidate_items(conn) if 'watched_title' in (item.get('reasons') or []) or 'watched_series' in (item.get('reasons') or [])}
        items = [item for item in items if str(item['tconst']) not in watched_tconsts]
    return items

def _ensure_genre_scores_schema_columns(conn) -> None:
    """Dovybavi snapshot tabulku o nove volitelne score sloupce.

    Tyhle vypocty se pousti i jednorazovymi skripty mimo FastAPI startup, proto
    nesmime spolehat jen na globalni `ensure_database()`. Zde se drzi jen lehke
    `ALTER TABLE ... IF NOT EXISTS` pro nullable sloupce.
    """
    conn.execute('ALTER TABLE app.genre_scores ADD COLUMN IF NOT EXISTS actor_affinity_score DOUBLE')

def _record_genre_score_snapshot(conn, scores: Sequence[dict[str, Any]], *, score_scope: str, algorithm_version: str | None, source_origin: str, source_ref: str | None, generated_at: str) -> dict[str, Any]:
    """Ulozi jeden beh genre scoringu do snapshot tabulky."""

    if not scores:
        raise ValueError('Je potreba dodat alespon jeden zanr se score.')
    datetime.fromisoformat(generated_at.replace('Z', '+00:00'))
    _ensure_genre_scores_schema_columns(conn)
    prepared_rows: list[dict[str, Any]] = []
    for index, item in enumerate(scores, start=1):
        genre = str(item.get('genre') or '').strip()
        if not genre:
            raise ValueError('Kazdy zaznam genre_scores musi mit genre.')
        if item.get('final_score') is None:
            raise ValueError(f"Zaznam pro zanr '{genre}' nema final_score.")
        prepared_rows.append({'id': str(uuid.uuid4()), 'genre': genre, 'titles_considered': item.get('titles_considered'), 'watched_titles_considered': item.get('watched_titles_considered'), 'rated_titles_considered': item.get('rated_titles_considered'), 'contributing_titles_json': _dumps_json_or_none(item.get('contributing_titles')), 'excluded_titles_json': _dumps_json_or_none(item.get('excluded_titles')), 'favorite_genre_weight': item.get('favorite_genre_weight'), 'preference_overlap_score': item.get('preference_overlap_score'), 'preference_alignment_score': item.get('preference_alignment_score'), 'affinity_score': item.get('affinity_score'), 'rating_signal_score': item.get('rating_signal_score'), 'watch_signal_score': item.get('watch_signal_score'), 'recency_score': item.get('recency_score'), 'actor_affinity_score': item.get('actor_affinity_score'), 'frequency_score': item.get('frequency_score'), 'consistency_score': item.get('consistency_score'), 'novelty_score': item.get('novelty_score'), 'confidence_score': item.get('confidence_score'), 'manual_adjustment_score': item.get('manual_adjustment_score'), 'final_score': item.get('final_score'), 'normalized_score': item.get('normalized_score'), 'rank_in_run': item.get('rank_in_run', index), 'metrics_json': _dumps_json_or_none(item.get('metrics')), 'explanation': item.get('explanation')})
    conn.executemany('\n        INSERT INTO app.genre_scores (\n            id, genre, generated_at, algorithm_version, score_scope, source_origin, source_ref,\n            titles_considered, watched_titles_considered, rated_titles_considered,\n            contributing_titles_json, excluded_titles_json,\n            favorite_genre_weight, preference_overlap_score, preference_alignment_score, affinity_score,\n            rating_signal_score, watch_signal_score, recency_score, actor_affinity_score, frequency_score, consistency_score,\n            novelty_score, confidence_score, manual_adjustment_score, final_score, normalized_score,\n            rank_in_run, metrics_json, explanation, created_at\n        )\n        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n        ', [[item['id'], item['genre'], generated_at, algorithm_version, score_scope, source_origin, source_ref, item['titles_considered'], item['watched_titles_considered'], item['rated_titles_considered'], item['contributing_titles_json'], item['excluded_titles_json'], item['favorite_genre_weight'], item['preference_overlap_score'], item['preference_alignment_score'], item['affinity_score'], item['rating_signal_score'], item['watch_signal_score'], item['recency_score'], item['actor_affinity_score'], item['frequency_score'], item['consistency_score'], item['novelty_score'], item['confidence_score'], item['manual_adjustment_score'], item['final_score'], item['normalized_score'], item['rank_in_run'], item['metrics_json'], item['explanation'], generated_at] for item in prepared_rows])
    return {'generated_at': generated_at, 'score_scope': score_scope, 'algorithm_version': algorithm_version, 'count': len(prepared_rows)}

def _get_latest_genre_scores(conn, *, score_scope: str | None, limit: int | None) -> dict[str, Any] | None:
    """Vrati posledni snapshot genre scoringu z runtime tabulky."""

    latest_row = conn.execute('\n        SELECT generated_at, score_scope\n        FROM app.genre_scores\n        WHERE (? IS NULL OR score_scope = ?)\n        ORDER BY generated_at DESC, score_scope ASC\n        LIMIT 1\n        ', [score_scope, score_scope]).fetchone()
    if latest_row is None:
        return None
    generated_at = latest_row[0]
    resolved_scope = latest_row[1]
    sql = '\n        SELECT\n            id,\n            genre,\n            generated_at,\n            algorithm_version,\n            score_scope,\n            source_origin,\n            source_ref,\n            titles_considered,\n            watched_titles_considered,\n            rated_titles_considered,\n            contributing_titles_json,\n            excluded_titles_json,\n            favorite_genre_weight,\n            preference_overlap_score,\n            preference_alignment_score,\n            affinity_score,\n            rating_signal_score,\n            watch_signal_score,\n            recency_score,\n            actor_affinity_score,\n            frequency_score,\n            consistency_score,\n            novelty_score,\n            confidence_score,\n            manual_adjustment_score,\n            final_score,\n            normalized_score,\n            rank_in_run,\n            metrics_json,\n            explanation,\n            created_at\n        FROM app.genre_scores\n        WHERE generated_at = ? AND score_scope = ?\n        ORDER BY rank_in_run ASC NULLS LAST, final_score DESC, genre ASC\n    '
    params: list[Any] = [generated_at, resolved_scope]
    if limit is not None:
        sql += ' LIMIT ?'
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    items = [{'id': row[0], 'genre': row[1], 'generated_at': row[2], 'algorithm_version': row[3], 'score_scope': row[4], 'source_origin': row[5], 'source_ref': row[6], 'titles_considered': row[7], 'watched_titles_considered': row[8], 'rated_titles_considered': row[9], 'contributing_titles': _loads_json_or_none(row[10]), 'excluded_titles': _loads_json_or_none(row[11]), 'favorite_genre_weight': row[12], 'preference_overlap_score': row[13], 'preference_alignment_score': row[14], 'affinity_score': row[15], 'rating_signal_score': row[16], 'watch_signal_score': row[17], 'recency_score': row[18], 'actor_affinity_score': row[19], 'frequency_score': row[20], 'consistency_score': row[21], 'novelty_score': row[22], 'confidence_score': row[23], 'manual_adjustment_score': row[24], 'final_score': row[25], 'normalized_score': row[26], 'rank_in_run': row[27], 'metrics': _loads_json_or_none(row[28]), 'explanation': row[29], 'created_at': row[30]} for row in rows]
    return {'generated_at': generated_at, 'score_scope': resolved_scope, 'count': len(items), 'items': items}

def record_watch_event(tconst: str, *, watched_on: str | None=None, notes: str | None=None, add_to_watched_list: bool=False, archive_from_list_id: str | None=None, archive_display_tconst: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro zapis jednoho watch eventu."""

    from filmy.db_library import record_watch_event as _impl
    return _impl(tconst, watched_on=watched_on, notes=notes, add_to_watched_list=add_to_watched_list, archive_from_list_id=archive_from_list_id, archive_display_tconst=archive_display_tconst)

def record_watch_events_through_episode(episode_tconst: str, *, watched_on: str | None=None, notes: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro serialovy watched-through zapis."""

    from filmy.db_library import record_watch_events_through_episode as _impl
    return _impl(episode_tconst, watched_on=watched_on, notes=notes)

def delete_group_from_user_list(list_id: str, display_tconst: str) -> dict[str, Any]:
    """Kompatibilni facade pro smazani zobrazene skupiny ze seznamu."""

    from filmy.db_library import delete_group_from_user_list as _impl
    return _impl(list_id, display_tconst)

def move_group_between_user_lists(source_list_id: str, target_list_id: str, display_tconst: str) -> dict[str, Any]:
    """Kompatibilni facade pro presun skupiny mezi seznamy."""

    from filmy.db_library import move_group_between_user_lists as _impl
    return _impl(source_list_id, target_list_id, display_tconst)

def copy_group_to_user_list(source_list_id: str, target_list_id: str, display_tconst: str) -> dict[str, Any]:
    """Kompatibilni facade pro kopii skupiny mezi seznamy."""

    from filmy.db_library import copy_group_to_user_list as _impl
    return _impl(source_list_id, target_list_id, display_tconst)

def create_user_list(name: str, description: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro zalozeni uzivatelskeho seznamu."""

    from filmy.db_library import create_user_list as _impl
    return _impl(name, description)

def update_user_list_description(list_id: str, description: str | None=None, ai_input_role: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro upravu popisu a AI role seznamu."""

    from filmy.db_library import update_user_list_description as _impl
    return _impl(list_id, description, ai_input_role=ai_input_role)

def delete_user_list(list_id: str) -> dict[str, Any]:
    """Kompatibilni facade pro smazani uzivatelskeho seznamu."""

    from filmy.db_library import delete_user_list as _impl
    return _impl(list_id)

def clear_ai_suggestions_list_items() -> dict[str, Any]:
    """Kompatibilni facade pro vycisteni AI suggestions seznamu."""

    from filmy.db_library import clear_ai_suggestions_list_items as _impl
    return _impl()

def set_title_role_signal(tconst: str, *, nconst: str | None=None, character_name: str | None=None, signal_type: str='character', polarity: str='positive', strength: int=8, notes: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro zapis role signalu u titulu."""

    from filmy.db_library import set_title_role_signal as _impl
    return _impl(tconst, nconst=nconst, character_name=character_name, signal_type=signal_type, polarity=polarity, strength=strength, notes=notes)

def replace_title_role_signals(tconst: str, *, nconst: str | None=None, character_name: str | None=None, signal_types: list[str] | tuple[str, ...] | None=None, polarity: str='positive', strength: int=8, notes: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro prepis role signalu u titulu."""

    from filmy.db_library import replace_title_role_signals as _impl
    return _impl(tconst, nconst=nconst, character_name=character_name, signal_types=signal_types, polarity=polarity, strength=strength, notes=notes)

def delete_title_role_signals(tconst: str, *, nconst: str | None=None, character_name: str | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro smazani role signalu u titulu."""

    from filmy.db_library import delete_title_role_signals as _impl
    return _impl(tconst, nconst=nconst, character_name=character_name)

def get_title_role_signals(tconst: str) -> list[dict[str, Any]]:
    """Kompatibilni facade pro cteni role signalu titulu."""

    from filmy.db_library import get_title_role_signals as _impl
    return _impl(tconst)

def _pick_best_title_match(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Vybere nejlepsiho title kandidata z lookup vrstvy."""

    from filmy.db_lookup import _pick_best_title_match as _impl
    return _impl(query, candidates)

def _build_title_lookup_result(*, query: str, title_type: str | None, selected: dict[str, Any], candidates: list[dict[str, Any]], candidates_limit: int) -> dict[str, Any]:
    """Slozi vysledny payload title lookupu."""

    from filmy.db_lookup import _build_title_lookup_result as _impl
    return _impl(query=query, title_type=title_type, selected=selected, candidates=candidates, candidates_limit=candidates_limit)

def _lookup_person_from_search_recall(query: str, *, candidates_limit: int) -> dict[str, Any] | None:
    """Zkusi vratit person lookup primo z recall vrstvy."""

    from filmy.db_lookup import _lookup_person_from_search_recall as _impl
    return _impl(query, candidates_limit=candidates_limit)

def _remember_person_lookup(query: str, selected: dict[str, Any]) -> None:
    """Ulozi uspesny person lookup do recall vrstvy."""

    from filmy.db_lookup import _remember_person_lookup as _impl
    return _impl(query, selected)

def _pick_best_person_match(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Vybere nejlepsiho person kandidata z lookup vrstvy."""

    from filmy.db_lookup import _pick_best_person_match as _impl
    return _impl(query, candidates)

def _build_lookup_candidate(candidate: dict[str, Any], *, query: str, is_selected: bool) -> dict[str, Any]:
    """Slozi serializovany title lookup kandidat pro odpoved API/UI."""

    from filmy.db_lookup import _build_lookup_candidate as _impl
    return _impl(candidate, query=query, is_selected=is_selected)

def _build_person_lookup_candidate(candidate: dict[str, Any], *, query: str, is_selected: bool) -> dict[str, Any]:
    """Slozi serializovany person lookup kandidat pro odpoved API/UI."""

    from filmy.db_lookup import _build_person_lookup_candidate as _impl
    return _impl(candidate, query=query, is_selected=is_selected)

def _is_confident_person_lookup(query: str, candidate: dict[str, Any]) -> bool:
    """Vrati, jestli je person kandidat dostatecne jednoznacny."""

    from filmy.db_lookup import _is_confident_person_lookup as _impl
    return _impl(query, candidate)

def _should_expand_people_to_fuzzy(query: str, candidates: list[dict[str, Any]]) -> bool:
    """Rozhodne, jestli ma person lookup prejit do fuzzy vetve."""

    from filmy.db_lookup import _should_expand_people_to_fuzzy as _impl
    return _impl(query, candidates)

def _person_lookup_item_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Prevede SQL radek na interny person lookup slovnik."""

    from filmy.db_lookup import _person_lookup_item_from_row as _impl
    return _impl(row)

def _search_people_for_lookup(query: str, limit: int) -> list[dict[str, Any]]:
    """Spusti primarni person lookup dotaz."""

    from filmy.db_lookup import _search_people_for_lookup as _impl
    return _impl(query, limit)

def _search_people_for_lookup_fuzzy(query: str, limit: int) -> list[dict[str, Any]]:
    """Spusti fuzzy person lookup dotaz."""

    from filmy.db_lookup import _search_people_for_lookup_fuzzy as _impl
    return _impl(query, limit)

def _search_people_for_lookup_levenshtein(query: str, limit: int) -> list[dict[str, Any]]:
    """Spusti levenshtein fallback pro person lookup."""

    from filmy.db_lookup import _search_people_for_lookup_levenshtein as _impl
    return _impl(query, limit)

def _fetch_person_filmography_summary(conn, nconst: str) -> dict[str, Any]:
    """Vrati zkraceny filmograficky souhrn osoby pro lookup/detail helpery."""

    rows = conn.execute('\n        SELECT\n            c.credit_group,\n            t.primary_title,\n            t.start_year,\n            t.title_type\n        FROM app.title_credits AS c\n        JOIN app.catalog_titles AS t ON t.tconst = c.tconst\n        WHERE c.nconst = ?\n        ORDER BY t.start_year DESC NULLS LAST, t.primary_title\n        LIMIT 50\n        ', [nconst]).fetchall()
    grouped = {'director': [], 'creator': [], 'writer': [], 'cast': [], 'principal': []}
    for row in rows:
        grouped.setdefault(row[0], []).append({'title': row[1], 'start_year': row[2], 'title_type': row[3]})
    return {'credit_count': len(rows), 'director': grouped['director'][:20], 'creator': grouped['creator'][:20], 'writer': grouped['writer'][:20], 'cast': grouped['cast'][:20], 'principal': grouped['principal'][:20]}

def _is_confident_lookup(query: str, candidate: dict[str, Any]) -> bool:
    """Vrati, jestli je title kandidat dostatecne jednoznacny."""

    from filmy.db_lookup import _is_confident_lookup as _impl
    return _impl(query, candidate)

def _is_direct_enough_lookup(query: str, candidate: dict[str, Any]) -> bool:
    """Vrati, jestli je title kandidat dost primy bez dalsiho rozsirovani."""

    from filmy.db_lookup import _is_direct_enough_lookup as _impl
    return _impl(query, candidate)

def _is_exact_title_match(query: str, candidate: dict[str, Any]) -> bool:
    """Vrati, jestli kandidat odpovida presnemu nazvu dotazu."""

    from filmy.db_lookup import _is_exact_title_match as _impl
    return _impl(query, candidate)

def _lookup_local_signal_score(candidate: dict[str, Any]) -> int:
    """Spocita pomocne lokalni score title kandidata."""

    from filmy.db_lookup import _lookup_local_signal_score as _impl
    return _impl(candidate)

def _attach_library_summaries_to_exact_title_candidates(query: str, candidates: list[dict[str, Any]]) -> None:
    """Doplni local library summary k presnym title kandidatům."""

    from filmy.db_lookup import _attach_library_summaries_to_exact_title_candidates as _impl
    return _impl(query, candidates)

def _is_safe_recalled_title(query: str, *, title_type: str | None, recalled: dict[str, Any], candidates_limit: int) -> bool:
    """Vrati, jestli je recalled title bezpecne pouzit bez plneho search fallbacku."""

    from filmy.db_lookup import _is_safe_recalled_title as _impl
    return _impl(query, title_type=title_type, recalled=recalled, candidates_limit=candidates_limit)

def _should_expand_to_fuzzy(query: str, candidates: list[dict[str, Any]]) -> bool:
    """Rozhodne, jestli ma title lookup prejit do fuzzy vetve."""

    from filmy.db_lookup import _should_expand_to_fuzzy as _impl
    return _impl(query, candidates)

def _merge_lookup_candidates(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Slouci dve sady title kandidatu bez duplicit."""

    from filmy.db_lookup import _merge_lookup_candidates as _impl
    return _impl(primary, secondary)

def _lookup_identity_key(item: dict[str, Any]) -> str:
    """Vrati stabilni identitni klic lookup kandidata."""

    from filmy.db_lookup import _lookup_identity_key as _impl
    return _impl(item)

def _alias_priority_case_sql(region_column: str, language_column: str) -> str:
    """Vrati SQL `CASE` pro razeni aliasu podle regionu a jazyka."""

    from filmy.db_lookup import _alias_priority_case_sql as _impl
    return _impl(region_column, language_column)

def _catalog_row_from_alias_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Prevede aliasovy SQL radek na katalogovy title slovnik."""

    from filmy.db_lookup import _catalog_row_from_alias_row as _impl
    return _impl(row)

def _search_catalog_aliases_for_lookup(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Spusti primarni aliasovy lookup v katalogu."""

    from filmy.db_lookup import _search_catalog_aliases_for_lookup as _impl
    return _impl(query, title_type, limit)

def _search_catalog_aliases_for_lookup_fuzzy(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Spusti fuzzy aliasovy lookup v katalogu."""

    from filmy.db_lookup import _search_catalog_aliases_for_lookup_fuzzy as _impl
    return _impl(query, title_type, limit)

def _search_catalog_aliases_for_lookup_levenshtein(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Spusti levenshtein fallback nad aliasy katalogu."""

    from filmy.db_lookup import _search_catalog_aliases_for_lookup_levenshtein as _impl
    return _impl(query, title_type, limit)

def _search_catalog_for_lookup_fuzzy(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Spusti fuzzy lookup nad primarnimi katalogovymi nazvy."""

    from filmy.db_lookup import _search_catalog_for_lookup_fuzzy as _impl
    return _impl(query, title_type, limit)

def _search_catalog_for_lookup_levenshtein(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Spusti levenshtein fallback nad primarnimi katalogovymi nazvy."""

    from filmy.db_lookup import _search_catalog_for_lookup_levenshtein as _impl
    return _impl(query, title_type, limit)

def _best_title_similarity(query_key: str, variants: list[Any]) -> float:
    """Vrati nejlepsi similarity score mezi dotazem a variantami nazvu."""

    from filmy.db_lookup import _best_title_similarity as _impl
    return _impl(query_key, variants)

def _best_person_name_similarity(query_key: str, primary_name: Any) -> float:
    """Vrati similarity score mezi dotazem a primarnim jmenem osoby."""

    from filmy.db_lookup import _best_person_name_similarity as _impl
    return _impl(query_key, primary_name)

def _token_similarity_score(query_key: str, variant_key: str) -> float:
    """Spocita token-based similarity score dvou normalizovanych retezcu."""

    from filmy.db_lookup import _token_similarity_score as _impl
    return _impl(query_key, variant_key)

def _match_tokens(value: str) -> list[str]:
    """Rozdeli normalizovany text na tokeny pro lookup heuristiky."""

    from filmy.db_lookup import _match_tokens as _impl
    return _impl(value)

def _tokens_are_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """Vrati, jestli je `needle` subsekvence tokenu v `haystack`."""

    from filmy.db_lookup import _tokens_are_subsequence as _impl
    return _impl(needle, haystack)

def _search_catalog_for_lookup(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Spusti primarni lookup nad katalogovymi primarnimi nazvy."""

    from filmy.db_lookup import _search_catalog_for_lookup as _impl
    return _impl(query, title_type, limit)

def _fetch_title_people(conn, tconst: str) -> dict[str, list[dict[str, Any]]]:
    """Vrati zakladni blok lidi pro title detail."""

    credit_rows = fetch_title_people_rows(tconst)
    directors: list[dict[str, Any]] = []
    writers: list[dict[str, Any]] = []
    creators: list[dict[str, Any]] = []
    cast: list[dict[str, Any]] = []
    seen_groups: set[tuple[str, str]] = set()
    for row in credit_rows:
        person = {'nconst': row[0], 'name': row[6]}
        group_key = (row[1], row[0])
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        if row[1] == 'director':
            directors.append(person)
        elif row[1] == 'writer':
            writers.append(person)
        elif row[1] == 'creator':
            creators.append(person)
        elif row[1] == 'cast' and len(cast) < 8:
            cast.append({**person, 'character': _principal_character(row[4]), 'category': row[2]})
    return {'directors': directors, 'writers': writers, 'creators': creators, 'cast': cast}

def _principal_character(value: str | None) -> str | None:
    """Vytahne prvni character label z JSON serializovaneho seznamu."""

    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return str(value)
    if isinstance(parsed, list) and parsed:
        return str(parsed[0])
    return None

def _title_type_label(title_type: str | None) -> str:
    """Prevede interny `title_type` na kratky uzivatelsky label."""

    labels = {'movie': 'Movie', 'tvMovie': 'TV Movie', 'tvSeries': 'TV Series', 'tvMiniSeries': 'TV Mini Series'}
    return labels.get(title_type or '', 'Title')

def upsert_tmdb_mapping(tconst: str, tmdb_media_type: str, tmdb_id: int, matched_by: str, sync_status: str, last_error: str | None=None) -> None:
    """Kompatibilni facade pro ulozeni TMDB mapovani titulu."""

    from filmy.db_tmdb import upsert_tmdb_mapping as _impl
    return _impl(tconst, tmdb_media_type, tmdb_id, matched_by, sync_status, last_error=last_error)

def store_tmdb_payloads(tconst: str, locale: str, detail_payload: dict[str, Any], provider_payload: dict[str, Any] | None) -> None:
    """Kompatibilni facade pro ulozeni TMDB payloadu."""

    from filmy.db_tmdb import store_tmdb_payloads as _impl
    return _impl(tconst, locale, detail_payload, provider_payload)

def get_tmdb_mapping(tconst: str) -> dict[str, Any] | None:
    """Kompatibilni facade pro nacteni TMDB mapovani."""

    from filmy.db_tmdb import get_tmdb_mapping as _impl
    return _impl(tconst)

def record_tmdb_asset(tconst: str, asset_kind: str, relative_path: str, local_path: str, fetch_reason: str, status: str, sha256: str | None) -> dict[str, Any]:
    """Kompatibilni facade pro zapsani TMDB asset zaznamu."""

    from filmy.db_tmdb import record_tmdb_asset as _impl
    return _impl(tconst, asset_kind, relative_path, local_path, fetch_reason, status, sha256)

def get_latest_tmdb_assets(tconst: str) -> list[dict[str, Any]]:
    """Kompatibilni facade pro posledni TMDB assety titulu."""

    from filmy.db_tmdb import get_latest_tmdb_assets as _impl
    return _impl(tconst)

def get_tmdb_detail_locales(tconst: str) -> list[str]:
    """Kompatibilni facade pro seznam lokalizaci TMDB detailu."""

    from filmy.db_tmdb import get_tmdb_detail_locales as _impl
    return _impl(tconst)

def get_tmdb_asset_summary(tconst: str) -> dict[str, dict[str, Any]]:
    """Kompatibilni facade pro agregovany souhrn TMDB assetu."""

    from filmy.db_tmdb import get_tmdb_asset_summary as _impl
    return _impl(tconst)

def get_latest_poster_records(tconsts: list[str]) -> dict[str, dict[str, Any]]:
    """Kompatibilni facade pro posledni poster zaznamy vice titulu."""

    from filmy.db_tmdb import get_latest_poster_records as _impl
    return _impl(tconsts)

def get_tmdb_enrichment_targets(limit: int | None=None, include_complete: bool=True, priority_tconsts: list[str] | None=None) -> list[dict[str, Any]]:
    """Kompatibilni facade pro vyber TMDB enrichment kandidatu."""

    from filmy.db_tmdb import get_tmdb_enrichment_targets as _impl
    return _impl(limit=limit, include_complete=include_complete, priority_tconsts=priority_tconsts)

def get_tmdb_target_counts() -> tuple[int, int]:
    """Kompatibilni facade pro pocty TMDB kandidatu."""

    from filmy.db_tmdb import get_tmdb_target_counts as _impl
    return _impl()

def _tmdb_status_is_complete(tconst: str) -> bool:
    """Vrati, jestli ma titul kompletni TMDB enrichment."""

    from filmy.db_tmdb import _tmdb_status_is_complete as _impl
    return _impl(tconst)

def _tmdb_flags_indicate_complete(flags: dict[str, Any] | None, *, primary_locale: str, fallback_locale: str) -> bool:
    """Vyhodnoti completion flagy TMDB payloadu pro dane locale poradi."""

    from filmy.db_tmdb import _tmdb_flags_indicate_complete as _impl
    return _impl(flags, primary_locale=primary_locale, fallback_locale=fallback_locale)

def _get_tmdb_postgres_runtime_items(conn, *, include_complete: bool) -> list[dict[str, Any]]:
    """Vrati runtime kandidaty pro TMDB enrichment z PostgreSQL vrstvy."""

    from filmy.db_tmdb import _get_tmdb_postgres_runtime_items as _impl
    return _impl(conn, include_complete=include_complete)

def _get_priority_tmdb_target_items(conn, priority_tconsts: Sequence[str]) -> list[dict[str, Any]]:
    """Vrati prioritni TMDB kandidaty pro explicitne zadane tituly."""

    from filmy.db_tmdb import _get_priority_tmdb_target_items as _impl
    return _impl(conn, priority_tconsts)

def _merge_tmdb_target_items(primary: Sequence[dict[str, Any]], secondary: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Slouci dve sady TMDB kandidatu bez duplicit."""

    from filmy.db_tmdb import _merge_tmdb_target_items as _impl
    return _impl(primary, secondary)

def _merge_runtime_candidate_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Slouci raw runtime kandidatni radky do stabilniho seznamu."""

    from filmy.db_tmdb import _merge_runtime_candidate_rows as _impl
    return _impl(rows)

def _catalog_title_rows_by_tconsts(conn, tconsts: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Vrati katalogove radky indexovane podle `tconst`."""

    from filmy.db_tmdb import _catalog_title_rows_by_tconsts as _impl
    return _impl(conn, tconsts)

def _episode_series_map(conn, tconsts: Sequence[str]) -> dict[str, str]:
    """Vrati mapu epizoda -> serial pro zadane `tconst`."""

    from filmy.db_tmdb import _episode_series_map as _impl
    return _impl(conn, tconsts)

def _compute_actor_affinity_scores(conn, tconsts: Sequence[str]) -> dict[str, float]:
    """Spocita actor affinity score pro zadane tituly."""

    from filmy.db_tmdb import _compute_actor_affinity_scores as _impl
    return _impl(conn, tconsts)

def _get_runtime_postgres_candidate_items(conn) -> list[dict[str, Any]]:
    """Vrati runtime kandidaty, ktere mohou spustit dalsi TMDB enrichment."""

    from filmy.db_tmdb import _get_runtime_postgres_candidate_items as _impl
    return _impl(conn)

def _tmdb_target_sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
    """Vrati stabilni sort key pro razeni TMDB kandidatu."""

    from filmy.db_tmdb import _tmdb_target_sort_key as _impl
    return _impl(item)

def get_title_detail_cache_targets(limit: int | None=None, include_ready: bool=False) -> list[dict[str, Any]]:
    """Kompatibilni facade pro title detail cache targety."""

    from filmy.db_tmdb import get_title_detail_cache_targets as _impl
    return _impl(limit=limit, include_ready=include_ready)

def _get_relevant_people_candidates(limit: int | None=None) -> list[dict[str, Any]]:
    """Vrati relevantni people kandidaty pro materializaci detailu."""

    from filmy.db_tmdb import _get_relevant_people_candidates as _impl
    return _impl(limit=limit)

def get_person_detail_cache_targets(limit: int | None=None, include_ready: bool=False) -> list[dict[str, Any]]:
    """Kompatibilni facade pro person detail cache targety."""

    from filmy.db_tmdb import get_person_detail_cache_targets as _impl
    return _impl(limit=limit, include_ready=include_ready)

def create_import_preview(source: str, filename: str, content: bytes, max_rows: int | None=None) -> dict[str, Any]:
    """Vytvori preview importniho batchu a ulozi jeho radky do runtime tabulek."""

    batch_id = str(uuid.uuid4())
    checksum = str(hash(content))
    text = content.decode('utf-8-sig', errors='replace')
    rows = _parse_import_rows(source, text)
    if max_rows is not None:
        rows = rows[:max_rows]
    resolver_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    preview_items: list[dict[str, Any]] = []
    batch_created_at = _now_iso()
    resolution_context = _build_resolution_context_postgres(source, rows)
    for idx, row in enumerate(rows, start=1):
        resolution = _resolve_import_row_postgres(source, row, resolver_cache, resolution_context)
        preview_items.append({'idx': idx, 'row': row, 'resolution': resolution})
    if source == 'netflix':
        unresolved_rows = [item['row'] for item in preview_items if item['resolution']['status'] == 'unresolved']
        if unresolved_rows:
            alias_context = _build_netflix_alias_context_postgres(unresolved_rows)
            if alias_context:
                for item in preview_items:
                    if item['resolution']['status'] != 'unresolved':
                        continue
                    alias_resolution = _resolve_netflix_alias_resolution_postgres(item['row'], alias_context)
                    if alias_resolution is not None:
                        item['resolution'] = alias_resolution
    import_row_values: list[dict[str, Any]] = []
    resolved_count = 0
    unresolved_count = 0
    for item in preview_items:
        row = item['row']
        resolution = item['resolution']
        if resolution['status'] == 'resolved':
            resolved_count += 1
        else:
            unresolved_count += 1
        import_row_values.append({'id': str(uuid.uuid4()), 'batch_id': batch_id, 'source': source, 'row_number': item['idx'], 'raw_json': json.dumps(row, ensure_ascii=False), 'parsed_title': row.get('parsed_title'), 'parsed_year': row.get('parsed_year'), 'parsed_watched_on': row.get('parsed_watched_on'), 'parsed_season_number': row.get('parsed_season_number'), 'parsed_episode_number': row.get('parsed_episode_number'), 'parsed_imdb_id': row.get('parsed_imdb_id'), 'parsed_tmdb_id': row.get('parsed_tmdb_id'), 'resolution_status': resolution['status'], 'resolved_tconst': resolution.get('tconst'), 'resolution_confidence': resolution.get('confidence'), 'resolution_note': resolution.get('note')})
    create_import_batch_record(batch_id=batch_id, source=source, filename=filename, checksum=checksum, status='previewed', created_at=batch_created_at)
    insert_import_rows(import_row_values)
    return {'batch_id': batch_id, 'source': source, 'filename': filename, 'rows_total': len(rows), 'rows_resolved': resolved_count, 'rows_unresolved': unresolved_count}

def get_import_batch(batch_id: str) -> dict[str, Any] | None:
    """Vrati importni batch i s prvnimi preview radky."""

    batch = fetch_import_batch_record(batch_id)
    if batch is None:
        return None
    return {**batch, 'rows': fetch_import_batch_rows(batch_id, limit=100)}

def commit_import_batch(batch_id: str) -> dict[str, Any]:
    """Commitne preview batch do watch eventu a vrati souhrn vysledku."""

    batch = fetch_import_batch_record(batch_id)
    if batch is None:
        raise ValueError('Import batch neexistuje.')
    if batch['status'] == 'committed':
        return {'batch_id': batch_id, 'committed': 0, 'status': 'already_committed'}
    result = commit_import_batch_postgres(batch_id=batch_id, committed_at=_now_iso())
    return {'batch_id': batch_id, 'committed': int(result['inserted_events']), 'skipped': int(result['skipped_events']), 'status': str(result['batch_status'])}

def inspect_trakt_export(export_dir: str='trakt-export') -> dict[str, Any]:
    """Kompatibilni facade pro inspekci Trakt exportu."""

    from filmy.db_legacy import inspect_trakt_export as _impl
    return _impl(export_dir)

def sync_trakt_export(export_dir: str='trakt-export') -> dict[str, Any]:
    """Kompatibilni facade pro import Trakt exportu."""

    from filmy.db_legacy import sync_trakt_export as _impl
    return _impl(export_dir)

def get_trakt_sync_runs(limit: int=20) -> list[dict[str, Any]]:
    """Kompatibilni facade pro seznam Trakt sync behu."""

    from filmy.db_legacy import get_trakt_sync_runs as _impl
    return _impl(limit=limit)

def get_trakt_sync_run(sync_run_id: str) -> dict[str, Any] | None:
    """Kompatibilni facade pro detail jednoho Trakt sync behu."""

    from filmy.db_legacy import get_trakt_sync_run as _impl
    return _impl(sync_run_id)

def get_trakt_sync_changes(sync_run_id: str | None=None, previous_sync_id: str | None=None, limit: int=100) -> dict[str, Any]:
    """Kompatibilni facade pro diff dvou Trakt sync snapshotu."""

    from filmy.db_legacy import get_trakt_sync_changes as _impl
    return _impl(sync_run_id=sync_run_id, previous_sync_id=previous_sync_id, limit=limit)

def get_watch_history(limit: int=100, source: str | None=None) -> list[dict[str, Any]]:
    """Vrati lokalni watch historii z PostgreSQL runtime vrstvy."""

    return fetch_watch_history_postgres(limit=limit, source=source)
RECENTLY_WATCHED_VIEW_ID = 'view:recently-watched'
WATCHED_VIEW_ID = 'view:watched'
HOT_WATCHLIST_VIEW_ID = 'view:hot-watchlist'

def _fetch_watch_view_page(limit: int, offset: int, *, cutoff_days: int | None) -> dict[str, Any]:
    """Vrati stranku watched view pres aktivni PostgreSQL implementaci."""

    return _fetch_watch_view_page_from_postgres(limit, offset, cutoff_days=cutoff_days)

def _fetch_watch_view_page_from_postgres(limit: int, offset: int, *, cutoff_days: int | None) -> dict[str, Any]:
    """Slozi stranku watched view z PostgreSQL read modelu."""

    total, rows = fetch_watch_view_page_rows(limit=limit, offset=offset, cutoff_days=cutoff_days)
    items = [{'tconst': row[0], 'title_type': row[1], 'title': row[2], 'year': row[3], 'season_number': None, 'episode_number': None, 'series_title': None, 'poster_url': _poster_url_from_local_path(row[4] or row[5]), 'last_watched_on': row[6], 'last_watched_at': row[7], 'end_year': None, 'runtime_minutes': None} for row in rows]
    return {'total': total, 'items': items, 'limit': limit, 'offset': offset}

def get_recently_watched_page(limit: int=50, offset: int=0) -> dict[str, Any]:
    """Kompatibilni facade pro strankovany recently watched pohled."""

    from filmy.db_library import get_recently_watched_page as _impl
    return _impl(limit=limit, offset=offset)

def get_watched_page(limit: int=50, offset: int=0) -> dict[str, Any]:
    """Kompatibilni facade pro strankovany watched pohled."""

    from filmy.db_library import get_watched_page as _impl
    return _impl(limit=limit, offset=offset)

def get_trakt_ratings(limit: int=100, active_only: bool=True) -> list[dict[str, Any]]:
    """Kompatibilni facade pro Trakt ratingy."""

    from filmy.db_legacy import get_trakt_ratings as _impl
    return _impl(limit=limit, active_only=active_only)

def get_trakt_list_overview(include_items: bool=False, active_only: bool=True) -> dict[str, Any]:
    """Kompatibilni facade pro prehled Trakt seznamu."""

    from filmy.db_legacy import get_trakt_list_overview as _impl
    return _impl(include_items=include_items, active_only=active_only)

def get_trakt_collection(limit: int=100, active_only: bool=True) -> list[dict[str, Any]]:
    """Kompatibilni facade pro Trakt collection."""

    from filmy.db_legacy import get_trakt_collection as _impl
    return _impl(limit=limit, active_only=active_only)

def get_trakt_status() -> dict[str, Any]:
    """Kompatibilni facade pro souhrn Trakt stavu."""

    from filmy.db_legacy import get_trakt_status as _impl
    return _impl()

def inspect_imdb_lists(export_dir: str='imdb_lists') -> dict[str, Any]:
    """Kompatibilni facade pro inspekci IMDb CSV seznamu."""

    from filmy.db_legacy import inspect_imdb_lists as _impl
    return _impl(export_dir)

def sync_imdb_lists(export_dir: str='imdb_lists') -> dict[str, Any]:
    """Kompatibilni facade pro import IMDb CSV seznamu."""

    from filmy.db_legacy import sync_imdb_lists as _impl
    return _impl(export_dir)

def get_imdb_lists_status() -> dict[str, Any]:
    """Kompatibilni facade pro souhrn stavu IMDb seznamu."""

    from filmy.db_legacy import get_imdb_lists_status as _impl
    return _impl()

def get_imdb_watchlist(limit: int=100, active_only: bool=True) -> list[dict[str, Any]]:
    """Kompatibilni facade pro IMDb watchlist."""

    from filmy.db_legacy import get_imdb_watchlist as _impl
    return _impl(limit=limit, active_only=active_only)

def get_imdb_favorite_people(limit: int=100, active_only: bool=True) -> list[dict[str, Any]]:
    """Kompatibilni facade pro oblibene IMDb osoby."""

    from filmy.db_legacy import get_imdb_favorite_people as _impl
    return _impl(limit=limit, active_only=active_only)

def inspect_plex_source() -> dict[str, Any]:
    """Kompatibilni facade pro inspekci Plex zdroje."""

    from filmy.db_legacy import inspect_plex_source as _impl
    return _impl()

def sync_plex_source(section_limit: int | None=None, item_limit_per_section: int | None=None) -> dict[str, Any]:
    """Kompatibilni facade pro Plex bootstrap sync."""

    from filmy.db_legacy import sync_plex_source as _impl
    return _impl(section_limit=section_limit, item_limit_per_section=item_limit_per_section)

def get_plex_status() -> dict[str, Any]:
    """Kompatibilni facade pro souhrn posledniho Plex syncu."""

    from filmy.db_legacy import get_plex_status as _impl
    return _impl()

def _upsert_plex_library_item(conn, sync_run_id: str, section: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Kompatibilni facade pro upsert jednoho Plex library itemu."""

    from filmy.db_legacy import _upsert_plex_library_item as _impl
    return _impl(conn, sync_run_id, section, snapshot)

def _sync_plex_item_to_local_library(conn, list_id: str, sync_run_id: str, snapshot: dict[str, Any], now: str) -> bool:
    """Kompatibilni facade pro propsani Plex itemu do lokalni knihovny."""

    from filmy.db_legacy import _sync_plex_item_to_local_library as _impl
    return _impl(conn, list_id, sync_run_id, snapshot, now)

def _sync_plex_watch_state(conn, snapshot: dict[str, Any]) -> bool:
    """Kompatibilni facade pro propsani Plex watched state."""

    from filmy.db_legacy import _sync_plex_watch_state as _impl
    return _impl(conn, snapshot)

def _sync_plex_content_state(conn, snapshot: dict[str, Any], now: str) -> bool:
    """Kompatibilni facade pro propsani Plex content state."""

    from filmy.db_legacy import _sync_plex_content_state as _impl
    return _impl(conn, snapshot, now)

def get_local_library_status() -> dict[str, Any]:
    """Kompatibilni facade pro lokalni library status."""

    from filmy.db_library import get_local_library_status as _impl
    return _impl()

def _poster_url_from_detail(detail: dict[str, Any] | None) -> str | None:
    """Vrati poster URL z TMDB detail payloadu."""

    from filmy.db_tmdb import _poster_url_from_detail as _impl
    return _impl(detail)

def _backdrop_url_from_detail(detail: dict[str, Any] | None) -> str | None:
    """Vrati backdrop URL z TMDB detail payloadu."""

    from filmy.db_tmdb import _backdrop_url_from_detail as _impl
    return _impl(detail)

def _latest_tmdb_asset_by_kind(assets: list[dict[str, Any]], asset_kind: str) -> dict[str, Any] | None:
    """Vybere posledni asset zadaneho druhu."""

    from filmy.db_tmdb import _latest_tmdb_asset_by_kind as _impl
    return _impl(assets, asset_kind)

def _resolve_tmdb_asset_local_path(asset: dict[str, Any] | None) -> str | None:
    """Prevede asset zaznam na lokalni filesystem cestu."""

    from filmy.db_tmdb import _resolve_tmdb_asset_local_path as _impl
    return _impl(asset)

def _poster_url_from_local_path(local_path_value: str | None) -> str | None:
    """Prevede lokalni poster cestu na mountnutou URL."""

    return _asset_url_from_local_path(local_path_value, assets_root=ASSETS_DIR, mount_path='/assets/tmdb')

def _asset_url_from_local_path(local_path_value: str | None, *, assets_root: Path, mount_path: str) -> str | None:
    """Prevede lokalni asset cestu na URL pod danym mount pointem."""

    if not local_path_value:
        return None
    local_path = Path(str(local_path_value))
    if not local_path.is_absolute():
        relative_path = local_path.as_posix().lstrip('/')
        return f'{mount_path}/{relative_path}' if relative_path else None
    try:
        relative_path = local_path.relative_to(assets_root).as_posix()
        return f'{mount_path}/{relative_path}'
    except ValueError:
        marker_parts = assets_root.parts[-2:]
        local_parts = local_path.parts
        for index in range(len(local_parts) - len(marker_parts) + 1):
            if tuple(local_parts[index:index + len(marker_parts)]) != marker_parts:
                continue
            relative_parts = local_parts[index + len(marker_parts):]
            if not relative_parts:
                return None
            return f"{mount_path}/{'/'.join(relative_parts)}"
        return None

def get_continue_watching_items(limit: int=5) -> list[dict[str, Any]]:
    """Kompatibilni facade pro continue watching karty."""

    from filmy.db_library import get_continue_watching_items as _impl
    return _impl(limit=limit)

def get_hot_watchlist_page(limit: int=50, offset: int=0, available_in_cz: bool=False) -> dict[str, Any]:
    """Kompatibilni facade pro hot watchlist page."""

    from filmy.db_library import get_hot_watchlist_page as _impl
    return _impl(limit=limit, offset=offset, available_in_cz=available_in_cz)

def get_user_list_items_page(list_id: str, limit: int=50, offset: int=0, available_in_cz: bool=False) -> dict[str, Any]:
    """Kompatibilni facade pro strankovany detail uzivatelskeho seznamu."""

    from filmy.db_library import get_user_list_items_page as _impl
    return _impl(list_id, limit=limit, offset=offset, available_in_cz=available_in_cz)

def get_user_list_items(list_id: str, limit: int=12) -> list[dict[str, Any]]:
    """Kompatibilni facade pro kratky vyrez polozek uzivatelskeho seznamu."""

    from filmy.db_library import get_user_list_items as _impl
    return _impl(list_id, limit=limit)

def format_czech_datetime(value: Any) -> str | None:
    """Naformatuje datum/cas do kratkeho ceskeho zobrazeni."""

    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
    return dt.strftime('%d. %m. %Y %H:%M')

def _sync_trakt_history(conn, sync_run_id: str, files: list[dict[str, Any]]) -> dict[str, int]:
    """Kompatibilni facade pro interni import Trakt historie."""

    from filmy.db_legacy import _sync_trakt_history as _impl
    return _impl(conn, sync_run_id, files)

def _sync_trakt_ratings(conn, sync_run_id: str, files: list[dict[str, Any]]) -> dict[str, int]:
    """Kompatibilni facade pro interni import Trakt ratingu."""

    from filmy.db_legacy import _sync_trakt_ratings as _impl
    return _impl(conn, sync_run_id, files)

def _sync_trakt_lists(conn, sync_run_id: str, metadata_files: list[dict[str, Any]], custom_list_files: list[dict[str, Any]], watchlist_files: list[dict[str, Any]]) -> dict[str, int]:
    """Kompatibilni facade pro interni import Trakt seznamu."""

    from filmy.db_legacy import _sync_trakt_lists as _impl
    return _impl(conn, sync_run_id, metadata_files, custom_list_files, watchlist_files)

def _sync_trakt_collection(conn, sync_run_id: str, files: list[dict[str, Any]]) -> dict[str, int]:
    """Kompatibilni facade pro interni import Trakt collection."""

    from filmy.db_legacy import _sync_trakt_collection as _impl
    return _impl(conn, sync_run_id, files)

def _read_last_activities(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Kompatibilni facade pro payload `last_activities` z Trakt exportu."""

    from filmy.db_legacy import _read_last_activities as _impl
    return _impl(files)

def _get_catalog_refresh_state(conn) -> tuple[bool, bool]:
    """Vrati, jestli katalog potrebuje rebuild nebo jen refresh manifestu."""

    table_exists = conn.execute("\n            SELECT COUNT(*)\n            FROM information_schema.tables\n            WHERE table_schema = 'app' AND table_name = 'catalog_titles'\n            ").fetchone()[0]
    if table_exists == 0:
        return (True, False)
    manifest_rows = fetch_imdb_manifest_rows()
    if not manifest_rows:
        return (True, False)
    meta_rows = fetch_catalog_refresh_rows()
    stored = {row['source_key']: {'path': row['source_path'], 'mtime': row['source_mtime'], 'size': row['source_size'], 'sha256': row['source_sha256']} for row in manifest_rows}
    manifest_needs_update = not bool(meta_rows)
    for source in SOURCE_FILES:
        current_mtime = source.stat_mtime
        current_size = source.stat_size
        current_path = source.path.as_posix()
        stored_row = stored.get(source.key)
        if stored_row is None:
            return (True, False)
        if stored_row['size'] != current_size:
            return (True, False)
        path_changed = stored_row['path'] != current_path
        mtime_changed = stored_row['mtime'] != current_mtime
        if path_changed or mtime_changed:
            if stored_row['sha256'] != source.sha256:
                return (True, False)
            manifest_needs_update = True
    return (False, manifest_needs_update)

def _store_imdb_file_manifest(conn) -> None:
    """Ulozi aktualni manifest IMDb zdrojovych souboru."""

    now = _now_iso()
    rows = [{'source_key': source.key, 'source_path': source.path.as_posix(), 'source_mtime': source.stat_mtime, 'source_size': source.stat_size, 'source_sha256': source.sha256, 'recorded_at': now} for source in SOURCE_FILES]
    replace_imdb_manifest_rows(rows)
    return

def _store_catalog_refresh_meta(conn) -> None:
    """Ulozi lehky fingerprint refresh stavu katalogu."""

    replace_catalog_refresh_meta_rows([{'source_key': source.key, 'fingerprint': f'{source.stat_mtime}:{source.stat_size}'} for source in SOURCE_FILES])
    return

def _ensure_user_list(conn, list_id: str, name: str, list_kind: str, source_origin: str, source_ref: str, now: str, *, description: str | None=None, preferred_slug: str | None=None) -> str:
    """Zajisti existenci uzivatelskeho seznamu a vrati jeho ID."""

    slug = preferred_slug or _slugify(name) or list_id
    conn.execute('\n        INSERT INTO app.user_lists (id, slug, name, description, list_kind, source_origin, source_ref, created_at, updated_at)\n        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n        ON CONFLICT (id) DO UPDATE SET\n            slug = excluded.slug,\n            name = excluded.name,\n            description = CASE\n                WHEN excluded.description IS NOT NULL THEN excluded.description\n                ELSE app.user_lists.description\n            END,\n            list_kind = excluded.list_kind,\n            updated_at = excluded.updated_at\n        ', [list_id, slug, name, description, list_kind, source_origin, source_ref, now, now])
    return list_id

def _upsert_user_list_item(conn, *, list_id: str, canonical_key: str, tconst: str | None, media_type: str, imdb_id: str | None, tmdb_id: int | None, trakt_id: int | None, parent_tconst: str | None, parent_title: str | None, title: str | None, season_number: int | None, episode_number: int | None, rank: int | None, added_at: str | None, notes: str | None, source_origin: str, source_ref: str | None, now: str) -> None:
    """Upsertne jednu polozku uzivatelskeho seznamu podle canonical key."""

    item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{list_id}|{canonical_key}'))
    conn.execute('\n        INSERT INTO app.user_list_items (\n            id, list_id, canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title,\n            title, season_number, episode_number, rank, added_at, notes, source_origin, source_ref,\n            is_archived, created_at, updated_at\n        )\n        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, ?, ?)\n        ON CONFLICT (list_id, canonical_key) DO UPDATE SET\n            tconst = COALESCE(app.user_list_items.tconst, excluded.tconst),\n            imdb_id = COALESCE(app.user_list_items.imdb_id, excluded.imdb_id),\n            tmdb_id = COALESCE(app.user_list_items.tmdb_id, excluded.tmdb_id),\n            trakt_id = COALESCE(app.user_list_items.trakt_id, excluded.trakt_id),\n            parent_tconst = COALESCE(app.user_list_items.parent_tconst, excluded.parent_tconst),\n            parent_title = COALESCE(app.user_list_items.parent_title, excluded.parent_title),\n            title = COALESCE(app.user_list_items.title, excluded.title),\n            rank = COALESCE(app.user_list_items.rank, excluded.rank),\n            added_at = CASE\n                WHEN app.user_list_items.is_archived THEN COALESCE(excluded.added_at, app.user_list_items.added_at)\n                ELSE COALESCE(app.user_list_items.added_at, excluded.added_at)\n            END,\n            notes = COALESCE(app.user_list_items.notes, excluded.notes),\n            is_archived = FALSE,\n            updated_at = excluded.updated_at\n        ', [item_id, list_id, canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title, title, season_number, episode_number, rank, added_at, notes, source_origin, source_ref, now, now])

def _upsert_user_rating(conn, *, canonical_key: str, tconst: str | None, media_type: str, imdb_id: str | None, tmdb_id: int | None, trakt_id: int | None, parent_tconst: str | None, parent_title: str | None, title: str | None, season_number: int | None, episode_number: int | None, rating: int, rated_at: str | None, source_origin: str, source_ref: str | None, now: str, liked_notes: str | None=None, disliked_notes: str | None=None) -> None:
    """Upsertne uzivatelsky rating nad canonical identitou media."""

    conn.execute('\n        INSERT INTO app.user_ratings (\n            canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title, title,\n            season_number, episode_number, rating, liked_notes, disliked_notes, rated_at,\n            source_origin, source_ref, created_at, updated_at\n        )\n        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n        ON CONFLICT (canonical_key) DO UPDATE SET\n            tconst = COALESCE(app.user_ratings.tconst, excluded.tconst),\n            imdb_id = COALESCE(app.user_ratings.imdb_id, excluded.imdb_id),\n            tmdb_id = COALESCE(app.user_ratings.tmdb_id, excluded.tmdb_id),\n            trakt_id = COALESCE(app.user_ratings.trakt_id, excluded.trakt_id),\n            parent_tconst = COALESCE(app.user_ratings.parent_tconst, excluded.parent_tconst),\n            parent_title = COALESCE(app.user_ratings.parent_title, excluded.parent_title),\n            title = COALESCE(app.user_ratings.title, excluded.title),\n            rating = excluded.rating,\n            liked_notes = excluded.liked_notes,\n            disliked_notes = excluded.disliked_notes,\n            rated_at = COALESCE(excluded.rated_at, app.user_ratings.rated_at),\n            updated_at = excluded.updated_at\n        ', [canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title, title, season_number, episode_number, rating, liked_notes, disliked_notes, rated_at, source_origin, source_ref, now, now])

def _catalog_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    """Prevede katalogovy SQL radek na slovnik pouzivany ve facade."""

    return {'tconst': row[0], 'title_type': row[1], 'primary_title': row[2], 'original_title': row[3], 'start_year': row[4], 'end_year': row[5] if len(row) > 9 else None, 'runtime_minutes': row[6] if len(row) > 9 else row[5], 'genres': (row[7] if len(row) > 9 else row[6]).split(',') if (row[7] if len(row) > 9 else row[6]) else [], 'average_rating': row[8] if len(row) > 9 else row[7], 'num_votes': row[9] if len(row) > 9 else row[8]}

def _build_local_media_identity(detail: dict[str, Any]) -> dict[str, Any]:
    """Slozi jednotnou lokalni identitu pro title nebo episode detail."""

    if detail['kind'] == 'episode':
        return {'tconst': detail['tconst'], 'media_type': 'episode', 'imdb_id': detail['tconst'], 'tmdb_id': (detail.get('tmdb') or {}).get('tmdb_id'), 'parent_tconst': detail.get('series_tconst'), 'parent_title': None, 'title': detail.get('primary_title'), 'season_number': detail.get('season_number'), 'episode_number': detail.get('episode_number')}
    return {'tconst': detail['tconst'], 'media_type': 'title', 'imdb_id': detail['tconst'], 'tmdb_id': (detail.get('tmdb') or {}).get('tmdb_id'), 'parent_tconst': None, 'parent_title': None, 'title': detail.get('primary_title'), 'season_number': None, 'episode_number': None}

def _get_library_summary_for_tconst(tconst: str) -> dict[str, Any]:
    """Vrati lokalni library summary pro title nebo episode `tconst`."""

    title = fetch_catalog_title_row(tconst)
    if title is not None:
        return _fetch_library_summary(None, tconst, title[1])
    episode = fetch_catalog_episode_row(tconst)
    if episode is not None:
        return _fetch_library_summary(None, tconst, 'tvEpisode')
    raise ValueError('Titul nebyl nalezen.')

def _fetch_aliases(conn, tconst: str) -> list[dict[str, Any]]:
    """Vrati omezeny seznam aliasu titulu."""

    rows = fetch_title_alias_rows(tconst, limit=20)
    return [{'title': row[0], 'region': row[1], 'language': row[2], 'types': row[3], 'is_original_title': row[4]} for row in rows]

def _fetch_tmdb(conn, tconst: str) -> dict[str, Any] | None:
    """Vrati sjednoceny TMDB snapshot pro title detail."""

    ui_config = get_ui_config()
    primary_locale, fallback_locale = ui_config.tmdb_locale_order
    snapshot = fetch_tmdb_payload_snapshot(tconst, primary_locale=primary_locale, fallback_locale=fallback_locale)
    if snapshot is None:
        return None
    mapping = snapshot['mapping']
    return {'media_type': mapping['tmdb_media_type'], 'tmdb_id': mapping['tmdb_id'], 'matched_by': mapping['matched_by'], 'matched_at': mapping['matched_at'], 'sync_status': mapping['sync_status'], 'last_error': mapping['last_error'], 'details': snapshot['details'], 'detail_locales': snapshot['detail_locales'], 'providers': snapshot['providers'], 'assets': snapshot['assets']}

def _fetch_content_state(conn, tconst: str) -> dict[str, Any] | None:
    """Vrati lokalni content state titulu v serializovanem tvaru."""

    state = fetch_content_state_postgres(tconst)
    if state is None:
        return None
    return {'interest_state': state['interest_state'], 'last_previewed_at': state['last_previewed_at'], 'last_watched_at': state['last_watched_at'], 'updated_at': state['updated_at']}

def _fetch_library_summary(conn, tconst: str, title_type: str | None) -> dict[str, Any]:
    """Vrati z PostgreSQL read modelu souhrn lokalni knihovny pro titul."""

    return fetch_library_summary_snapshot(tconst, title_type)

def _fetch_watch_stats_from_postgres(tconst: str, title_type: str | None) -> tuple[int, datetime | None]:
    """Vrati watched count a posledni watched timestamp pro titul nebo serial."""

    if title_type in ('tvSeries', 'tvMiniSeries'):
        episode_rows = fetch_series_episode_rows(tconst)
        episode_tconsts = [str(row[0]) for row in episode_rows]
        stats_by_tconst = fetch_watch_stats_for_tconsts(episode_tconsts)
        watched_count = sum((int(item.get('watched_count') or 0) for item in stats_by_tconst.values()))
        last_values = [item.get('last_watched_at') for item in stats_by_tconst.values() if item.get('last_watched_at') is not None]
        return (watched_count, max(last_values) if last_values else None)
    direct = fetch_watch_stats_for_tconsts([tconst]).get(str(tconst))
    if direct is None:
        return (0, None)
    return (int(direct.get('watched_count') or 0), direct.get('last_watched_at'))

def _parse_import_rows(source: str, text: str) -> list[dict[str, Any]]:
    """Rozparsuje raw importni CSV podle typu zdroje."""

    reader = csv.DictReader(io.StringIO(text))
    if source == 'netflix':
        return [_parse_netflix_row(row) for row in reader]
    if source == 'trakt':
        return [_parse_trakt_row(row) for row in reader]
    raise ValueError(f'Nepodporovaný source: {source}')

def _parse_netflix_row(row: dict[str, str | None]) -> dict[str, Any]:
    """Prevede jeden radek Netflix CSV na interny import format."""

    title = (row.get('Title') or row.get('title') or '').strip()
    watched_on = _parse_netflix_date((row.get('Date') or row.get('date') or '').strip())
    parts = [part.strip() for part in title.split(':')]
    parsed_title = parts[0] if parts else title
    season_number = None
    episode_number = None
    episode_title = None
    if len(parts) > 1:
        parsed_title = parts[0]
        season_number = _extract_season_number(parts[1])
    if len(parts) > 2:
        episode_title = ': '.join((part.strip() for part in parts[2:] if part.strip())) or None
        episode_number = _extract_episode_number(episode_title)
    year = _extract_year(parsed_title)
    return {'parsed_title': parsed_title, 'parsed_year': year, 'parsed_watched_on': watched_on or None, 'parsed_season_number': season_number, 'parsed_episode_number': episode_number, 'parsed_imdb_id': None, 'parsed_tmdb_id': None, 'series_title': parsed_title, 'episode_title': episode_title, 'raw': row}

def _parse_trakt_row(row: dict[str, str | None]) -> dict[str, Any]:
    """Prevede jeden radek Trakt CSV na interny import format."""

    title = (row.get('title') or row.get('Title') or '').strip()
    year = _safe_int(row.get('year') or row.get('Year'))
    watched_on = (row.get('watched_at') or row.get('Watched At') or row.get('watched_on') or '').strip()
    season_number = _safe_int(row.get('season') or row.get('Season'))
    episode_number = _safe_int(row.get('episode') or row.get('Episode'))
    imdb_id = (row.get('imdb_id') or row.get('IMDb ID') or '').strip() or None
    tmdb_id = _safe_int(row.get('tmdb_id') or row.get('TMDB ID'))
    return {'parsed_title': title, 'parsed_year': year, 'parsed_watched_on': watched_on or None, 'parsed_season_number': season_number, 'parsed_episode_number': episode_number, 'parsed_imdb_id': imdb_id, 'parsed_tmdb_id': tmdb_id, 'raw': row}

def _resolve_import_row_postgres(source: str, row: dict[str, Any], resolver_cache: dict[tuple[Any, ...], dict[str, Any]] | None=None, resolution_context: dict[str, Any] | None=None) -> dict[str, Any]:
    """Zkusi sparovat jeden importni radek na lokalni titul nebo epizodu."""

    cache_key = (source, row.get('parsed_title'), row.get('parsed_year'), row.get('parsed_season_number'), row.get('parsed_episode_number'), row.get('parsed_imdb_id'), row.get('parsed_tmdb_id'), row.get('series_title'), row.get('episode_title'))
    if resolver_cache is not None and cache_key in resolver_cache:
        return resolver_cache[cache_key]
    imdb_id = row.get('parsed_imdb_id')
    if imdb_id:
        found = fetch_catalog_title_row(str(imdb_id))
        if found is not None:
            return _cache_resolution(resolver_cache, cache_key, {'status': 'resolved', 'tconst': str(found[0]), 'confidence': 1.0, 'note': 'matched_by_imdb_id'})
    tmdb_id = row.get('parsed_tmdb_id')
    if tmdb_id is not None:
        found_tconst = fetch_tconst_for_tmdb_id(int(tmdb_id))
        if found_tconst is not None:
            return _cache_resolution(resolver_cache, cache_key, {'status': 'resolved', 'tconst': found_tconst, 'confidence': 0.95, 'note': 'matched_by_tmdb_id'})
    if source == 'netflix' and resolution_context is not None:
        episode_tconst = _resolve_netflix_episode_from_context(row, resolution_context)
        if episode_tconst:
            return _cache_resolution(resolver_cache, cache_key, {'status': 'resolved', 'tconst': episode_tconst, 'confidence': 0.9, 'note': 'matched_by_episode_context'})
        title_tconst = _resolve_netflix_title_from_context(row, resolution_context)
        if title_tconst:
            return _cache_resolution(resolver_cache, cache_key, {'status': 'resolved', 'tconst': title_tconst, 'confidence': 0.8, 'note': 'matched_by_title_context'})
    if row.get('parsed_season_number') is not None or row.get('parsed_episode_number') is not None:
        series_title = row.get('series_title') or row.get('parsed_title')
        series_tconst = None
        if resolution_context is not None:
            series_title_lower = (series_title or '').strip().lower()
            series_title_key = _normalize_match_key(series_title)
            series_tconst = resolution_context['title_map'].get(series_title_lower) or resolution_context['normalized_title_map'].get(series_title_key)
        if series_tconst is None and series_title:
            series_tconst = fetch_primary_title_matches([(series_title or '').strip().lower()]).get((series_title or '').strip().lower()) or fetch_title_lookup_primary_key_matches([_normalize_match_key(series_title)]).get(_normalize_match_key(series_title))
        if series_tconst is not None:
            found = _resolve_episode_by_series_tconst_postgres(str(series_tconst), row.get('parsed_season_number'), row.get('parsed_episode_number'), row.get('episode_title'))
            if found:
                note = 'matched_by_episode_title' if row.get('episode_title') else 'matched_by_episode'
                confidence = 0.9 if row.get('episode_title') else 0.85
                return _cache_resolution(resolver_cache, cache_key, {'status': 'resolved', 'tconst': found, 'confidence': confidence, 'note': note})
    parsed_title = row.get('parsed_title')
    if parsed_title:
        found_tconst = fetch_title_by_primary_title_year(str(parsed_title), row.get('parsed_year'))
        if found_tconst is not None:
            return _cache_resolution(resolver_cache, cache_key, {'status': 'resolved', 'tconst': found_tconst, 'confidence': 0.8, 'note': 'matched_by_title_year'})
    if source != 'netflix' and parsed_title:
        alias_key = _normalize_match_key(parsed_title)
        found_tconst = fetch_title_alias_lookup_matches([alias_key]).get(alias_key)
        if found_tconst is not None:
            return _cache_resolution(resolver_cache, cache_key, {'status': 'resolved', 'tconst': found_tconst, 'confidence': 0.7, 'note': 'matched_by_alias'})
    return _cache_resolution(resolver_cache, cache_key, {'status': 'unresolved', 'tconst': None, 'confidence': 0.0, 'note': f'unresolved_{source}'})

def _extract_year(title: str) -> int | None:
    """Vytahne rok z tvaru `Title (1999)` pokud je v nazvu pritomny."""

    if len(title) < 6:
        return None
    for idx in range(len(title) - 5):
        chunk = title[idx:idx + 6]
        if chunk.startswith('(') and chunk.endswith(')') and chunk[1:5].isdigit():
            return int(chunk[1:5])
    return None

def _extract_season_number(value: str | None) -> int | None:
    """Vytahne cislo sezony z textu typu `Season 2`."""

    if not value:
        return None
    match = re.search('season\\s+(\\d+)', value, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def _extract_episode_number(value: str | None) -> int | None:
    """Vytahne cislo epizody z textu typu `Episode 5`."""

    if not value:
        return None
    match = re.fullmatch('episode\\s+(\\d+)', value.strip(), flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def _parse_netflix_date(value: str | None) -> str | None:
    """Prevede Netflix datum na ISO format, kdyz jde rozumne rozparsovat."""

    if not value:
        return None
    for fmt in ('%m/%d/%y', '%m/%d/%Y'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value

def _safe_int(value: Any) -> int | None:
    """Bezpecne prevede hodnotu na `int`, jinak vrati `None`."""

    if value in (None, '', '\\N'):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None

def _parse_unix_timestamp(value: Any) -> str | None:
    """Prevede unix timestamp na UTC ISO string."""

    parsed = _safe_int(value)
    if parsed is None:
        return None
    return datetime.fromtimestamp(parsed, UTC).isoformat()

def _safe_float(value: Any) -> float | None:
    """Bezpecne prevede hodnotu na `float`, jinak vrati `None`."""

    if value in (None, '', '\\N'):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None

def _parse_iso_date(value: Any) -> str | None:
    """Prevede bezne datumove formaty na ISO datum."""

    if value in (None, '', '\\N'):
        return None
    text = str(value).strip()
    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None

def _slugify(value: str | None) -> str:
    """Prevede text na jednoduche slug ID."""

    return _normalize_match_key(value).replace(' ', '-')

def _plex_source_key(rating_key: str) -> str:
    """Vrati stabilni source key pro Plex rating key."""

    return f'plex:{rating_key}'

def _plex_item_is_watched(snapshot: dict[str, Any]) -> bool:
    """Rozhodne, jestli Plex snapshot znamena dokoukany titul."""

    view_count = _safe_int(snapshot.get('view_count')) or 0
    viewed_leaf_count = _safe_int(snapshot.get('viewed_leaf_count')) or 0
    leaf_count = _safe_int(snapshot.get('leaf_count')) or 0
    return view_count > 0 or (leaf_count > 0 and viewed_leaf_count >= leaf_count)

def _plex_item_is_in_progress(snapshot: dict[str, Any]) -> bool:
    """Rozhodne, jestli je Plex snapshot rozkoukany, ale ne dokoukany."""

    if _plex_item_is_watched(snapshot):
        return False
    viewed_leaf_count = _safe_int(snapshot.get('viewed_leaf_count')) or 0
    leaf_count = _safe_int(snapshot.get('leaf_count')) or 0
    return leaf_count > 0 and viewed_leaf_count > 0

def _plex_fingerprint(server_client_identifier: str, sections: list[dict[str, Any]]) -> str:
    """Spocita fingerprint Plex serveru a importovatelnych sekci."""

    payload = {'server_client_identifier': server_client_identifier, 'sections': [{'key': section.get('key'), 'type': section.get('type'), 'title': section.get('title'), 'updatedAt': section.get('updatedAt'), 'scannedAt': section.get('scannedAt'), 'contentChangedAt': section.get('contentChangedAt')} for section in sections]}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

def _canonical_media_key(media_type: str | None, tconst: str | None, imdb_id: str | None, tmdb_id: int | None, trakt_id: int | None, season_number: int | None, episode_number: int | None) -> str:
    """Slozi priorizovany canonical key pro lokalni title/episode identitu."""

    normalized_type = (media_type or 'title').lower()
    if tconst:
        return f'{normalized_type}:tconst:{tconst}'
    if imdb_id:
        return f'{normalized_type}:imdb:{imdb_id}'
    if tmdb_id is not None:
        return f'{normalized_type}:tmdb:{tmdb_id}'
    if trakt_id is not None:
        return f'{normalized_type}:trakt:{trakt_id}'
    return f'{normalized_type}:s{season_number or 0}:e{episode_number or 0}'

def _cache_resolution(resolver_cache: dict[tuple[Any, ...], dict[str, Any]] | None, cache_key: tuple[Any, ...], resolution: dict[str, Any]) -> dict[str, Any]:
    """Ulozi resolution do cache a vrati ji zpet volajicimu."""

    if resolver_cache is not None:
        resolver_cache[cache_key] = resolution
    return resolution

def _build_resolution_context_postgres(source: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Predpripravi lookup mapy pro davkove parsovani Netflix importu."""

    if source != 'netflix':
        return None
    title_names = sorted({(row.get('parsed_title') or '').strip().lower() for row in rows if row.get('parsed_title')})
    title_keys = sorted({_normalize_match_key(row.get('parsed_title')) for row in rows if row.get('parsed_title')})
    series_names = sorted({(row.get('series_title') or '').strip().lower() for row in rows if row.get('series_title')})
    series_keys = sorted({_normalize_match_key(row.get('series_title')) for row in rows if row.get('series_title')})
    title_map = fetch_primary_title_matches(title_names)
    normalized_title_map = fetch_title_lookup_primary_key_matches(title_keys)
    series_title_map = fetch_primary_title_matches(series_names)
    series_title_key_map = fetch_title_lookup_primary_key_matches(series_keys)
    episode_by_number: dict[tuple[str, int, int], str] = {}
    episode_by_title: dict[tuple[str, int, str], str] = {}
    normalized_episode_by_number: dict[tuple[str, int, int], str] = {}
    normalized_episode_by_title: dict[tuple[str, int, str], str] = {}
    series_names_by_tconst: dict[str, list[str]] = {}
    for series_lower, tconst in series_title_map.items():
        series_names_by_tconst.setdefault(str(tconst), []).append(str(series_lower))
    series_keys_by_tconst: dict[str, list[str]] = {}
    for series_key, tconst in series_title_key_map.items():
        series_keys_by_tconst.setdefault(str(tconst), []).append(str(series_key))
    for series_tconst in sorted(set(series_names_by_tconst) | set(series_keys_by_tconst)):
        episode_rows = fetch_series_episode_rows(series_tconst)
        lower_names = series_names_by_tconst.get(series_tconst, [])
        normalized_keys = series_keys_by_tconst.get(series_tconst, [])
        for episode_tconst, season_number, episode_number, primary_title, _start_year in episode_rows:
            episode_title_lower = str(primary_title or '').strip().lower()
            episode_title_key = _normalize_match_key(primary_title)
            if season_number is not None and episode_number is not None:
                for series_lower in lower_names:
                    episode_by_number.setdefault((series_lower, int(season_number), int(episode_number)), str(episode_tconst))
                for series_key in normalized_keys:
                    normalized_episode_by_number.setdefault((series_key, int(season_number), int(episode_number)), str(episode_tconst))
            if season_number is not None and episode_title_lower:
                for series_lower in lower_names:
                    episode_by_title.setdefault((series_lower, int(season_number), episode_title_lower), str(episode_tconst))
                for series_key in normalized_keys:
                    normalized_episode_by_title.setdefault((series_key, int(season_number), episode_title_key), str(episode_tconst))
    return {'title_map': title_map, 'normalized_title_map': normalized_title_map, 'episode_by_number': episode_by_number, 'episode_by_title': episode_by_title, 'normalized_episode_by_number': normalized_episode_by_number, 'normalized_episode_by_title': normalized_episode_by_title}

def _resolve_netflix_episode_from_context(row: dict[str, Any], resolution_context: dict[str, Any]) -> str | None:
    """Zkusi sparovat Netflix radek na konkretni epizodu pres predpripraveny kontext."""

    series_title = (row.get('series_title') or '').strip().lower()
    series_key = _normalize_match_key(row.get('series_title'))
    season_number = row.get('parsed_season_number')
    episode_number = row.get('parsed_episode_number')
    episode_title = (row.get('episode_title') or '').strip().lower()
    episode_key = _normalize_match_key(row.get('episode_title'))
    if series_title and season_number is not None and (episode_number is not None):
        match = resolution_context['episode_by_number'].get((series_title, season_number, episode_number))
        if match:
            return match
    if series_key and season_number is not None and (episode_number is not None):
        match = resolution_context['normalized_episode_by_number'].get((series_key, season_number, episode_number))
        if match:
            return match
    if series_title and season_number is not None and episode_title:
        return resolution_context['episode_by_title'].get((series_title, season_number, episode_title))
    if series_key and season_number is not None and episode_key:
        match = resolution_context['normalized_episode_by_title'].get((series_key, season_number, episode_key))
        if match:
            return match
    if series_key and episode_key:
        for key in ((series_key, season_number or 0, episode_key), (series_key, 0, episode_key)):
            match = resolution_context['normalized_episode_by_title'].get(key)
            if match:
                return match
    return None

def _resolve_netflix_title_from_context(row: dict[str, Any], resolution_context: dict[str, Any]) -> str | None:
    """Zkusi sparovat Netflix radek primo na titul nebo serial."""

    title = (row.get('parsed_title') or '').strip().lower()
    title_key = _normalize_match_key(row.get('parsed_title'))
    if not title:
        return None
    return resolution_context['title_map'].get(title) or resolution_context['normalized_title_map'].get(title_key)

def _build_netflix_alias_context_postgres(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pripravi aliasovy fallback kontext pro Netflix import."""

    title_keys = sorted({_normalize_match_key(row.get('parsed_title')) for row in rows if row.get('parsed_title')})
    if not title_keys:
        return None
    title_map = fetch_title_alias_lookup_matches(title_keys)
    if not title_map:
        return None
    return {'title_map': title_map}

def _resolve_netflix_alias_resolution_postgres(row: dict[str, Any], alias_context: dict[str, Any]) -> dict[str, Any] | None:
    """Zkusi sparovat Netflix radek pres title aliasy."""

    title_key = _normalize_match_key(row.get('parsed_title'))
    alias_tconst = alias_context['title_map'].get(title_key)
    if alias_tconst is None:
        return None
    if row.get('parsed_season_number') is None and row.get('parsed_episode_number') is None and (not row.get('episode_title')):
        return {'status': 'resolved', 'tconst': alias_tconst, 'confidence': 0.72, 'note': 'matched_by_alias_title'}
    episode = _resolve_episode_by_series_tconst_postgres(alias_tconst, row.get('parsed_season_number'), row.get('parsed_episode_number'), row.get('episode_title'))
    if episode:
        return {'status': 'resolved', 'tconst': episode, 'confidence': 0.78, 'note': 'matched_by_alias_series'}
    return {'status': 'resolved', 'tconst': alias_tconst, 'confidence': 0.7, 'note': 'matched_by_alias_title'}

def _resolve_episode_by_series_tconst_postgres(series_tconst: str, season_number: int | None, episode_number: int | None, episode_title: str | None) -> str | None:
    """Najde epizodu v ramci serialu podle season/episode nebo nazvu epizody."""

    episode_rows = fetch_series_episode_rows(series_tconst)
    normalized_episode_title = _normalize_match_key(episode_title)
    for episode_tconst, row_season_number, row_episode_number, primary_title, _start_year in episode_rows:
        if season_number is not None and episode_number is not None and (row_season_number == season_number) and (row_episode_number == episode_number):
            return str(episode_tconst)
    if episode_title:
        for episode_tconst, _row_season_number, _row_episode_number, primary_title, _start_year in episode_rows:
            if _normalize_match_key(primary_title) == normalized_episode_title:
                return str(episode_tconst)
    return None

def _resolve_export_path(export_dir: str) -> Path:
    """Prevede exportni adresar na overenou absolutni cestu."""

    raw_path = Path(export_dir)
    path = raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path
    path = path.resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f'Adresář s Trakt exportem neexistuje: {path}')
    return path

def _sync_imdb_watchlist(conn, sync_run_id: str, file_info: dict[str, Any] | None) -> dict[str, int]:
    """Kompatibilni facade pro interni import IMDb watchlistu."""

    from filmy.db_legacy import _sync_imdb_watchlist as _impl
    return _impl(conn, sync_run_id, file_info)

def _sync_imdb_favorite_people(conn, sync_run_id: str, file_info: dict[str, Any] | None) -> dict[str, int]:
    """Kompatibilni facade pro interni import IMDb oblibenych osob."""

    from filmy.db_legacy import _sync_imdb_favorite_people as _impl
    return _impl(conn, sync_run_id, file_info)

def _loads_json_or_none(value: str | None) -> Any:
    """Bezpecne nacte JSON nebo vrati `None` pro prazdnou hodnotu."""

    if not value:
        return None
    return json.loads(value)

def _dumps_json_or_none(value: Any) -> str | None:
    """Serializuje JSON nebo vrati `None` pro prazdny payload."""

    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)

def _fetch_change_rows(conn, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    """Spusti diff dotaz a vrati vysledky jako slovnikove radky."""

    rows = conn.execute(sql, params).fetchall()
    return [{'entity_id': row[0], 'media_type': row[1], 'parent_title': row[2], 'title': row[3], 'changed_at': row[4]} for row in rows]

def _trakt_list_item_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    """Prevede SQL radek Trakt list itemu na serializovany slovnik."""

    return {'source_key': row[0], 'media_type': row[1], 'imdb_id': row[2], 'tmdb_id': row[3], 'tconst': row[4], 'parent_title': row[5], 'title': row[6], 'season_number': row[7], 'episode_number': row[8], 'rank': row[9], 'listed_at': row[10], 'notes': row[11], 'my_rating': row[12], 'is_active': row[13]}

def _has_trakt_snapshot(conn, table_name: str, sync_run_id: str) -> bool:
    """Vrati, jestli pro dany sync beh uz existuje snapshot tabulka."""

    return conn.execute(f'SELECT COUNT(*) FROM {table_name} WHERE sync_run_id = ?', [sync_run_id]).fetchone()[0] > 0

def _backfill_trakt_snapshots_for_run(conn, sync_run_id: str) -> None:
    """Doplni snapshoty pro starsi Trakt beh, pokud tehdy jeste nevznikly."""

    if not _has_trakt_snapshot(conn, 'old.trakt_history_snapshot', sync_run_id):
        conn.execute('\n            INSERT INTO old.trakt_history_snapshot (sync_run_id, history_id)\n            SELECT ?, history_id\n            FROM old.trakt_history_events\n            WHERE is_active = TRUE AND last_seen_sync_id = ?\n            ', [sync_run_id, sync_run_id])
    if not _has_trakt_snapshot(conn, 'old.trakt_ratings_snapshot', sync_run_id):
        conn.execute('\n            INSERT INTO old.trakt_ratings_snapshot (sync_run_id, source_key)\n            SELECT ?, source_key\n            FROM old.trakt_ratings\n            WHERE is_active = TRUE AND last_seen_sync_id = ?\n            ', [sync_run_id, sync_run_id])
    if not _has_trakt_snapshot(conn, 'old.trakt_list_items_snapshot', sync_run_id):
        conn.execute('\n            INSERT INTO old.trakt_list_items_snapshot (sync_run_id, source_key)\n            SELECT ?, source_key\n            FROM old.trakt_list_items\n            WHERE is_active = TRUE AND last_seen_sync_id = ?\n            ', [sync_run_id, sync_run_id])
    if not _has_trakt_snapshot(conn, 'old.trakt_collection_snapshot', sync_run_id):
        conn.execute('\n            INSERT INTO old.trakt_collection_snapshot (sync_run_id, source_key)\n            SELECT ?, source_key\n            FROM old.trakt_collection_items\n            WHERE is_active = TRUE AND last_seen_sync_id = ?\n            ', [sync_run_id, sync_run_id])

def _snapshot_change_count(conn, snapshot_table: str, key_column: str, left_sync_id: str, right_sync_id: str) -> int:
    """Spocte, kolik snapshot entit pribylo mezi dvema behy."""

    return conn.execute(f'\n        SELECT COUNT(*)\n        FROM {snapshot_table} AS cur\n        LEFT JOIN {snapshot_table} AS prev\n          ON prev.sync_run_id = ? AND prev.{key_column} = cur.{key_column}\n        WHERE cur.sync_run_id = ? AND prev.{key_column} IS NULL\n        ', [right_sync_id, left_sync_id]).fetchone()[0]

def _snapshot_change_rows(conn, snapshot_table: str, snapshot_key_column: str, entity_table: str, entity_key_column: str, media_type_column: str, parent_title_column: str, title_column: str, changed_at_column: str, left_sync_id: str, right_sync_id: str, limit: int) -> list[dict[str, Any]]:
    """Vrati konkretni diff radky mezi dvema snapshot behy."""

    rows = conn.execute(f'\n        SELECT\n            ent.{entity_key_column} AS entity_id,\n            ent.{media_type_column} AS media_type,\n            ent.{parent_title_column} AS parent_title,\n            ent.{title_column} AS title,\n            ent.{changed_at_column} AS changed_at\n        FROM {snapshot_table} AS cur\n        LEFT JOIN {snapshot_table} AS prev\n          ON prev.sync_run_id = ? AND prev.{snapshot_key_column} = cur.{snapshot_key_column}\n        JOIN {entity_table} AS ent\n          ON ent.{entity_key_column} = cur.{snapshot_key_column}\n        WHERE cur.sync_run_id = ? AND prev.{snapshot_key_column} IS NULL\n        ORDER BY ent.{changed_at_column} DESC NULLS LAST\n        LIMIT ?\n        ', [right_sync_id, left_sync_id, limit]).fetchall()
    return [{'entity_id': row[0], 'media_type': row[1], 'parent_title': row[2], 'title': row[3], 'changed_at': row[4]} for row in rows]

def _describe_trakt_file(path: Path) -> dict[str, Any]:
    """Vrati metadata a odhad kategorie jednoho Trakt export souboru."""

    payload = _load_json_file(path)
    stat = path.stat()
    return {'name': path.name, 'path': path.as_posix(), 'relative_path': path.name, 'category': _categorize_trakt_file(path.name), 'item_count': len(payload) if isinstance(payload, list) else 1, 'size': stat.st_size, 'mtime': int(stat.st_mtime), 'sha256': _file_sha256(path)}

def _categorize_trakt_file(name: str) -> str:
    """Zaradi Trakt export soubor do logicke kategorie importu."""

    if name.startswith('watched-history-'):
        return 'watched_history'
    if name.startswith('ratings-'):
        return 'ratings'
    if name.startswith('collection-'):
        return 'collection'
    if name == 'lists-watchlist.json':
        return 'watchlist'
    if name == 'lists-lists.json':
        return 'list_metadata'
    if name.startswith('lists-list-'):
        return 'custom_lists'
    if name == 'user-last-activities.json':
        return 'last_activities'
    if name.startswith('watched-shows-') or name.startswith('watched-movies-'):
        return 'watched_summary'
    return 'ignored'

def _categorize_imdb_list_file(name: str) -> str:
    """Zaradi IMDb CSV seznam do podporovane kategorie."""

    if name == 'watchlist.csv':
        return 'watchlist'
    if name == 'favorite_person.csv':
        return 'favorite_people'
    return 'ignored'

def _fingerprint_trakt_files(files: list[dict[str, Any]]) -> str:
    """Spocita fingerprint cele sady Trakt export souboru."""

    digest = hashlib.sha256()
    for item in files:
        digest.update(f"{item['relative_path']}|{item['size']}|{item['mtime']}|{item['sha256']}\n".encode('utf-8'))
    return digest.hexdigest()

def _load_json_file(path: Path) -> Any:
    """Nacte JSON soubor z disku."""

    return json.loads(path.read_text(encoding='utf-8'))

def _file_sha256(path: Path) -> str:
    """Spocita SHA-256 hash souboru."""

    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Nacte CSV soubor do seznamu slovniku."""

    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))

def _count_csv_rows(path: Path) -> int:
    """Vrati pocet radku v CSV souboru bez dalsi logiky."""

    return len(_read_csv_rows(path))

def _parse_trakt_list_id_from_filename(name: str) -> str:
    """Vytahne Trakt list ID z nazvu exportovaneho souboru."""

    match = re.match('lists-list-(\\d+)-', name)
    return match.group(1) if match else 'unknown'

def _extract_trakt_media(item: dict[str, Any]) -> dict[str, Any]:
    """Normalizuje Trakt movie/show/season/episode payload na jeden slovnik."""

    media_type = str(item.get('type') or '')
    if media_type == 'movie':
        media = item.get('movie') or {}
        ids = media.get('ids') or {}
        imdb_id = ids.get('imdb')
        tmdb_id = _safe_int(ids.get('tmdb'))
        return {'media_type': 'movie', 'trakt_id': _safe_int(ids.get('trakt')), 'imdb_id': imdb_id, 'tmdb_id': tmdb_id, 'tconst': imdb_id, 'parent_trakt_id': None, 'parent_title': None, 'title': media.get('title'), 'season_number': None, 'episode_number': None}
    if media_type == 'show':
        media = item.get('show') or {}
        ids = media.get('ids') or {}
        imdb_id = ids.get('imdb')
        tmdb_id = _safe_int(ids.get('tmdb'))
        return {'media_type': 'show', 'trakt_id': _safe_int(ids.get('trakt')), 'imdb_id': imdb_id, 'tmdb_id': tmdb_id, 'tconst': imdb_id, 'parent_trakt_id': None, 'parent_title': None, 'title': media.get('title'), 'season_number': None, 'episode_number': None}
    if media_type == 'season':
        season = item.get('season') or {}
        show = item.get('show') or {}
        season_ids = season.get('ids') or {}
        show_ids = show.get('ids') or {}
        return {'media_type': 'season', 'trakt_id': _safe_int(season_ids.get('trakt')), 'imdb_id': season_ids.get('imdb'), 'tmdb_id': _safe_int(season_ids.get('tmdb')), 'tconst': None, 'parent_trakt_id': _safe_int(show_ids.get('trakt')), 'parent_title': show.get('title'), 'title': f"{show.get('title') or ''} season {season.get('number')}".strip(), 'season_number': _safe_int(season.get('number')), 'episode_number': None}
    if media_type == 'episode':
        episode = item.get('episode') or {}
        show = item.get('show') or {}
        episode_ids = episode.get('ids') or {}
        show_ids = show.get('ids') or {}
        imdb_id = episode_ids.get('imdb')
        return {'media_type': 'episode', 'trakt_id': _safe_int(episode_ids.get('trakt')), 'imdb_id': imdb_id, 'tmdb_id': _safe_int(episode_ids.get('tmdb')), 'tconst': imdb_id, 'parent_trakt_id': _safe_int(show_ids.get('trakt')), 'parent_title': show.get('title'), 'title': episode.get('title'), 'season_number': _safe_int(episode.get('season')), 'episode_number': _safe_int(episode.get('number'))}
    return {'media_type': media_type or 'unknown', 'trakt_id': None, 'imdb_id': None, 'tmdb_id': None, 'tconst': None, 'parent_trakt_id': None, 'parent_title': None, 'title': None, 'season_number': None, 'episode_number': None}

def _build_trakt_media_key(media: dict[str, Any]) -> str:
    """Slozi stabilni identitni klic pro Trakt media payload."""

    trakt_id = media.get('trakt_id')
    imdb_id = media.get('imdb_id')
    tmdb_id = media.get('tmdb_id')
    if trakt_id is not None:
        return f"{media['media_type']}:{trakt_id}"
    if imdb_id:
        return f"{media['media_type']}:{imdb_id}"
    if tmdb_id is not None:
        return f"{media['media_type']}:tmdb:{tmdb_id}"
    if media.get('parent_trakt_id') is not None and media.get('season_number') is not None:
        return f"{media['media_type']}:{media['parent_trakt_id']}:{media['season_number']}:{media.get('episode_number') or 0}"
    return ''

def _sql_path(path: Path) -> str:
    """Vrati absolutni cestu vhodnou pro vlozeni do SQL stringu."""

    return path.resolve().as_posix().replace("'", "''")

def _normalize_match_key(value: Any, *, strip_leading_articles: bool=False) -> str:
    """Normalizuje text pro title/person matching a import lookup."""

    if value is None:
        return ''
    text = str(value).strip().lower()
    if not text:
        return ''
    text = re.sub('\\(\\s*\\d{4}\\s*\\)', ' ', text)
    text = text.replace('&', ' and ')
    text = text.replace('%', ' percent ')
    text = unicodedata.normalize('NFKD', text)
    text = ''.join((ch for ch in text if not unicodedata.combining(ch)))
    text = re.sub('\\bper\\s+cent\\b', ' percent ', text)
    text = re.sub('\\bpct\\b', ' percent ', text)
    text = re.sub('\\bprocent[a-z]*\\b', ' percent ', text)
    text = re.sub('[^a-z0-9]+', ' ', text)
    text = re.sub('\\s+', ' ', text).strip()
    if strip_leading_articles:
        text = re.sub('^(the|a|an)\\s+', '', text).strip()
    return text

def _now_iso() -> str:
    """Vrati aktualni UTC cas jako ISO string."""

    return datetime.now(UTC).isoformat()
