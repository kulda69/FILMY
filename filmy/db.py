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
    key: str
    path: Path

    @property
    def stat_signature(self) -> str:
        stat = self.path.stat()
        return f'{int(stat.st_mtime)}:{stat.st_size}'

    @property
    def stat_mtime(self) -> int:
        return int(self.path.stat().st_mtime)

    @property
    def stat_size(self) -> int:
        return self.path.stat().st_size

    @property
    def sha256(self) -> str:
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
    return ' '.join(str(value or '').strip().split())

def _search_recall_entry_id(entity_type: str, query_text_fold: str, target_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'search-recall|{entity_type}|{query_text_fold}|{target_id}'))

def _prune_search_recall_entries(conn, limit: int) -> None:
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
    stats = fetch_catalog_stats_row_postgres()
    return {**stats, 'database': POSTGRES_DATABASE_NAME, 'assets_path': ASSETS_DIR.as_posix()}

def get_imdb_manifest() -> list[dict[str, Any]]:
    return fetch_imdb_manifest_rows()

def search_catalog(query: str | None, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    rows = fetch_catalog_search_rows(query=query, title_type=title_type, limit=limit)
    items = [_catalog_row_to_dict(row) for row in rows]
    for item in items:
        item['library'] = _fetch_library_summary(None, item['tconst'], item['title_type'])
    return items

def get_content_detail(tconst: str) -> dict[str, Any] | None:
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
    lookup = lookup_title_by_query(query=query, title_type=title_type, candidates_limit=5)
    if lookup is None:
        return None
    source_presentation = get_title_presentation(lookup['selected_tconst'])
    if source_presentation is None:
        return None
    presentation = dict(source_presentation)
    presentation['query'] = query
    presentation['match'] = dict(lookup['selected'])
    return presentation

def describe_person_by_query(query: str) -> dict[str, Any] | None:
    from filmy.db_people import describe_person_by_query as _impl
    return _impl(query)

def _title_candidate_from_presentation(presentation: dict[str, Any], *, fuzzy_score: float | None=None, matched_alias_title: str | None=None) -> dict[str, Any]:
    return {'tconst': presentation['tconst'], 'primary_title': presentation.get('title'), 'original_title': presentation.get('original_title'), 'title_type': presentation.get('title_type'), 'start_year': presentation.get('year'), 'runtime_minutes': presentation.get('runtime_minutes'), 'genres': presentation.get('genres') or [], 'average_rating': presentation.get('imdb_rating'), 'num_votes': presentation.get('imdb_votes'), 'library': presentation.get('library_state') or {}, 'matched_alias_title': matched_alias_title, 'fuzzy_score': fuzzy_score}

def _person_candidate_from_presentation(presentation: dict[str, Any], *, fuzzy_score: float | None=None) -> dict[str, Any]:
    return {'nconst': presentation['nconst'], 'primary_name': presentation.get('name'), 'birth_year': presentation.get('birth_year'), 'death_year': presentation.get('death_year'), 'primary_profession': presentation.get('primary_profession'), 'known_for_titles': presentation.get('known_for_titles'), 'filmography': presentation.get('filmography') or {}, 'credit_count': presentation.get('credit_count') or 0, 'fuzzy_score': fuzzy_score}

def _lookup_title_from_search_recall(query: str, *, title_type: str | None, candidates_limit: int) -> dict[str, Any] | None:
    """Try to satisfy title lookup from the small recent-search recall table first."""
    query_key = _normalize_match_key(query)
    query_text = _normalize_search_query_text(query)
    if not query_key or not query_text:
        return None
    match = fetch_search_recall_match(entity_type='title', query_key=query_key, query_text_fold=query_text.casefold())
    row = None if match is None else (match[0], match[1], None)
    if row is None:
        return None
    title_row = fetch_catalog_title_row(str(row[0]))
    if title_row is None:
        return None
    if title_type is not None and str(title_row[1]) != str(title_type):
        return None
    candidate = _catalog_row_to_dict(title_row)
    candidate['fuzzy_score'] = row[1]
    candidate['matched_alias_title'] = None
    if not _is_safe_recalled_title(query, title_type=title_type, recalled=candidate, candidates_limit=candidates_limit):
        return None
    result = _build_title_lookup_result(query=query, title_type=title_type, selected=candidate, candidates=[candidate], candidates_limit=candidates_limit)
    if result is not None:
        _record_search_recall_entry(entity_type='title', query=query, target_id=str(candidate['tconst']), target_label=str(candidate.get('primary_title') or ''), target_title_type=str(candidate.get('title_type') or ''), matched_alias_title=candidate.get('matched_alias_title'), fuzzy_score=candidate.get('fuzzy_score'))
    return result

def _remember_title_lookup(query: str, selected: dict[str, Any]) -> None:
    if not _is_confident_lookup(query, selected) and (not _is_direct_enough_lookup(query, selected)):
        return
    _record_search_recall_entry(entity_type='title', query=query, target_id=str(selected['tconst']), target_label=str(selected.get('primary_title') or ''), target_title_type=str(selected.get('title_type') or ''), matched_alias_title=selected.get('matched_alias_title'), fuzzy_score=selected.get('fuzzy_score'))

def lookup_title_by_query(query: str, title_type: str | None=None, candidates_limit: int=5, allow_expensive_fallback: bool=False) -> dict[str, Any] | None:
    started_at = time.perf_counter()
    recalled = _lookup_title_from_search_recall(query, title_type=title_type, candidates_limit=candidates_limit)
    if recalled is not None:
        logger.info('lookup_title_by_query query=%r mode=recall candidates=%s elapsed_ms=%.1f', query, recalled.get('candidate_count'), (time.perf_counter() - started_at) * 1000)
        return recalled
    query_key = _normalize_match_key(query)
    query_tokens = _match_tokens(query_key)
    candidates = _search_catalog_for_lookup(query=query, title_type=title_type, limit=max(candidates_limit, 1) * 5)
    if candidates or len(query_tokens) != 1 or len(query_key) < 5:
        alias_candidates = _search_catalog_aliases_for_lookup(query=query, title_type=title_type, limit=max(candidates_limit, 1) * 5)
        candidates = _merge_lookup_candidates(candidates, alias_candidates)
    if candidates:
        _attach_library_summaries_to_exact_title_candidates(query, candidates)
        direct_selected = _pick_best_title_match(query, candidates)
        if _is_direct_enough_lookup(query, direct_selected):
            result = _build_title_lookup_result(query=query, title_type=title_type, selected=direct_selected, candidates=candidates, candidates_limit=candidates_limit)
            if result is not None:
                _remember_title_lookup(query, direct_selected)
                logger.info('lookup_title_by_query query=%r mode=direct candidates=%s elapsed_ms=%.1f', query, result.get('candidate_count'), (time.perf_counter() - started_at) * 1000)
            return result
    should_expand = not candidates or _should_expand_to_fuzzy(query, candidates)
    if should_expand:
        fuzzy_candidates = _search_catalog_for_lookup_fuzzy(query=query, title_type=title_type, limit=max(candidates_limit, 1) * 5)
        candidates = _merge_lookup_candidates(candidates, fuzzy_candidates)
        alias_fuzzy_candidates = _search_catalog_aliases_for_lookup_fuzzy(query=query, title_type=title_type, limit=max(candidates_limit, 1) * 5)
        candidates = _merge_lookup_candidates(candidates, alias_fuzzy_candidates)
        _attach_library_summaries_to_exact_title_candidates(query, candidates)
    if not candidates:
        logger.info('lookup_title_by_query query=%r mode=miss elapsed_ms=%.1f', query, (time.perf_counter() - started_at) * 1000)
        return None
    selected = _pick_best_title_match(query, candidates)
    if allow_expensive_fallback and len(query_tokens) > 1 and (not _is_confident_lookup(query, selected)):
        wide_candidates = _search_catalog_for_lookup_levenshtein(query=query, title_type=title_type, limit=max(candidates_limit, 1) * 5)
        candidates = _merge_lookup_candidates(candidates, wide_candidates)
        alias_wide_candidates = _search_catalog_aliases_for_lookup_levenshtein(query=query, title_type=title_type, limit=max(candidates_limit, 1) * 5)
        candidates = _merge_lookup_candidates(candidates, alias_wide_candidates)
        if not candidates:
            logger.info('lookup_title_by_query query=%r mode=wide-miss elapsed_ms=%.1f', query, (time.perf_counter() - started_at) * 1000)
            return None
        selected = _pick_best_title_match(query, candidates)
    else:
        selected = _pick_best_title_match(query, candidates)
    result = _build_title_lookup_result(query=query, title_type=title_type, selected=selected, candidates=candidates, candidates_limit=candidates_limit)
    if result is not None:
        _remember_title_lookup(query, selected)
        logger.info('lookup_title_by_query query=%r mode=%s candidates=%s elapsed_ms=%.1f', query, 'wide' if allow_expensive_fallback and len(query_tokens) > 1 and (not _is_confident_lookup(query, selected)) else 'fuzzy', result.get('candidate_count'), (time.perf_counter() - started_at) * 1000)
    return result

def lookup_person_by_query(query: str, candidates_limit: int=5) -> dict[str, Any] | None:
    from filmy.db_people import lookup_person_by_query as _impl
    return _impl(query, candidates_limit=candidates_limit)

def get_person_presentation(nconst: str) -> dict[str, Any] | None:
    from filmy.db_people import get_person_presentation as _impl
    return _impl(nconst)

def get_person_portrait_summary(nconst: str) -> dict[str, Any]:
    """Return only portrait-related person data without building full person presentation."""
    return {'portrait_url': _person_portrait_url(nconst), 'has_portrait': _person_portrait_path(nconst) is not None}

def _fetch_known_for_items(conn, known_for_titles: str | None) -> list[dict[str, Any]]:
    if not known_for_titles:
        return []
    ordered_tconsts = [item.strip() for item in str(known_for_titles).split(',') if item.strip()]
    if not ordered_tconsts:
        return []
    rows = fetch_known_for_title_rows(ordered_tconsts)
    items_by_tconst = {row[0]: {'tconst': row[0], 'title': row[1], 'start_year': row[2]} for row in rows}
    return [items_by_tconst[tconst] for tconst in ordered_tconsts if tconst in items_by_tconst]

def render_person_presentation(presentation: dict[str, Any]) -> str:
    from filmy.db_people import render_person_presentation as _impl
    return _impl(presentation)

@lru_cache(maxsize=256)
def _get_title_presentation_cached(tconst: str) -> dict[str, Any] | None:
    if not tconst:
        return None
    cached = _load_cached_title_presentation(None, tconst)
    if cached is not None:
        return cached
    detail = get_content_detail(tconst)
    if detail is None:
        return None
    series_title = detail.get('series_title')
    if detail.get('kind') == 'episode' and detail.get('series_tconst') and (series_title is None):
        series_title = fetch_catalog_primary_title(str(detail['series_tconst']))
    people = _fetch_title_people(None, tconst)
    cache_fingerprint = _title_cache_source_fingerprint(None, tconst)
    tmdb_payload = detail.get('tmdb') or {}
    tmdb_details = tmdb_payload.get('details') or {}
    overview = tmdb_details.get('overview')
    providers = [provider['provider_name'] for provider in tmdb_payload.get('providers') or [] if provider.get('provider_name')]
    unique_providers = list(dict.fromkeys(providers))
    presentation = {'tconst': detail['tconst'], 'title': detail.get('primary_title'), 'original_title': detail.get('original_title'), 'title_type': detail.get('title_type'), 'kind': detail.get('kind'), 'kind_label': _title_type_label(detail.get('title_type')), 'year': detail.get('start_year'), 'end_year': detail.get('end_year'), 'runtime_minutes': detail.get('runtime_minutes'), 'genres': detail.get('genres') or [], 'imdb_rating': detail.get('average_rating'), 'imdb_votes': detail.get('num_votes'), 'overview': overview, 'tmdb_details': tmdb_details, 'tmdb_providers': tmdb_payload.get('providers') or [], 'directed_by': people['directors'], 'written_by': people['writers'], 'created_by': people['creators'], 'main_cast': people['cast'], 'available_in_czechia': unique_providers, 'library_state': detail.get('library') or {}, 'content_state': detail.get('content_state') or {}, 'episodes': detail.get('episodes') or [], 'aliases': detail.get('aliases') or [], 'tmdb_locales': (detail.get('tmdb') or {}).get('detail_locales') or [], 'poster_url': _poster_url_from_detail(detail), 'backdrop_url': _backdrop_url_from_detail(detail), 'series_tconst': detail.get('series_tconst'), 'series_title': series_title, 'season_number': detail.get('season_number'), 'episode_number': detail.get('episode_number'), 'has_poster': any((asset.get('asset_kind') == 'poster' for asset in tmdb_payload.get('assets') or [])), 'has_backdrop': any((asset.get('asset_kind') == 'backdrop' for asset in tmdb_payload.get('assets') or []))}
    presentation['display_text'] = render_title_presentation(presentation)
    _store_cached_title_presentation(tconst, presentation, cache_fingerprint)
    return presentation

def get_title_presentation(tconst: str) -> dict[str, Any] | None:
    return _get_title_presentation_cached(tconst)

def get_title_people_panel(tconst: str) -> dict[str, Any] | None:
    """Return only title credits needed by the detail people panel.

    This avoids rebuilding the whole title presentation for lightweight partial
    refreshes such as `/titles/{tconst}/main-cast`.
    """
    exists = fetch_catalog_title_row(tconst) is not None or fetch_catalog_episode_row(tconst) is not None
    if not exists:
        return None
    people = _fetch_title_people(None, tconst)
    return {'tconst': tconst, 'directed_by': people['directors'], 'written_by': people['writers'], 'created_by': people['creators'], 'main_cast': people['cast']}

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
    lines: list[str] = []
    lines.append(str(presentation['title']))
    meta_bits = [presentation['kind_label']]
    if presentation.get('year') is not None:
        meta_bits.append(str(presentation['year']))
    lines.append(', '.join(meta_bits))
    genres = presentation.get('genres') or []
    if genres:
        lines.append(' / '.join(genres))
    rating = presentation.get('imdb_rating')
    if rating is not None:
        votes = presentation.get('imdb_votes')
        vote_suffix = f' ({votes} votes)' if votes is not None else ''
        lines.append(f'IMDb: {rating}/10{vote_suffix}')
    overview = presentation.get('overview')
    if overview:
        lines.append('')
        lines.append("What it's about")
        lines.append(str(overview))
    if presentation.get('created_by'):
        lines.append('')
        lines.append('Created by')
        lines.append(', '.join((person['name'] for person in presentation['created_by'])))
    if presentation.get('directed_by'):
        lines.append('')
        lines.append('Directed by')
        lines.append(', '.join((person['name'] for person in presentation['directed_by'])))
    if presentation.get('written_by'):
        lines.append('')
        lines.append('Written by')
        lines.append(', '.join((person['name'] for person in presentation['written_by'])))
    if presentation.get('main_cast'):
        lines.append('')
        lines.append('Main cast')
        for person in presentation['main_cast']:
            role = f" as {person['character']}" if person.get('character') else ''
            lines.append(f"{person['name']}{role}")
    if presentation.get('available_in_czechia'):
        lines.append('')
        lines.append('Available in Czechia')
        for provider in presentation['available_in_czechia']:
            lines.append(provider)
    library = presentation.get('library_state') or {}
    if library:
        lines.append('')
        lines.append('Your local library state')
        if library.get('watched_count') is not None:
            times = 'time' if library['watched_count'] == 1 else 'times'
            lines.append(f"Watched {library['watched_count']} {times}")
        if library.get('last_watched_at') is not None:
            lines.append(f"Last watched: {library['last_watched_at']}")
        if library.get('in_watchlist'):
            lines.append('In watchlist')
        if library.get('rating') is not None:
            lines.append(f"Your rating: {library['rating']}/10")
    if presentation.get('episodes'):
        lines.append('')
        lines.append('Episodes')
        lines.append(f"{len(presentation['episodes'])} episodes loaded")
    local_bits: list[str] = []
    if presentation.get('tmdb_locales'):
        local_bits.append('TMDB detail: ' + ', '.join(presentation['tmdb_locales']))
    asset_bits: list[str] = []
    if presentation.get('has_poster'):
        asset_bits.append('poster')
    if presentation.get('has_backdrop'):
        asset_bits.append('backdrop')
    if asset_bits:
        local_bits.append('assets: ' + ', '.join(asset_bits))
    if local_bits:
        lines.append('')
        lines.append('Available locally')
        lines.extend(local_bits)
    return '\n'.join(lines)

def _title_detail_cache_path(tconst: str) -> Path:
    if not tconst:
        raise ValueError('tconst is required')
    return ASSETS_DIR / tconst / 'detail.json'

def _person_detail_cache_path(nconst: str) -> Path:
    if not nconst:
        raise ValueError('nconst is required')
    return PEOPLE_ASSETS_DIR / nconst / 'detail.json'

def _person_portrait_path(nconst: str) -> Path | None:
    if not nconst:
        return None
    person_dir = PEOPLE_ASSETS_DIR / nconst
    for suffix in ('jpg', 'jpeg', 'webp', 'png'):
        candidate = person_dir / f'portrait.{suffix}'
        if candidate.exists():
            return candidate
    return None

def _person_portrait_url(nconst: str) -> str | None:
    portrait_path = _person_portrait_path(nconst)
    if portrait_path is None:
        return None
    return _asset_url_from_local_path(portrait_path.as_posix(), assets_root=PEOPLE_ASSETS_DIR, mount_path='/assets/people')

def _person_biography_path(nconst: str) -> Path:
    return PEOPLE_ASSETS_DIR / nconst / 'biography.json'

def _person_biography_meta(nconst: str) -> dict[str, Any] | None:
    path = _person_biography_path(nconst)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None

def _person_biography_payload(nconst: str) -> dict[str, Any] | None:
    meta = _person_biography_meta(nconst)
    if not meta or str(meta.get('status') or '') != 'fetched':
        return None
    biography = str(meta.get('biography') or '').strip()
    if not biography:
        return None
    return {'text': biography, 'locale': meta.get('locale'), 'tmdb_person_id': meta.get('tmdb_person_id'), 'updated_at': meta.get('updated_at')}

def _title_detail_cache_status(tconst: str, source_fingerprint: str) -> str:
    cache_path = _title_detail_cache_path(tconst)
    if not cache_path.exists():
        return 'missing'
    try:
        cached = json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return 'invalid'
    if not isinstance(cached, dict):
        return 'invalid'
    if cached.get('tconst') != tconst:
        return 'stale'
    if cached.get('cache_version') != TITLE_PRESENTATION_CACHE_VERSION:
        return 'stale'
    if cached.get('source_fingerprint') != source_fingerprint:
        return 'stale'
    return 'ready'

def _person_detail_cache_status(nconst: str, source_fingerprint: str) -> str:
    cache_path = _person_detail_cache_path(nconst)
    if not cache_path.exists():
        return 'missing'
    try:
        cached = json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return 'invalid'
    if not isinstance(cached, dict):
        return 'invalid'
    if cached.get('nconst') != nconst:
        return 'stale'
    if cached.get('source_fingerprint') != source_fingerprint:
        return 'stale'
    return 'ready'

def _get_person_affinity_rating(conn, nconst: str) -> int:
    return fetch_person_affinity_rating_postgres(nconst)

def _load_cached_title_presentation(conn, tconst: str) -> dict[str, Any] | None:
    cache_path = _title_detail_cache_path(tconst)
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get('tconst') != tconst:
        return None
    expected_fingerprint = _title_cache_source_fingerprint(conn, tconst)
    if expected_fingerprint is None:
        return None
    if _title_detail_cache_status(tconst, expected_fingerprint) != 'ready':
        return None
    if cached.get('kind') not in {'title', 'episode'}:
        return None
    if cached.get('cache_version') != TITLE_PRESENTATION_CACHE_VERSION:
        return None
    if 'has_poster' not in cached or 'has_backdrop' not in cached:
        return None
    if cached.get('has_poster') and (not cached.get('poster_url')):
        return None
    return cached

def _load_cached_person_presentation(conn, nconst: str) -> dict[str, Any] | None:
    cache_path = _person_detail_cache_path(nconst)
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get('nconst') != nconst:
        return None
    expected_fingerprint = _person_cache_source_fingerprint(conn, nconst)
    if expected_fingerprint is None:
        return None
    if _person_detail_cache_status(nconst, expected_fingerprint) != 'ready':
        return None
    return cached

def _store_cached_title_presentation(tconst: str, presentation: dict[str, Any], source_fingerprint: str | None) -> None:
    if not source_fingerprint:
        return
    cache_path = _title_detail_cache_path(tconst)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _jsonify_for_cache({**presentation, 'cache_version': TITLE_PRESENTATION_CACHE_VERSION, 'source_fingerprint': source_fingerprint, 'cached_at': _now_iso()})
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

def _store_cached_person_presentation(nconst: str, presentation: dict[str, Any], source_fingerprint: str | None) -> None:
    if not source_fingerprint:
        return
    cache_path = _person_detail_cache_path(nconst)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _jsonify_for_cache({**presentation, 'source_fingerprint': source_fingerprint, 'cached_at': _now_iso()})
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

def _title_cache_source_fingerprint(conn, tconst: str, detail: dict[str, Any] | None=None) -> str | None:
    refresh_fingerprint = fetch_catalog_refresh_fingerprint()
    if refresh_fingerprint is None:
        return None
    if detail is None:
        detail = _fetch_title_cache_source_detail(conn, tconst)
    if detail is None:
        return None
    payload = {'refresh': refresh_fingerprint, 'detail': {'tconst': detail.get('tconst'), 'title_type': detail.get('title_type') or detail.get('kind'), 'title': detail.get('primary_title'), 'original_title': detail.get('original_title'), 'start_year': detail.get('start_year'), 'end_year': detail.get('end_year'), 'runtime_minutes': detail.get('runtime_minutes'), 'genres': detail.get('genres') or [], 'tmdb_locales': (detail.get('tmdb') or {}).get('detail_locales') or [], 'tmdb_assets': _tmdb_asset_summary_signature(detail.get('tmdb') or {}), 'library': detail.get('library') or {}, 'content_state': detail.get('content_state') or {}, 'aliases': detail.get('aliases') or []}}
    digest = hashlib.sha256(json.dumps(_jsonify_for_cache(payload), sort_keys=True, ensure_ascii=False).encode('utf-8'))
    return digest.hexdigest()

def _person_cache_source_fingerprint(conn, nconst: str, presentation: dict[str, Any] | None=None) -> str | None:
    refresh_fingerprint = fetch_catalog_refresh_fingerprint()
    if refresh_fingerprint is None:
        return None
    if presentation is None:
        presentation = _fetch_person_cache_source_detail(conn, nconst)
        if presentation is None:
            return None
    payload = {'refresh': refresh_fingerprint, 'person': {'nconst': presentation.get('nconst'), 'name': presentation.get('name'), 'birth_year': presentation.get('birth_year'), 'death_year': presentation.get('death_year'), 'primary_profession': presentation.get('primary_profession'), 'known_for_titles': presentation.get('known_for_titles'), 'filmography': presentation.get('filmography') or {}, 'credit_count': presentation.get('credit_count'), 'portrait_url': presentation.get('portrait_url'), 'has_portrait': presentation.get('has_portrait'), 'affinity_rating': presentation.get('affinity_rating') or 0, 'biography': presentation.get('biography') or None}}
    digest = hashlib.sha256(json.dumps(_jsonify_for_cache(payload), sort_keys=True, ensure_ascii=False).encode('utf-8'))
    return digest.hexdigest()

def _fetch_person_cache_source_detail(conn, nconst: str) -> dict[str, Any] | None:
    person = fetch_person_catalog_row(nconst)
    if person is None:
        return None
    credits = fetch_person_credit_rows(nconst, limit=500)
    affinity_rating = _get_person_affinity_rating(conn, person[0])
    filmography: dict[str, list[dict[str, Any]]] = {'directed': [], 'written': [], 'created': [], 'acted': [], 'other': []}
    credit_count = 0
    seen_titles: set[str] = set()
    for row in credits:
        credit_count += 1
        tconst = row[5]
        if tconst in seen_titles:
            continue
        seen_titles.add(tconst)
        entry = {'tconst': tconst, 'title': row[6], 'original_title': row[7], 'start_year': row[8], 'title_type': row[9], 'credit_group': row[0], 'category': row[1], 'job': row[2], 'character': _principal_character(row[3])}
        if row[0] == 'director':
            filmography['directed'].append(entry)
        elif row[0] == 'creator':
            filmography['created'].append(entry)
        elif row[0] == 'writer':
            filmography['written'].append(entry)
        elif row[0] == 'cast':
            filmography['acted'].append(entry)
        else:
            filmography['other'].append(entry)
    if affinity_rating > 0:
        for episode_series_entry in _fetch_person_episode_series_credits(conn, nconst, existing_tconsts=seen_titles):
            filmography['acted'].append(episode_series_entry)
            seen_titles.add(str(episode_series_entry['tconst']))
        filmography['acted'].sort(key=lambda item: (item.get('start_year') or 0, item.get('title') or ''), reverse=True)
    presentation = {'nconst': person[0], 'name': person[1], 'birth_year': person[2], 'death_year': person[3], 'primary_profession': person[4], 'known_for_titles': person[5], 'known_for_items': _fetch_known_for_items(conn, person[5]), 'filmography': filmography, 'credit_count': credit_count, 'portrait_url': _person_portrait_url(person[0]), 'has_portrait': _person_portrait_path(person[0]) is not None, 'affinity_rating': affinity_rating, 'biography': _person_biography_payload(person[0])}
    presentation['display_text'] = render_person_presentation(presentation)
    return presentation

def _fetch_person_episode_series_credits(conn, nconst: str, *, existing_tconsts: set[str] | None=None) -> list[dict[str, Any]]:
    """Aggregate episode-only acting credits to their parent series.

    This is intentionally reserved for affinity-rated people. The common person
    detail path keeps using the faster `app.title_credits` materialization,
    while rated people get a richer filmography that can surface TV series
    where the actor appears only on episode rows in IMDb data.
    """
    try:
        rows = fetch_person_episode_series_credit_rows(nconst, limit=200)
    except Exception:
        return []
    blocked = existing_tconsts or set()
    items: list[dict[str, Any]] = []
    for row in rows:
        series_tconst = str(row[0])
        if series_tconst in blocked:
            continue
        episode_count = int(row[5] or 0)
        if episode_count <= 0:
            continue
        items.append({'tconst': series_tconst, 'title': row[1], 'original_title': row[2], 'start_year': row[3], 'title_type': row[4], 'credit_group': 'cast', 'category': 'actor', 'job': f'{episode_count} episodes', 'character': None})
    return items

def _fetch_title_cache_source_detail(conn, tconst: str) -> dict[str, Any] | None:
    title = fetch_catalog_title_row(tconst)
    if title is not None:
        title_detail: dict[str, Any] = {'tconst': title[0], 'title_type': title[1], 'primary_title': title[2], 'original_title': title[3], 'start_year': title[4], 'end_year': title[5], 'runtime_minutes': title[6], 'genres': title[7] or []}
    else:
        episode = fetch_catalog_episode_row(tconst)
        if episode is None:
            return None
        title_detail = {'tconst': episode[0], 'kind': 'episode', 'primary_title': episode[4] if True else episode[4], 'original_title': episode[5] if True else episode[5], 'start_year': episode[6] if True else episode[6], 'runtime_minutes': episode[7] if True else episode[7]}
    title_detail['aliases'] = _fetch_aliases(conn, tconst)
    title_detail['content_state'] = _fetch_content_state(conn, tconst)
    if title is not None:
        title_detail['library'] = _fetch_library_summary(conn, tconst, title[1])
    else:
        title_detail['library'] = _fetch_library_summary(conn, tconst, 'tvEpisode')
    tmdb = _fetch_tmdb(conn, tconst)
    title_detail['tmdb'] = tmdb
    return title_detail

def _tmdb_asset_summary_signature(tmdb: dict[str, Any]) -> list[dict[str, Any]]:
    assets = tmdb.get('assets') or []
    return [{'kind': asset.get('asset_kind'), 'path': asset.get('local_path') or asset.get('relative_path'), 'status': asset.get('status'), 'sha256': asset.get('sha256')} for asset in assets]

def _tmdb_detail_is_cache_ready(tmdb: dict[str, Any] | None) -> bool:
    if not tmdb:
        return False
    locales = set(tmdb.get('detail_locales') or [])
    if 'en-US' not in locales or 'cs-CZ' not in locales:
        return False
    details = tmdb.get('details') or {}
    fetched_assets = {asset.get('asset_kind') for asset in tmdb.get('assets') or [] if asset.get('status') == 'fetched'}
    if details.get('poster_path') and 'poster' not in fetched_assets:
        return False
    if details.get('backdrop_path') and 'backdrop' not in fetched_assets:
        return False
    return True

def _jsonify_for_cache(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonify_for_cache(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonify_for_cache(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonify_for_cache(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, set):
        return sorted((_jsonify_for_cache(item) for item in value))
    return value

def update_content_state(tconst: str, interest_state: str) -> dict[str, Any]:
    from filmy.db_library import update_content_state as _impl
    return _impl(tconst, interest_state)

def set_watchlist_state(tconst: str, *, in_watchlist: bool, notes: str | None=None) -> dict[str, Any]:
    from filmy.db_library import set_watchlist_state as _impl
    return _impl(tconst, in_watchlist=in_watchlist, notes=notes)

def add_title_to_user_list(tconst: str, list_id: str, *, notes: str | None=None) -> dict[str, Any]:
    from filmy.db_library import add_title_to_user_list as _impl
    return _impl(tconst, list_id, notes=notes)

def set_user_rating(tconst: str, rating: int, *, liked_notes: str | None=None, disliked_notes: str | None=None) -> dict[str, Any]:
    from filmy.db_library import set_user_rating as _impl
    return _impl(tconst, rating, liked_notes=liked_notes, disliked_notes=disliked_notes)

def set_person_affinity_rating(nconst: str, rating: int) -> dict[str, Any]:
    from filmy.db_library import set_person_affinity_rating as _impl
    return _impl(nconst, rating)

def clear_user_rating(tconst: str) -> dict[str, Any]:
    from filmy.db_library import clear_user_rating as _impl
    return _impl(tconst)

def get_ai_taste_seed(source_list: str='kouknout-znovu', limit: int=50) -> dict[str, Any]:
    """Return read-only taste examples for an external AI recommendation layer."""
    safe_limit = max(1, min(int(limit), 200))
    return fetch_ai_taste_seed_rows(source_list=source_list, limit=safe_limit)

def get_ai_taste_inputs(limit_per_list: int=25) -> dict[str, Any]:
    """Return AI taste inputs grouped by user-list AI role."""
    safe_limit = max(1, min(int(limit_per_list), 100))
    included_roles = ('strong_positive', 'interested_owned', 'interested_planned', 'in_progress', 'negative')
    excluded_roles = ('external_suggestion', 'ignore')
    role_labels = {'strong_positive': 'Silne pozitivni priklady.', 'interested_owned': 'Tituly, ktere Jiri ma nebo s nimi udelal rucni praci.', 'interested_planned': 'Tituly, ktere chce videt nebo stoji za pozornost.', 'in_progress': 'Rozkoukane tituly; opatrny signal zajmu.', 'negative': 'Negativni priklady nebo veci, ktere nemaji podporovat podobne pozitivni tipy.', 'external_suggestion': 'Vystup z AI; nepouzivat jako vstup.', 'ignore': 'Neutralni nebo technicke seznamy; nepouzivat jako vstup.'}
    visible_lists = get_local_library_status().get('visible_lists') or []
    grouped: dict[str, list[dict[str, Any]]] = {role: [] for role in included_roles}
    excluded_sources: list[dict[str, Any]] = []
    for source_list in visible_lists:
        role = str(source_list.get('ai_input_role') or 'ignore')
        source_summary = {'id': source_list.get('id'), 'slug': source_list.get('slug'), 'name': source_list.get('name'), 'description': source_list.get('description'), 'list_kind': source_list.get('list_kind'), 'ai_input_role': role, 'item_count': source_list.get('item_count')}
        if role in excluded_roles:
            excluded_sources.append(source_summary)
            continue
        if role not in grouped:
            excluded_sources.append(source_summary)
            continue
        seed = get_ai_taste_seed(source_list=str(source_list['id']), limit=safe_limit)
        grouped[role].append({'source_list': seed.get('source_list') or source_summary, 'role_description': role_labels[role], 'limit': seed.get('limit'), 'items': seed.get('items') or []})
    return {'contract_version': 1, 'limit_per_list': safe_limit, 'included_roles': list(included_roles), 'excluded_roles': list(excluded_roles), 'role_descriptions': role_labels, 'groups': grouped, 'excluded_sources': excluded_sources, 'usage_notes': ['`external_suggestion` a `ignore` se nikdy neposilaji jako vstup pro AI tipy.', '`negative` je vstup pro varovani a vymezovani vkusu, ne pozitivni seed.', 'Polozky ve skupinach maji stejny tvar jako `/api/ai/taste-seed` vcetne people affinity a title role signals.']}

def get_ai_rated_titles(*, min_user_rating: int=8, limit: int=50, title_type: str | None=None) -> dict[str, Any]:
    """Return locally rated titles for an external AI recommendation layer."""
    safe_rating = max(1, min(int(min_user_rating), 10))
    safe_limit = max(1, min(int(limit), 200))
    cleaned_title_type = (title_type or '').strip() or None
    return fetch_ai_rated_title_rows(min_user_rating=safe_rating, limit=safe_limit, title_type=cleaned_title_type)

def get_ai_noted_titles(*, notes: str='any', min_user_rating: int | None=None, limit: int=50) -> dict[str, Any]:
    """Return titles with written local notes for an external AI layer."""
    cleaned_notes = (notes or 'any').strip().lower()
    if cleaned_notes not in {'any', 'liked', 'disliked'}:
        cleaned_notes = 'any'
    safe_min_rating = None
    if min_user_rating is not None:
        safe_min_rating = max(1, min(int(min_user_rating), 10))
    safe_limit = max(1, min(int(limit), 200))
    return fetch_ai_noted_title_rows(notes=cleaned_notes, min_user_rating=safe_min_rating, limit=safe_limit)

def get_ai_watched_titles(*, include_rated: bool=True, include_negative: bool=True) -> dict[str, Any]:
    """Return a complete hard exclusion list for external AI recommendations."""
    return fetch_ai_watched_title_rows(include_rated=include_rated, include_negative=include_negative)

def import_ai_recommendations_file(path: str | Path) -> dict[str, Any]:
    from filmy.ai_recommendations import import_ai_recommendations_file as _impl
    return _impl(path)

def list_ai_recommendation_files() -> list[dict[str, Any]]:
    from filmy.ai_recommendations import list_ai_recommendation_files as _impl
    return _impl()

def delete_ai_recommendation_file(filename: str) -> dict[str, Any]:
    from filmy.ai_recommendations import delete_ai_recommendation_file as _impl
    return _impl(filename)

def get_latest_ai_recommendation_for_title(tconst: str) -> dict[str, Any] | None:
    return fetch_latest_ai_recommendation_for_title_postgres(tconst)

def get_ai_context() -> dict[str, Any]:
    """Return stable local taste context for an external AI recommendation layer."""
    return {'contract_version': 1, 'rating_scales': {'user_rating': {'min': 1, 'max': 10, 'type': 'integer', 'description': 'Jiriho lokalni hodnoceni titulu; vyssi cislo znamena silnejsi oblibu.'}, 'person_affinity_rating': {'min': 0, 'max': 10, 'type': 'integer', 'description': 'Jiriho oblibenost osoby; 0 znamena bez pozitivni affinity.'}, 'title_role_signal_strength': {'min': 0, 'max': 10, 'type': 'integer', 'description': 'Sila konkretniho signalu role/postavy v jednom titulu; neni to celkovy rating titulu ani affinity k herci.'}, 'imdb_rating': {'min': 0, 'max': 10, 'type': 'decimal', 'description': 'Externi IMDb rating; neni to Jiriho lokalni hodnoceni.'}, 'favorite_preference_rank': {'min': 1, 'max': 10, 'type': 'integer', 'description': 'Rucni priorita favorite genres/traits; nizsi cislo znamena silnejsi preferenci, null znamena nehodnoceno.'}}, 'favorite_genres': get_favorite_genres(active_only=False), 'favorite_traits': get_favorite_traits(active_only=False), 'score_signal_notes': {'genre_score_signals': 'Lokalni preference zanru podle historie, ratingu a dalsich signalu.', 'actor_affinity_rating': 'Souhrnny signal oblibenosti hodnocenych hercu navazanych na titul.', 'people_affinity': 'Konkretni osoby z titulu, ktere maji rucni affinity rating; kontrakt pro navazujici rozsireni taste-seed.', 'title_role_signals': 'Konkretni role/postavy v titulu, ktere Jiri oznacil jako pozitivni, negativni nebo smisene signaly. Tento signal je oddeleny od ratingu titulu i affinity k herci.'}, 'title_role_signal_definitions': {'signal_types': {'character': 'Postava jako celek.', 'dialogue': 'Dialogy, hlas, slovni projev nebo zpusob komunikace postavy.', 'behavior': 'Chovani, rozhodovani a reakce postavy.', 'relationship_dynamic': 'Vztahova dynamika postavy s ostatnimi.', 'performance': 'Herecke provedeni v konkretni roli.', 'visual_appeal': 'Vzhled, styl nebo vizualni pusobeni role v danem titulu.', 'attraction': 'Pritazlivost nebo charisma role v danem titulu a dobe.', 'other': 'Jiny titulove vazany signal role/postavy.'}, 'polarities': {'positive': 'Signal, ktery muze podporit podobna doporuceni.', 'negative': 'Signal, ktery muze podobna doporuceni oslabit nebo vyloucit.', 'mixed': 'Signal je dulezity, ale neni jednoznacne pozitivni ani negativni.'}, 'notes': 'Poznamka je textovy kontext pro cloveka a pozdeji AI interpretaci; nema se sama prevadet na ciselne skore bez opatrnosti.'}, 'usage_notes': ['Endpoint je read-only a nevola externi AI ani online katalogy.', 'Navazujici AI projekt ho ma volat jako obecny kontext pred praci s konkretnimi tituly.', 'Favorite genres a favorite traits se vraci cele, vcetne neaktivnich polozek.', 'Title role signals mohou byt silne i u titulu s nizkym celkovym hodnocenim; napr. nebrat cely serial jako oblibeny, ale brat konkretni postavu/dialogy/chovani jako vzor.']}

def get_ai_scoring_explainer() -> dict[str, Any]:
    """Explain local scoring semantics for an external AI recommendation layer."""
    return {'contract_version': 1, 'score_scope': 'default', 'status': {'implemented_scoring': 'Aktualni lokalni scoring pocita hlavne zanrove a titulove signaly z historie, lokalnich ratingu, watch signalu, people affinity a rucnich favorite genres.', 'role_signals_status': 'Title role signals jsou nova samostatna vrstva. Zatim se nepositaji do genre_score_signals ani final_score.', 'future_role_signal_task': 'Pozdeji navrhnout samostatnou scoring vetev pro role/postava signaly, napr. role_signal_score nebo character_preference_signals. Nezvedat tim automaticky celkove hodnoceni titulu.'}, 'principles': ['Lokalni score je pomocny signal pro razeni a vysvetleni, ne definitivni pravda.', 'Jiriho lokalni rating ma vyssi vyznam nez externi IMDb rating.', 'IMDb rating je verejny externi signal kvality/popularity, ne osobni preference.', 'People affinity je osobni signal k osobe, ne obecna popularita herce.', 'Title role signals mohou byt silne i u titulu s nizkym celkovym ratingem; cist je samostatne.', 'Negativni seznamy a negativni signaly maji pomahat vymezit vkus, ne mechanicky mazat vsechny podobne tituly.'], 'signals': {'final_score': {'meaning': 'Normalizovane lokalni skore kandidata nebo zanru v danem score scope.', 'ai_usage': 'Pouzit jako podpurny signal razeni, ne jako jediny duvod doporuceni.'}, 'watch_signal_score': {'meaning': 'Signal odvozeny z historie sledovani a opakovanych lokalnich interakci.', 'ai_usage': 'Ukazuje, ze Jiri s podobnym obsahem realne travil cas.'}, 'rating_signal_score': {'meaning': 'Signal odvozeny z Jiriho lokalnich ratingu.', 'ai_usage': 'Silnejsi osobni signal nez IMDb rating; stale ho cist spolecne se slovnimi poznamkami.'}, 'actor_affinity_score': {'meaning': 'Signal odvozeny z oblibenosti osob navazanych na titul.', 'ai_usage': 'Pouzit opatrne: osoba neni totéz jako role v konkretnim titulu.'}, 'genre_score_signals': {'meaning': 'Zanrove signaly, ktere ukazuji, proc se nejaky zanr nebo titul muze potkavat s lokalnim vkusem.', 'ai_usage': 'Pouzit jako kontext k zanrum, ne jako samostatne vysvetleni celeho vkusu.'}, 'favorite_genres': {'meaning': 'Rucni zanrove preference; nizsi preference_rank znamena silnejsi preferenci.', 'ai_usage': 'Cist jako explicitni korekci automatickych signalu.'}, 'favorite_traits': {'meaning': 'Rucni jemne preference typu slow-burn, dialogue-driven nebo atmospheric.', 'ai_usage': 'Zatim hlavne interpretacni kontext; nemusi byt plne zapocitany ve vsech scoring vypoctech.'}, 'people_affinity': {'meaning': 'Konkretni osoby v titulu, ktere maji rucni affinity rating.', 'ai_usage': 'Cist jako osobni vztah k osobe, oddelene od role/postavy.'}, 'title_role_signals': {'meaning': 'Konkretni signaly role/postavy v jednom titulu: postava, dialogy, chovani, vztahova dynamika, provedeni, vzhled nebo pritazlivost.', 'ai_usage': 'Cist samostatne mimo final_score. Priklad: nizky rating celeho serialu muze koexistovat se silnym pozitivnim signalem jedne postavy.', 'current_scoring_inclusion': False}}, 'known_limitations': ['Title role signals zatim nejsou zapocitane do final_score ani genre_score_signals.', 'Favorite traits jsou sbirane jako jemny kontext; jejich vliv na scoring se muze dal menit.', 'Bez slovnich poznamek muze byt duvod ratingu nejasny.', 'Seznamove role ai_input_role rikaji vyznam zdroje, ale samy o sobe nejsou detailni vysvetleni vkusu.'], 'recommended_ai_reading_order': ['Nejdrive nacist /api/ai/context kvuli skalam a definicim.', 'Potom nacist /api/ai/scoring-explainer kvuli vyznamu score poli.', 'Potom nacist /api/ai/taste-inputs pro sirsi vstupy podle ai_input_role.', 'Podle potreby doplnit /api/ai/rated-titles pro silne lokalne hodnocene tituly.', 'Pri interpretaci kazde polozky kombinovat user_rating, liked/disliked notes, people_affinity, title_role_signals a genre_score_signals.']}

def get_favorite_genres(active_only: bool=True) -> list[dict[str, Any]]:
    """Return locally curated favorite genres ordered by preference and weight."""
    return fetch_favorite_genres_postgres(active_only=active_only)

def get_catalog_genres() -> list[dict[str, Any]]:
    """Return all distinct catalog genres with how many titles use them."""
    return fetch_catalog_genres_postgres()

def get_favorite_traits(active_only: bool=True) -> list[dict[str, Any]]:
    """Return locally curated favorite traits ordered by preference and weight."""
    return fetch_favorite_traits_postgres(active_only=active_only)

def get_genre_score_source_rows() -> list[dict[str, Any]]:
    """Return title-level behavioral inputs for genre scoring."""
    return fetch_genre_score_source_rows_postgres()

def get_home_suggestion_sections(*, limit_per_section: int | None=4) -> dict[str, Any]:
    """Build the two homepage suggestion buckets from local metadata only."""
    active_traits = [item for item in fetch_favorite_traits_postgres(active_only=True) if item.get('preference_rank') is not None]
    latest_genre_scores = fetch_latest_genre_scores_postgres(score_scope='default', limit=None)
    genre_score_lookup = {str(item.get('genre')): float(item.get('normalized_score') or 0.0) for item in (latest_genre_scores or {}).get('items') or [] if item.get('genre')}
    candidate_rows = _get_home_suggestion_candidate_rows(None)
    trait_matches: list[dict[str, Any]] = []
    new_on_imdb: list[dict[str, Any]] = []
    for row in candidate_rows:
        trait_eval = evaluate_trait_candidate(row, active_traits, genre_score_lookup)
        if trait_eval['matched_traits']:
            trait_matches.append({**row, **trait_eval})
        new_eval = evaluate_new_imdb_candidate(row, active_traits, genre_score_lookup)
        if new_eval['is_recent'] and new_eval['imdb_quality_score'] >= 0.35 and (int(row.get('cz_provider_count') or 0) > 0 or new_eval['trait_score'] >= 0.2 or new_eval['actor_affinity_score'] >= 0.15):
            new_on_imdb.append({**row, **new_eval})
    trait_matches.sort(key=lambda item: (-float(item['total_score']), -len(item.get('matched_traits') or []), -int(item.get('num_votes') or 0), -float(item.get('average_rating') or 0.0), -int(item.get('start_year') or 0), str(item.get('primary_title') or '')))
    new_on_imdb.sort(key=lambda item: (-float(item['total_score']), -float(item.get('freshness_score') or 0.0), -int(item.get('num_votes') or 0), -float(item.get('average_rating') or 0.0), -int(item.get('start_year') or 0), str(item.get('primary_title') or '')))
    trait_items = trait_matches if limit_per_section is None else trait_matches[:limit_per_section]
    new_items = new_on_imdb if limit_per_section is None else new_on_imdb[:limit_per_section]
    return {'trait_matches': trait_items, 'new_on_imdb': new_items, 'active_traits': active_traits}

def get_genre_suggestion_candidates(genre: str, *, limit: int | None=24) -> dict[str, Any]:
    """Return current unwatched recommendation candidates for one genre."""
    resolved_genre = genre.strip()
    active_traits = [item for item in fetch_favorite_traits_postgres(active_only=True) if item.get('preference_rank') is not None]
    latest_genre_scores = fetch_latest_genre_scores_postgres(score_scope='default', limit=None)
    genre_score_lookup = {str(item.get('genre')): float(item.get('normalized_score') or 0.0) for item in (latest_genre_scores or {}).get('items') or [] if item.get('genre')}
    candidate_rows = _get_home_suggestion_candidate_rows(None)
    items: list[dict[str, Any]] = []
    for row in candidate_rows:
        genres = [str(item) for item in row.get('genres') or []]
        if resolved_genre not in genres:
            continue
        trait_eval = evaluate_trait_candidate(row, active_traits, genre_score_lookup)
        if not (trait_eval['trait_score'] >= 0.2 or trait_eval['actor_affinity_score'] >= 0.15):
            continue
        candidate_score = min(1.0, float(trait_eval['total_score']) + 0.18 * float(trait_eval['genre_alignment_score']))
        items.append({**row, **trait_eval, 'candidate_score': round(candidate_score, 4)})
    items.sort(key=lambda item: (-float(item['candidate_score']), -len(item.get('matched_traits') or []), -int(item.get('cz_provider_count') or 0), -float(item.get('average_rating') or 0.0), -int(item.get('num_votes') or 0), -int(item.get('start_year') or 0), str(item.get('primary_title') or '')))
    if limit is not None:
        items = items[:limit]
    return {'genre': resolved_genre, 'items': items, 'active_traits': active_traits}

def replace_favorite_genres(genres: Sequence[str | dict[str, Any]], *, source_origin: str='local_app', source_ref: str | None=None, archive_missing: bool=True) -> dict[str, Any]:
    """Replace or refresh the curated favorite genre list."""
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(genres, start=1):
        if isinstance(item, str):
            genre = item.strip()
            payload = {'genre': genre, 'weight': 1.0, 'preference_rank': index, 'notes': None, 'is_active': True}
        else:
            genre = str(item.get('genre') or '').strip()
            payload = {'genre': genre, 'weight': float(item.get('weight', 1.0)), 'preference_rank': item.get('preference_rank', index), 'notes': item.get('notes'), 'is_active': bool(item.get('is_active', True))}
        if not genre:
            raise ValueError('Kazdy zanr musi mit neprazdny nazev.')
        normalized.append(payload)
    now = _now_iso()
    normalized_genres = {item['genre'] for item in normalized}
    replace_favorite_genres_postgres(items=normalized, source_origin=source_origin, source_ref=source_ref, archive_missing=archive_missing, now=now)
    return {'count': len(normalized), 'genres': sorted(normalized_genres), 'updated_at': now}

def replace_favorite_traits(traits: Sequence[str | dict[str, Any]], *, source_origin: str='local_app', source_ref: str | None=None, archive_missing: bool=True) -> dict[str, Any]:
    """Replace or refresh the curated favorite trait list."""
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(traits, start=1):
        if isinstance(item, str):
            trait = item.strip()
            payload = {'trait': trait, 'weight': 1.0, 'preference_rank': index, 'notes': None, 'is_active': True}
        else:
            trait = str(item.get('trait') or '').strip()
            payload = {'trait': trait, 'weight': float(item.get('weight', 1.0)), 'preference_rank': item.get('preference_rank', index), 'notes': item.get('notes'), 'is_active': bool(item.get('is_active', True))}
        if not trait:
            raise ValueError('Kazdy trait musi mit neprazdny nazev.')
        normalized.append(payload)
    now = _now_iso()
    normalized_traits = {item['trait'] for item in normalized}
    replace_favorite_traits_postgres(items=normalized, source_origin=source_origin, source_ref=source_ref, archive_missing=archive_missing, now=now)
    return {'count': len(normalized), 'traits': sorted(normalized_traits), 'updated_at': now}

def record_genre_score_snapshot(scores: Sequence[dict[str, Any]], *, score_scope: str='default', algorithm_version: str | None=None, source_origin: str='local_app', source_ref: str | None=None, generated_at: str | None=None) -> dict[str, Any]:
    """Persist one genre-score snapshot run with per-genre score breakdown."""
    if not scores:
        raise ValueError('Je potreba dodat alespon jeden zanr se score.')
    snapshot_time = generated_at or _now_iso()
    datetime.fromisoformat(snapshot_time.replace('Z', '+00:00'))
    prepared_rows: list[dict[str, Any]] = []
    for index, item in enumerate(scores, start=1):
        genre = str(item.get('genre') or '').strip()
        if not genre:
            raise ValueError('Kazdy zaznam genre_scores musi mit genre.')
        if item.get('final_score') is None:
            raise ValueError(f"Zaznam pro zanr '{genre}' nema final_score.")
        prepared_rows.append({'id': str(uuid.uuid4()), 'genre': genre, 'titles_considered': item.get('titles_considered'), 'watched_titles_considered': item.get('watched_titles_considered'), 'rated_titles_considered': item.get('rated_titles_considered'), 'contributing_titles_json': _dumps_json_or_none(item.get('contributing_titles')), 'excluded_titles_json': _dumps_json_or_none(item.get('excluded_titles')), 'favorite_genre_weight': item.get('favorite_genre_weight'), 'preference_overlap_score': item.get('preference_overlap_score'), 'preference_alignment_score': item.get('preference_alignment_score'), 'affinity_score': item.get('affinity_score'), 'rating_signal_score': item.get('rating_signal_score'), 'watch_signal_score': item.get('watch_signal_score'), 'recency_score': item.get('recency_score'), 'actor_affinity_score': item.get('actor_affinity_score'), 'frequency_score': item.get('frequency_score'), 'consistency_score': item.get('consistency_score'), 'novelty_score': item.get('novelty_score'), 'confidence_score': item.get('confidence_score'), 'manual_adjustment_score': item.get('manual_adjustment_score'), 'final_score': item.get('final_score'), 'normalized_score': item.get('normalized_score'), 'rank_in_run': item.get('rank_in_run', index), 'metrics_json': _dumps_json_or_none(item.get('metrics')), 'explanation': item.get('explanation')})
    return insert_genre_score_snapshot(rows=[{'id': item['id'], 'genre': item['genre'], 'generated_at': snapshot_time, 'algorithm_version': algorithm_version, 'score_scope': score_scope, 'source_origin': source_origin, 'source_ref': source_ref, 'titles_considered': item['titles_considered'], 'watched_titles_considered': item['watched_titles_considered'], 'rated_titles_considered': item['rated_titles_considered'], 'contributing_titles_json': item['contributing_titles_json'], 'excluded_titles_json': item['excluded_titles_json'], 'favorite_genre_weight': item['favorite_genre_weight'], 'preference_overlap_score': item['preference_overlap_score'], 'preference_alignment_score': item['preference_alignment_score'], 'affinity_score': item['affinity_score'], 'rating_signal_score': item['rating_signal_score'], 'watch_signal_score': item['watch_signal_score'], 'recency_score': item['recency_score'], 'actor_affinity_score': item['actor_affinity_score'], 'frequency_score': item['frequency_score'], 'consistency_score': item['consistency_score'], 'novelty_score': item['novelty_score'], 'confidence_score': item['confidence_score'], 'manual_adjustment_score': item['manual_adjustment_score'], 'final_score': item['final_score'], 'normalized_score': item['normalized_score'], 'rank_in_run': item['rank_in_run'], 'metrics_json': item['metrics_json'], 'explanation': item['explanation'], 'created_at': snapshot_time} for item in prepared_rows])

def compute_and_record_genre_scores(*, score_scope: str='default', algorithm_version: str | None=None, source_origin: str='local_app', source_ref: str | None=None, generated_at: str | None=None) -> dict[str, Any]:
    """Compute one genre-score snapshot from local data and store it."""
    snapshot_time = generated_at or _now_iso()
    title_rows = fetch_genre_score_source_rows_postgres()
    favorite_genres = fetch_favorite_genres_postgres(active_only=True)
    catalog_genres = fetch_catalog_genres_postgres()
    scores = compute_genre_scores(title_rows, favorite_genres, catalog_genres, generated_at=snapshot_time)
    if not scores:
        raise ValueError('Pro vypocet genre_scores zatim nejsou zadna lokalni data.')
    resolved_algorithm_version = algorithm_version or ((scores[0].get('metrics') or {}).get('algorithm_version') if scores else None)
    summary = record_genre_score_snapshot(scores, score_scope=score_scope, algorithm_version=resolved_algorithm_version, source_origin=source_origin, source_ref=source_ref, generated_at=snapshot_time)
    top_rows = fetch_latest_genre_scores_postgres(score_scope=score_scope, limit=10)
    return {**summary, 'titles_considered': len(title_rows), 'favorite_genres_count': len(favorite_genres), 'top_genres': top_rows['items'] if top_rows else []}

def get_latest_genre_scores(*, score_scope: str | None=None, limit: int | None=None) -> dict[str, Any] | None:
    """Load the newest genre-score snapshot, optionally within one scope."""
    return fetch_latest_genre_scores_postgres(score_scope=score_scope, limit=limit)

def _get_favorite_genres(conn, *, active_only: bool) -> list[dict[str, Any]]:
    rows = conn.execute('\n        SELECT\n            genre,\n            weight,\n            preference_rank,\n            source_origin,\n            source_ref,\n            notes,\n            is_active,\n            created_at,\n            updated_at\n        FROM app.favorite_genres\n        WHERE (? = FALSE OR is_active = TRUE)\n        ORDER BY preference_rank ASC NULLS LAST, weight DESC, genre ASC\n        ', [active_only]).fetchall()
    return [{'genre': row[0], 'weight': row[1], 'preference_rank': row[2], 'source_origin': row[3], 'source_ref': row[4], 'notes': row[5], 'is_active': row[6], 'created_at': row[7], 'updated_at': row[8]} for row in rows]

def _get_favorite_traits(conn, *, active_only: bool) -> list[dict[str, Any]]:
    rows = conn.execute('\n        SELECT\n            trait,\n            weight,\n            preference_rank,\n            source_origin,\n            source_ref,\n            notes,\n            is_active,\n            created_at,\n            updated_at\n        FROM app.favorite_traits\n        WHERE (? = FALSE OR is_active = TRUE)\n        ORDER BY preference_rank ASC NULLS LAST, weight DESC, trait ASC\n        ', [active_only]).fetchall()
    return [{'trait': row[0], 'weight': row[1], 'preference_rank': row[2], 'source_origin': row[3], 'source_ref': row[4], 'notes': row[5], 'is_active': row[6], 'created_at': row[7], 'updated_at': row[8]} for row in rows]

def _get_catalog_genres(conn) -> list[dict[str, Any]]:
    rows = conn.execute("\n        WITH exploded AS (\n            SELECT\n                trim(unnest(string_split(genres, ','))) AS genre\n            FROM app.catalog_titles\n            WHERE genres IS NOT NULL AND genres <> ''\n        )\n        SELECT genre, COUNT(*) AS title_count\n        FROM exploded\n        WHERE genre IS NOT NULL AND genre <> ''\n        GROUP BY genre\n        ORDER BY genre ASC\n        ").fetchall()
    return [{'genre': row[0], 'title_count': row[1]} for row in rows]

def _get_genre_score_source_rows(conn, *, ratings_in_postgres: bool=False, watch_events_in_postgres: bool=False) -> list[dict[str, Any]]:
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
    from filmy.db_library import record_watch_event as _impl
    return _impl(tconst, watched_on=watched_on, notes=notes, add_to_watched_list=add_to_watched_list, archive_from_list_id=archive_from_list_id, archive_display_tconst=archive_display_tconst)

def record_watch_events_through_episode(episode_tconst: str, *, watched_on: str | None=None, notes: str | None=None) -> dict[str, Any]:
    from filmy.db_library import record_watch_events_through_episode as _impl
    return _impl(episode_tconst, watched_on=watched_on, notes=notes)

def delete_group_from_user_list(list_id: str, display_tconst: str) -> dict[str, Any]:
    from filmy.db_library import delete_group_from_user_list as _impl
    return _impl(list_id, display_tconst)

def move_group_between_user_lists(source_list_id: str, target_list_id: str, display_tconst: str) -> dict[str, Any]:
    from filmy.db_library import move_group_between_user_lists as _impl
    return _impl(source_list_id, target_list_id, display_tconst)

def copy_group_to_user_list(source_list_id: str, target_list_id: str, display_tconst: str) -> dict[str, Any]:
    from filmy.db_library import copy_group_to_user_list as _impl
    return _impl(source_list_id, target_list_id, display_tconst)

def create_user_list(name: str, description: str | None=None) -> dict[str, Any]:
    from filmy.db_library import create_user_list as _impl
    return _impl(name, description)

def update_user_list_description(list_id: str, description: str | None=None, ai_input_role: str | None=None) -> dict[str, Any]:
    from filmy.db_library import update_user_list_description as _impl
    return _impl(list_id, description, ai_input_role=ai_input_role)

def delete_user_list(list_id: str) -> dict[str, Any]:
    from filmy.db_library import delete_user_list as _impl
    return _impl(list_id)

def clear_ai_suggestions_list_items() -> dict[str, Any]:
    from filmy.db_library import clear_ai_suggestions_list_items as _impl
    return _impl()

def set_title_role_signal(tconst: str, *, nconst: str | None=None, character_name: str | None=None, signal_type: str='character', polarity: str='positive', strength: int=8, notes: str | None=None) -> dict[str, Any]:
    from filmy.db_library import set_title_role_signal as _impl
    return _impl(tconst, nconst=nconst, character_name=character_name, signal_type=signal_type, polarity=polarity, strength=strength, notes=notes)

def replace_title_role_signals(tconst: str, *, nconst: str | None=None, character_name: str | None=None, signal_types: list[str] | tuple[str, ...] | None=None, polarity: str='positive', strength: int=8, notes: str | None=None) -> dict[str, Any]:
    from filmy.db_library import replace_title_role_signals as _impl
    return _impl(tconst, nconst=nconst, character_name=character_name, signal_types=signal_types, polarity=polarity, strength=strength, notes=notes)

def delete_title_role_signals(tconst: str, *, nconst: str | None=None, character_name: str | None=None) -> dict[str, Any]:
    from filmy.db_library import delete_title_role_signals as _impl
    return _impl(tconst, nconst=nconst, character_name=character_name)

def get_title_role_signals(tconst: str) -> list[dict[str, Any]]:
    from filmy.db_library import get_title_role_signals as _impl
    return _impl(tconst)

def _pick_best_title_match(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    query_key = _normalize_match_key(query)
    query_key_articleless = _normalize_match_key(query, strip_leading_articles=True)
    exact_matches: list[dict[str, Any]] = []
    for candidate in candidates:
        if _normalize_match_key(candidate.get('primary_title')) == query_key:
            exact_matches.append(candidate)
            continue
        if _normalize_match_key(candidate.get('original_title')) == query_key:
            exact_matches.append(candidate)
            continue
        if _normalize_match_key(candidate.get('matched_alias_title')) == query_key:
            exact_matches.append(candidate)
            continue
        if _normalize_match_key(candidate.get('primary_title'), strip_leading_articles=True) == query_key_articleless:
            exact_matches.append(candidate)
            continue
        if _normalize_match_key(candidate.get('original_title'), strip_leading_articles=True) == query_key_articleless:
            exact_matches.append(candidate)
            continue
        if _normalize_match_key(candidate.get('matched_alias_title'), strip_leading_articles=True) == query_key_articleless:
            exact_matches.append(candidate)
    if exact_matches:
        exact_matches.sort(key=lambda item: (_lookup_local_signal_score(item), -int(item.get('alias_priority') or 99), item.get('num_votes') or 0, item.get('start_year') or 0), reverse=True)
        return exact_matches[0]
    fuzzy_matches = [candidate for candidate in candidates if candidate.get('fuzzy_score') is not None]
    if fuzzy_matches:
        fuzzy_matches.sort(key=lambda item: (item.get('fuzzy_score') or 0.0, -int(item.get('alias_priority') or 99), item.get('num_votes') or 0, item.get('start_year') or 0), reverse=True)
        strongest = fuzzy_matches[0]
        if (strongest.get('fuzzy_score') or 0.0) >= 0.72:
            strongest_score = strongest.get('fuzzy_score') or 0.0
            near_top = [item for item in fuzzy_matches if (item.get('fuzzy_score') or 0.0) >= strongest_score - 0.08]
            if len(near_top) > 1:
                near_top.sort(key=lambda item: (item.get('num_votes') or 0, item.get('start_year') or 0, item.get('fuzzy_score') or 0.0), reverse=True)
                return near_top[0]
            return strongest
    return candidates[0]

def _build_title_lookup_result(*, query: str, title_type: str | None, selected: dict[str, Any], candidates: list[dict[str, Any]], candidates_limit: int) -> dict[str, Any]:
    selected_key = selected['tconst']
    ordered_candidates = sorted(candidates, key=lambda item: (0 if item['tconst'] == selected_key else 1, -(item.get('fuzzy_score') or 0.0), int(item.get('alias_priority') or 99), -(item.get('start_year') or 0), item['primary_title']))
    return {'query': query, 'title_type': title_type, 'selected_tconst': selected_key, 'selected': _build_lookup_candidate(selected, query=query, is_selected=True), 'candidates': [_build_lookup_candidate(candidate, query=query, is_selected=candidate['tconst'] == selected_key) for candidate in ordered_candidates[:max(candidates_limit, 1)]], 'candidate_count': len(candidates)}

def _lookup_person_from_search_recall(query: str, *, candidates_limit: int) -> dict[str, Any] | None:
    """Try to satisfy person lookup from the small recent-search recall table first."""
    query_key = _normalize_match_key(query)
    query_text = _normalize_search_query_text(query)
    if not query_key or not query_text:
        return None
    match = fetch_search_recall_match(entity_type='person', query_key=query_key, query_text_fold=query_text.casefold())
    row = None if match is None else (match[0], match[1])
    if row is None:
        return None
    person_row = fetch_person_lookup_row(str(row[0]))
    if person_row is None:
        return None
    candidate = _person_lookup_item_from_row(person_row)
    candidate['fuzzy_score'] = row[1]
    result = {'query': query, 'selected_nconst': str(candidate['nconst']), 'selected': _build_person_lookup_candidate(candidate, query=query, is_selected=True), 'candidates': [_build_person_lookup_candidate(candidate, query=query, is_selected=True)], 'candidate_count': 1}
    _record_search_recall_entry(entity_type='person', query=query, target_id=str(candidate['nconst']), target_label=str(candidate.get('primary_name') or ''), fuzzy_score=candidate.get('fuzzy_score'))
    return result

def _remember_person_lookup(query: str, selected: dict[str, Any]) -> None:
    if not _is_confident_person_lookup(query, selected):
        return
    _record_search_recall_entry(entity_type='person', query=query, target_id=str(selected['nconst']), target_label=str(selected.get('primary_name') or ''), fuzzy_score=selected.get('fuzzy_score'))

def _pick_best_person_match(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    fuzzy_matches = [candidate for candidate in candidates if candidate.get('fuzzy_score') is not None]
    if fuzzy_matches:
        fuzzy_matches.sort(key=lambda item: (item.get('fuzzy_score') or 0.0, item.get('credit_count') or 0, item.get('birth_year') or 0), reverse=True)
        strongest = fuzzy_matches[0]
        if (strongest.get('fuzzy_score') or 0.0) >= 0.72:
            strongest_score = strongest.get('fuzzy_score') or 0.0
            near_top = [item for item in fuzzy_matches if (item.get('fuzzy_score') or 0.0) >= strongest_score - 0.08]
            if len(near_top) > 1:
                near_top.sort(key=lambda item: (item.get('credit_count') or 0, item.get('birth_year') or 0, item.get('fuzzy_score') or 0.0), reverse=True)
                return near_top[0]
            return strongest
    query_key = _normalize_match_key(query)
    exact_matches: list[dict[str, Any]] = []
    for candidate in candidates:
        if _normalize_match_key(candidate.get('primary_name')) == query_key:
            exact_matches.append(candidate)
    if exact_matches:
        exact_matches.sort(key=lambda item: (item.get('credit_count') or 0, item.get('birth_year') or 0), reverse=True)
        return exact_matches[0]
    return candidates[0]

def _build_lookup_candidate(candidate: dict[str, Any], *, query: str, is_selected: bool) -> dict[str, Any]:
    return {'tconst': candidate['tconst'], 'primary_title': candidate['primary_title'], 'original_title': candidate['original_title'], 'title_type': candidate['title_type'], 'kind_label': _title_type_label(candidate.get('title_type')), 'start_year': candidate['start_year'], 'runtime_minutes': candidate.get('runtime_minutes'), 'genres': candidate.get('genres') or [], 'average_rating': candidate.get('average_rating'), 'num_votes': candidate.get('num_votes'), 'library': candidate.get('library') or {}, 'is_selected': is_selected, 'is_exact_match': _normalize_match_key(candidate.get('primary_title')) == _normalize_match_key(query) or _normalize_match_key(candidate.get('original_title')) == _normalize_match_key(query) or _normalize_match_key(candidate.get('matched_alias_title')) == _normalize_match_key(query) or (_normalize_match_key(candidate.get('primary_title'), strip_leading_articles=True) == _normalize_match_key(query, strip_leading_articles=True)) or (_normalize_match_key(candidate.get('original_title'), strip_leading_articles=True) == _normalize_match_key(query, strip_leading_articles=True)) or (_normalize_match_key(candidate.get('matched_alias_title'), strip_leading_articles=True) == _normalize_match_key(query, strip_leading_articles=True)), 'fuzzy_score': candidate.get('fuzzy_score'), 'matched_alias_title': candidate.get('matched_alias_title')}

def _build_person_lookup_candidate(candidate: dict[str, Any], *, query: str, is_selected: bool) -> dict[str, Any]:
    return {'nconst': candidate['nconst'], 'primary_name': candidate['primary_name'], 'birth_year': candidate.get('birth_year'), 'death_year': candidate.get('death_year'), 'primary_profession': candidate.get('primary_profession'), 'known_for_titles': candidate.get('known_for_titles'), 'filmography': candidate.get('filmography') or {}, 'credit_count': candidate.get('credit_count') or 0, 'is_selected': is_selected, 'is_exact_match': _normalize_match_key(candidate.get('primary_name')) == _normalize_match_key(query), 'fuzzy_score': candidate.get('fuzzy_score')}

def _is_confident_person_lookup(query: str, candidate: dict[str, Any]) -> bool:
    if _normalize_match_key(candidate.get('primary_name')) == _normalize_match_key(query):
        return True
    return (candidate.get('fuzzy_score') or 0.0) >= 0.82

def _should_expand_people_to_fuzzy(query: str, candidates: list[dict[str, Any]]) -> bool:
    query_key = _normalize_match_key(query)
    if not query_key or not candidates:
        return True
    for candidate in candidates[:3]:
        if _normalize_match_key(candidate.get('primary_name')) == query_key:
            return False
    best_direct_score = max((_best_person_name_similarity(query_key, candidate.get('primary_name')) for candidate in candidates[:5]))
    return best_direct_score < 0.72

def _person_lookup_item_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {'nconst': row[0], 'primary_name': row[1], 'birth_year': row[2], 'death_year': row[3], 'primary_profession': row[4], 'known_for_titles': row[5], 'credit_count': row[6] or 0}

def _search_people_for_lookup(query: str, limit: int) -> list[dict[str, Any]]:
    rows = fetch_people_for_lookup_rows(query, limit)
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _person_lookup_item_from_row(row)
        item['fuzzy_score'] = _best_person_name_similarity(_normalize_match_key(query), item['primary_name'])
        items.append(item)
    return items

def _search_people_for_lookup_fuzzy(query: str, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query)
    if len(query_key) < 3:
        return []
    prefix3 = query_key[:3]
    prefix2 = query_key[:2]
    length_floor = max(len(query_key) - 2, 1)
    length_ceiling = len(query_key) + 3
    rows = fetch_people_for_lookup_fuzzy_rows(query_key, 500)
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _person_lookup_item_from_row(row)
        item['fuzzy_score'] = _best_person_name_similarity(query_key, item['primary_name'])
        items.append(item)
    items.sort(key=lambda item: (item.get('fuzzy_score') or 0.0, item.get('credit_count') or 0, item.get('birth_year') or 0), reverse=True)
    return [item for item in items if (item.get('fuzzy_score') or 0.0) >= 0.65][:limit]

def _search_people_for_lookup_levenshtein(query: str, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query)
    if len(query_key) < 4:
        return []
    first_letter = query_key[0]
    query_len = len(query_key)
    length_floor = max(query_len - 4, 1)
    length_ceiling = query_len + 4
    rows = fetch_people_for_lookup_levenshtein_rows(query_key, 500)
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _person_lookup_item_from_row(row[:7])
        item['fuzzy_score'] = _best_person_name_similarity(query_key, item['primary_name'])
        items.append(item)
    items.sort(key=lambda item: (item.get('fuzzy_score') or 0.0, item.get('credit_count') or 0, item.get('birth_year') or 0), reverse=True)
    return [item for item in items if (item.get('fuzzy_score') or 0.0) >= 0.65][:limit]

def _fetch_person_filmography_summary(conn, nconst: str) -> dict[str, Any]:
    rows = conn.execute('\n        SELECT\n            c.credit_group,\n            t.primary_title,\n            t.start_year,\n            t.title_type\n        FROM app.title_credits AS c\n        JOIN app.catalog_titles AS t ON t.tconst = c.tconst\n        WHERE c.nconst = ?\n        ORDER BY t.start_year DESC NULLS LAST, t.primary_title\n        LIMIT 50\n        ', [nconst]).fetchall()
    grouped = {'director': [], 'creator': [], 'writer': [], 'cast': [], 'principal': []}
    for row in rows:
        grouped.setdefault(row[0], []).append({'title': row[1], 'start_year': row[2], 'title_type': row[3]})
    return {'credit_count': len(rows), 'director': grouped['director'][:20], 'creator': grouped['creator'][:20], 'writer': grouped['writer'][:20], 'cast': grouped['cast'][:20], 'principal': grouped['principal'][:20]}

def _is_confident_lookup(query: str, candidate: dict[str, Any]) -> bool:
    if _normalize_match_key(candidate.get('primary_title')) == _normalize_match_key(query):
        return True
    if _normalize_match_key(candidate.get('original_title')) == _normalize_match_key(query):
        return True
    if _normalize_match_key(candidate.get('matched_alias_title')) == _normalize_match_key(query):
        return True
    if _normalize_match_key(candidate.get('primary_title'), strip_leading_articles=True) == _normalize_match_key(query, strip_leading_articles=True):
        return True
    if _normalize_match_key(candidate.get('original_title'), strip_leading_articles=True) == _normalize_match_key(query, strip_leading_articles=True):
        return True
    if _normalize_match_key(candidate.get('matched_alias_title'), strip_leading_articles=True) == _normalize_match_key(query, strip_leading_articles=True):
        return True
    return (candidate.get('fuzzy_score') or 0.0) >= 0.82

def _is_direct_enough_lookup(query: str, candidate: dict[str, Any]) -> bool:
    query_key = _normalize_match_key(query)
    query_key_articleless = _normalize_match_key(query, strip_leading_articles=True)
    if not query_key:
        return False
    for variant in [candidate.get('primary_title'), candidate.get('original_title'), candidate.get('matched_alias_title')]:
        if _normalize_match_key(variant) == query_key:
            return True
        if _normalize_match_key(variant, strip_leading_articles=True) == query_key_articleless:
            return True
    return False

def _is_exact_title_match(query: str, candidate: dict[str, Any]) -> bool:
    query_key = _normalize_match_key(query)
    query_key_articleless = _normalize_match_key(query, strip_leading_articles=True)
    if not query_key:
        return False
    for variant in [candidate.get('primary_title'), candidate.get('original_title'), candidate.get('matched_alias_title')]:
        if _normalize_match_key(variant) == query_key:
            return True
        if _normalize_match_key(variant, strip_leading_articles=True) == query_key_articleless:
            return True
    return False

def _lookup_local_signal_score(candidate: dict[str, Any]) -> int:
    """Score exact-title ambiguities by Jiri's local library signals."""
    library = candidate.get('library') or {}
    score = 0
    if int(library.get('watched_count') or 0) > 0:
        score += 80
    rating = library.get('rating') or {}
    if rating.get('value') is not None:
        score += 60 + int(rating.get('value') or 0)
    if library.get('in_watchlist'):
        score += 40
    if library.get('lists'):
        score += 20
    return score

def _attach_library_summaries_to_exact_title_candidates(query: str, candidates: list[dict[str, Any]]) -> None:
    """Attach local state only where it helps disambiguate identical titles."""
    exact_candidates = [candidate for candidate in candidates if _is_exact_title_match(query, candidate)]
    if len(exact_candidates) < 2:
        return
    for candidate in exact_candidates:
        if candidate.get('library'):
            continue
        try:
            candidate['library'] = _fetch_library_summary(None, str(candidate['tconst']), candidate.get('title_type'))
        except Exception:
            logger.debug('lookup library summary failed for %s', candidate.get('tconst'), exc_info=True)
            candidate['library'] = {}

def _is_safe_recalled_title(query: str, *, title_type: str | None, recalled: dict[str, Any], candidates_limit: int) -> bool:
    """Avoid recall shortcuts for ambiguous exact-title queries."""
    query_key = _normalize_match_key(query)
    if len(_match_tokens(query_key)) != 1 or len(query_key) < 5:
        return True
    candidates = _search_catalog_for_lookup(query=query, title_type=title_type, limit=max(max(candidates_limit, 1) * 5, 25))
    alias_candidates = _search_catalog_aliases_for_lookup(query=query, title_type=title_type, limit=max(max(candidates_limit, 1) * 5, 25))
    candidates = _merge_lookup_candidates(candidates, alias_candidates)
    exact_candidates = [candidate for candidate in candidates if _is_exact_title_match(query, candidate)]
    if len(exact_candidates) < 2:
        return True
    return False

def _should_expand_to_fuzzy(query: str, candidates: list[dict[str, Any]]) -> bool:
    query_key = _normalize_match_key(query)
    query_key_articleless = _normalize_match_key(query, strip_leading_articles=True)
    if not query_key or not candidates:
        return True
    for candidate in candidates[:3]:
        if _normalize_match_key(candidate.get('primary_title')) == query_key:
            return False
        if _normalize_match_key(candidate.get('original_title')) == query_key:
            return False
        if _normalize_match_key(candidate.get('matched_alias_title')) == query_key:
            return False
        if _normalize_match_key(candidate.get('primary_title'), strip_leading_articles=True) == query_key_articleless:
            return False
        if _normalize_match_key(candidate.get('original_title'), strip_leading_articles=True) == query_key_articleless:
            return False
        if _normalize_match_key(candidate.get('matched_alias_title'), strip_leading_articles=True) == query_key_articleless:
            return False
    best_direct_score = max((_best_title_similarity(query_key, [candidate.get('primary_title'), candidate.get('original_title'), candidate.get('matched_alias_title')]) for candidate in candidates[:5]))
    return best_direct_score < 0.72

def _merge_lookup_candidates(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {_lookup_identity_key(item): item for item in primary}
    for item in secondary:
        identity = _lookup_identity_key(item)
        existing = merged.get(identity)
        if existing is None:
            merged[identity] = item
            continue
        existing_score = existing.get('fuzzy_score') or 0.0
        new_score = item.get('fuzzy_score') or 0.0
        if new_score > existing_score:
            combined = {**existing, **item}
            merged[identity] = combined
    return list(merged.values())

def _lookup_identity_key(item: dict[str, Any]) -> str:
    return str(item.get('tconst') or item.get('nconst') or '')

def _alias_priority_case_sql(region_column: str, language_column: str) -> str:
    return f"\n        CASE\n            WHEN lower(coalesce({language_column}, '')) = 'cs' OR upper(coalesce({region_column}, '')) = 'CZ' THEN 0\n            WHEN lower(coalesce({language_column}, '')) = 'en'\n                 OR upper(coalesce({region_column}, '')) IN ('US', 'GB', 'CA', 'IE', 'AU', 'NZ', 'IN') THEN 1\n            ELSE 2\n        END\n    "

def _catalog_row_from_alias_row(row: tuple[Any, ...]) -> dict[str, Any]:
    item = _catalog_row_to_dict(row[:9])
    item['matched_alias_title'] = row[9]
    item['alias_region'] = row[10]
    item['alias_language'] = row[11]
    item['alias_priority'] = row[12]
    return item

def _search_catalog_aliases_for_lookup(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query)
    query_key_articleless = _normalize_match_key(query, strip_leading_articles=True)
    if not query_key:
        return []
    with _pg_connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n                SELECT\n                    t.tconst,\n                    t.title_type,\n                    t.primary_title,\n                    t.original_title,\n                    t.start_year,\n                    t.runtime_minutes,\n                    t.genres,\n                    t.average_rating,\n                    t.num_votes,\n                    a.title AS matched_alias_title,\n                    a.region,\n                    a.language,\n                    a.alias_priority\n                FROM app.title_alias_lookup AS a\n                JOIN app.catalog_titles AS t ON t.tconst = a.tconst\n                WHERE (\n                    a.alias_key = %s\n                    OR a.alias_key_articleless = %s\n                    OR a.alias_key LIKE %s || '%%'\n                    OR a.alias_key_articleless LIKE %s || '%%'\n                )\n                  AND (%s::text IS NULL OR t.title_type = %s::text)\n                ORDER BY\n                    a.alias_priority,\n                    CASE\n                        WHEN a.alias_key = %s THEN 0\n                        WHEN a.alias_key_articleless = %s THEN 1\n                        WHEN a.alias_key LIKE %s || '%%' THEN 2\n                        WHEN a.alias_key_articleless LIKE %s || '%%' THEN 3\n                        ELSE 4\n                    END,\n                    t.start_year DESC NULLS LAST,\n                    t.num_votes DESC NULLS LAST,\n                    t.primary_title\n                LIMIT %s\n                ", (query_key, query_key_articleless or query_key, query_key, query_key_articleless or query_key, title_type, title_type, query_key, query_key_articleless or query_key, query_key, query_key_articleless or query_key, limit))
        rows = cursor.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _catalog_row_from_alias_row(row)
        item['fuzzy_score'] = _best_title_similarity(query_key, [item.get('matched_alias_title')])
        items.append(item)
    return items

def _search_catalog_aliases_for_lookup_fuzzy(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 3:
        return []
    prefix3 = query_key[:3]
    prefix2 = query_key[:2]
    length_floor = max(len(query_key) - 2, 1)
    length_ceiling = len(query_key) + 3
    scan_limit = 200
    with _pg_connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n                SELECT\n                    t.tconst,\n                    t.title_type,\n                    t.primary_title,\n                    t.original_title,\n                    t.start_year,\n                    t.runtime_minutes,\n                    t.genres,\n                    t.average_rating,\n                    t.num_votes,\n                    a.title AS matched_alias_title,\n                    a.region,\n                    a.language,\n                    a.alias_priority\n                FROM app.title_alias_lookup AS a\n                JOIN app.catalog_titles AS t ON t.tconst = a.tconst\n                WHERE (%s::text IS NULL OR t.title_type = %s::text)\n                  AND (\n                    a.alias_prefix3_articleless = %s\n                    OR a.alias_prefix2_articleless = %s\n                  )\n                  AND a.alias_length_articleless BETWEEN %s AND %s\n                ORDER BY\n                    a.alias_priority,\n                    t.num_votes DESC NULLS LAST,\n                    t.average_rating DESC NULLS LAST,\n                    t.start_year DESC NULLS LAST\n                LIMIT %s\n                ', (title_type, title_type, prefix3, prefix2, length_floor, length_ceiling, scan_limit))
        rows = cursor.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _catalog_row_from_alias_row(row)
        item['fuzzy_score'] = _best_title_similarity(query_key, [item.get('matched_alias_title')])
        items.append(item)
    items.sort(key=lambda item: (item.get('fuzzy_score') or 0.0, -int(item.get('alias_priority') or 99), item.get('num_votes') or 0, item.get('start_year') or 0), reverse=True)
    return [item for item in items if (item.get('fuzzy_score') or 0.0) >= 0.55][:limit]

def _search_catalog_aliases_for_lookup_levenshtein(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 4:
        return []
    first_letter = query_key[0]
    query_len = len(query_key)
    length_floor = max(query_len - 4, 1)
    length_ceiling = query_len + 4
    with _pg_connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n                SELECT\n                    t.tconst,\n                    t.title_type,\n                    t.primary_title,\n                    t.original_title,\n                    t.start_year,\n                    t.runtime_minutes,\n                    t.genres,\n                    t.average_rating,\n                    t.num_votes,\n                    a.title AS matched_alias_title,\n                    a.region,\n                    a.language,\n                    a.alias_priority\n                FROM app.title_alias_lookup AS a\n                JOIN app.catalog_titles AS t ON t.tconst = a.tconst\n                WHERE (%s::text IS NULL OR t.title_type = %s::text)\n                  AND a.alias_prefix1_articleless = %s\n                  AND a.alias_length_articleless BETWEEN %s AND %s\n                ORDER BY\n                    a.alias_priority,\n                    t.num_votes DESC NULLS LAST,\n                    t.start_year DESC NULLS LAST\n                LIMIT 500\n                ', (title_type, title_type, first_letter, length_floor, length_ceiling))
        rows = cursor.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _catalog_row_from_alias_row(row)
        item['fuzzy_score'] = _best_title_similarity(query_key, [item.get('matched_alias_title')])
        items.append(item)
    items.sort(key=lambda item: (item.get('fuzzy_score') or 0.0, -int(item.get('alias_priority') or 99), item.get('num_votes') or 0, item.get('start_year') or 0), reverse=True)
    return [item for item in items if (item.get('fuzzy_score') or 0.0) >= 0.55][:limit]

def _search_catalog_for_lookup_fuzzy(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 3:
        return []
    prefix3 = query_key[:3]
    prefix2 = query_key[:2]
    length_floor = max(len(query_key) - 2, 1)
    length_ceiling = len(query_key) + 3
    scan_limit = 200
    with _pg_connect() as conn, conn.cursor() as cursor:
        cursor.execute(f'\n                SELECT\n                    tconst,\n                    title_type,\n                    primary_title,\n                    original_title,\n                    start_year,\n                    runtime_minutes,\n                    genres,\n                    average_rating,\n                    num_votes\n                FROM app.title_lookup\n                WHERE (%s::text IS NULL OR title_type = %s::text)\n                  AND (\n                    primary_prefix3 = %s\n                    OR original_prefix3 = %s\n                    OR primary_prefix2 = %s\n                    OR original_prefix2 = %s\n                  )\n                  AND (\n                    primary_length BETWEEN %s AND %s\n                    OR original_length BETWEEN %s AND %s\n                  )\n                ORDER BY\n                    num_votes DESC NULLS LAST,\n                    average_rating DESC NULLS LAST,\n                    start_year DESC NULLS LAST\n                LIMIT {scan_limit}\n                ', (title_type, title_type, prefix3, prefix3, prefix2, prefix2, length_floor, length_ceiling, length_floor, length_ceiling))
        rows = cursor.fetchall()
    scored: list[dict[str, Any]] = []
    for row in rows:
        item = _catalog_row_to_dict(row)
        variants = [item.get('primary_title'), item.get('original_title')]
        item['fuzzy_score'] = _best_title_similarity(query_key, variants)
        scored.append(item)
    scored.sort(key=lambda item: (item.get('fuzzy_score') or 0.0, item.get('num_votes') or 0, item.get('start_year') or 0), reverse=True)
    return [item for item in scored if (item.get('fuzzy_score') or 0.0) >= 0.55][:limit]

def _search_catalog_for_lookup_levenshtein(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 4:
        return []
    first_letter = query_key[0]
    query_len = len(query_key)
    length_floor = max(query_len - 4, 1)
    length_ceiling = query_len + 4
    with _pg_connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n                SELECT\n                    tconst,\n                    title_type,\n                    primary_title,\n                    original_title,\n                    start_year,\n                    runtime_minutes,\n                    genres,\n                    average_rating,\n                    num_votes\n                FROM app.title_lookup\n                WHERE (%s::text IS NULL OR title_type = %s::text)\n                  AND (\n                    primary_prefix1 = %s\n                    OR original_prefix1 = %s\n                  )\n                  AND (\n                    primary_length BETWEEN %s AND %s\n                    OR original_length BETWEEN %s AND %s\n                  )\n                ORDER BY num_votes DESC NULLS LAST, average_rating DESC NULLS LAST, start_year DESC NULLS LAST\n                LIMIT 500\n                ', (title_type, title_type, first_letter, first_letter, length_floor, length_ceiling, length_floor, length_ceiling))
        rows = cursor.fetchall()
    scored: list[dict[str, Any]] = []
    for row in rows:
        item = _catalog_row_to_dict(row[:9])
        item['fuzzy_score'] = _best_title_similarity(query_key, [item.get('primary_title'), item.get('original_title')])
        scored.append(item)
    scored.sort(key=lambda item: (item.get('fuzzy_score') or 0.0, item.get('num_votes') or 0, item.get('start_year') or 0), reverse=True)
    return [item for item in scored if (item.get('fuzzy_score') or 0.0) >= 0.55][:limit]

def _best_title_similarity(query_key: str, variants: list[Any]) -> float:
    query_tokens = _match_tokens(query_key)
    best = 0.0
    for variant in variants:
        for variant_key in {_normalize_match_key(variant), _normalize_match_key(variant, strip_leading_articles=True)}:
            if not variant_key:
                continue
            sequence_score = difflib.SequenceMatcher(a=query_key, b=variant_key).ratio()
            token_score = _token_similarity_score(query_key, variant_key)
            if len(query_tokens) > 1:
                score = sequence_score * 0.6 + token_score * 0.4
            else:
                score = max(sequence_score, token_score)
            if variant_key.startswith(query_key) or query_key.startswith(variant_key):
                score = max(score, 0.8)
            best = max(best, score)
    return best

def _best_person_name_similarity(query_key: str, primary_name: Any) -> float:
    """Return the best fuzzy score for a person name across full-name and token variants."""
    name_key = _normalize_match_key(primary_name)
    if not query_key or not name_key:
        return 0.0
    name_tokens = _match_tokens(name_key)
    variants: list[str] = [name_key, name_key.replace(' ', '')]
    variants.extend(name_tokens)
    if len(name_tokens) > 1:
        variants.append(name_tokens[-1])
    seen: set[str] = set()
    ordered_variants: list[str] = []
    for variant in variants:
        if variant and variant not in seen:
            seen.add(variant)
            ordered_variants.append(variant)
    return _best_title_similarity(query_key, ordered_variants)

def _token_similarity_score(query_key: str, variant_key: str) -> float:
    query_tokens = _match_tokens(query_key)
    variant_tokens = _match_tokens(variant_key)
    if not query_tokens or not variant_tokens:
        return 0.0
    per_token_scores: list[float] = []
    for query_token in query_tokens:
        best_score = 0.0
        for variant_token in variant_tokens:
            best_score = max(best_score, difflib.SequenceMatcher(a=query_token, b=variant_token).ratio())
        per_token_scores.append(best_score)
    base_score = sum(per_token_scores) / len(per_token_scores)
    ordered_token_bonus = 0.05 if _tokens_are_subsequence(query_tokens, variant_tokens) else 0.0
    return min(1.0, base_score + ordered_token_bonus)

def _match_tokens(value: str) -> list[str]:
    return [token for token in value.split(' ') if token]

def _tokens_are_subsequence(needle: list[str], haystack: list[str]) -> bool:
    if not needle:
        return False
    index = 0
    for token in haystack:
        if token == needle[index]:
            index += 1
            if index == len(needle):
                return True
    return False

def _search_catalog_for_lookup(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    rows = fetch_catalog_search_rows(query=query, title_type=title_type, limit=limit)
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _catalog_row_to_dict(row)
        items.append(item)
    return items

def _fetch_title_people(conn, tconst: str) -> dict[str, list[dict[str, Any]]]:
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
    labels = {'movie': 'Movie', 'tvMovie': 'TV Movie', 'tvSeries': 'TV Series', 'tvMiniSeries': 'TV Mini Series'}
    return labels.get(title_type or '', 'Title')

def upsert_tmdb_mapping(tconst: str, tmdb_media_type: str, tmdb_id: int, matched_by: str, sync_status: str, last_error: str | None=None) -> None:
    upsert_tmdb_mapping_record(tconst=tconst, tmdb_media_type=tmdb_media_type, tmdb_id=tmdb_id, matched_by=matched_by, sync_status=sync_status, matched_at=_now_iso(), last_error=last_error)
    invalidate_title_presentation_cache(tconst)
    return

def store_tmdb_payloads(tconst: str, locale: str, detail_payload: dict[str, Any], provider_payload: dict[str, Any] | None) -> None:
    poster_path = detail_payload.get('poster_path')
    backdrop_path = detail_payload.get('backdrop_path')
    display_title = detail_payload.get('title') or detail_payload.get('name')
    release_date = detail_payload.get('release_date') or detail_payload.get('first_air_date')
    genres_json = json.dumps(detail_payload.get('genres', []), ensure_ascii=False)
    raw_json = json.dumps(detail_payload, ensure_ascii=False)
    providers = provider_payload.get('results', {}).get('CZ', {}) if provider_payload else {}
    synced_at = _now_iso()
    store_tmdb_payload_bundle(tconst=tconst, locale=locale, display_title=display_title, original_title=detail_payload.get('original_title') or detail_payload.get('original_name'), overview=detail_payload.get('overview'), poster_path=poster_path, backdrop_path=backdrop_path, release_date=release_date, genres_json=genres_json, raw_json=raw_json, synced_at=synced_at, providers=[{'provider_type': provider_type, 'provider_id': provider.get('provider_id'), 'provider_name': provider.get('provider_name'), 'logo_path': provider.get('logo_path'), 'display_priority': provider.get('display_priority')} for provider_type in ('flatrate', 'rent', 'buy', 'ads') for provider in providers.get(provider_type, [])])
    invalidate_title_presentation_cache(tconst)
    return

def get_tmdb_mapping(tconst: str) -> dict[str, Any] | None:
    return fetch_tmdb_mapping_record(tconst)

def record_tmdb_asset(tconst: str, asset_kind: str, relative_path: str, local_path: str, fetch_reason: str, status: str, sha256: str | None) -> dict[str, Any]:
    asset_id = str(uuid.uuid4())
    fetched_at = _now_iso()
    insert_tmdb_asset_record(asset_id=asset_id, tconst=tconst, asset_kind=asset_kind, relative_path=relative_path, local_path=local_path, fetch_reason=fetch_reason, status=status, sha256=sha256, fetched_at=fetched_at)
    invalidate_title_presentation_cache(tconst)
    return {'id': asset_id, 'tconst': tconst, 'asset_kind': asset_kind, 'relative_path': relative_path, 'local_path': local_path, 'fetch_reason': fetch_reason, 'status': status, 'sha256': sha256, 'fetched_at': fetched_at}

def get_latest_tmdb_assets(tconst: str) -> list[dict[str, Any]]:
    return fetch_latest_tmdb_assets_for_title(tconst)

def get_tmdb_detail_locales(tconst: str) -> list[str]:
    ui_config = get_ui_config()
    primary_locale, fallback_locale = ui_config.tmdb_locale_order
    snapshot = fetch_tmdb_payload_snapshot(tconst, primary_locale=primary_locale, fallback_locale=fallback_locale)
    return [] if snapshot is None else list(snapshot['detail_locales'])

def get_tmdb_asset_summary(tconst: str) -> dict[str, dict[str, Any]]:
    latest_by_kind: dict[str, dict[str, Any]] = {}
    for asset in get_latest_tmdb_assets(tconst):
        asset_kind = asset['asset_kind']
        if asset_kind in latest_by_kind:
            continue
        resolved_local_path = _resolve_tmdb_asset_local_path(asset)
        latest_by_kind[asset_kind] = {
            'status': asset.get('status'),
            'local_path': resolved_local_path,
            'exists': bool(resolved_local_path and Path(resolved_local_path).exists()),
            'fetched_at': asset.get('fetched_at'),
        }
    return latest_by_kind

def get_latest_poster_records(tconsts: list[str]) -> dict[str, dict[str, Any]]:
    clean_tconsts = [str(tconst).strip() for tconst in tconsts if str(tconst).strip()]
    if not clean_tconsts:
        return {}
    records: dict[str, dict[str, Any]] = {}
    for tconst in clean_tconsts:
        for asset in get_latest_tmdb_assets(tconst):
            if str(asset.get('asset_kind')) != 'poster':
                continue
            if str(asset.get('status')) != 'fetched':
                continue
            records[tconst] = {'poster_relative_path': asset.get('relative_path'), 'poster_local_path': asset.get('local_path')}
            break
    return records

def get_tmdb_enrichment_targets(limit: int | None=None, include_complete: bool=True, priority_tconsts: list[str] | None=None) -> list[dict[str, Any]]:
    items = _get_runtime_postgres_candidate_items(None)
    if priority_tconsts:
        priority_items = _get_priority_tmdb_target_items(None, priority_tconsts)
        existing = {item['tconst'] for item in items}
        items = [item for item in priority_items if item['tconst'] not in existing] + items
    ui_config = get_ui_config()
    primary_locale, fallback_locale = ui_config.tmdb_locale_order
    flags_by_tconst = fetch_tmdb_completion_flags([str(item['tconst']) for item in items], primary_locale=primary_locale, fallback_locale=fallback_locale)
    filtered_items: list[dict[str, Any]] = []
    for item in items:
        tconst = str(item['tconst'])
        flags = flags_by_tconst.get(tconst)
        if flags is not None and str(flags.get('sync_status') or '') == 'not_found':
            continue
        is_complete = _tmdb_flags_indicate_complete(flags, primary_locale=primary_locale, fallback_locale=fallback_locale)
        if include_complete or not is_complete:
            filtered_items.append(item)
    items = filtered_items
    if limit is not None:
        items = items[:limit]
    return items

def get_tmdb_target_counts() -> tuple[int, int]:
    """Return total candidate count and how many are already complete."""
    total = len(get_tmdb_enrichment_targets(include_complete=True))
    remaining = len(get_tmdb_enrichment_targets(include_complete=False))
    return (total, total - remaining)

def _tmdb_status_is_complete(tconst: str) -> bool:
    mapping = get_tmdb_mapping(tconst)
    if mapping is None:
        return False
    locales = set(get_tmdb_detail_locales(tconst))
    detail = ((get_content_detail(tconst) or {}).get('tmdb') or {}).get('details') or {}
    assets = get_tmdb_asset_summary(tconst)
    has_locales = 'en-US' in locales and 'cs-CZ' in locales
    poster_ok = not detail.get('poster_path') or bool((assets.get('poster') or {}).get('exists'))
    backdrop_ok = not detail.get('backdrop_path') or bool((assets.get('backdrop') or {}).get('exists'))
    return has_locales and poster_ok and backdrop_ok

def _tmdb_flags_indicate_complete(flags: dict[str, Any] | None, *, primary_locale: str, fallback_locale: str) -> bool:
    if flags is None:
        return False
    has_locales = bool(flags.get('has_primary')) and bool(flags.get('has_fallback'))
    poster_ok = not flags.get('poster_path') or bool(flags.get('has_poster'))
    backdrop_ok = not flags.get('backdrop_path') or bool(flags.get('has_backdrop'))
    return has_locales and poster_ok and backdrop_ok

def _get_tmdb_postgres_runtime_items(conn, *, include_complete: bool) -> list[dict[str, Any]]:
    runtime_items = _get_runtime_postgres_candidate_items(conn)
    if not runtime_items:
        return []
    candidate_rows: list[tuple[str, str, int]] = []
    for item in runtime_items:
        for reason in item.get('reasons') or []:
            candidate_rows.append((str(item['tconst']), str(reason), int(item['priority'])))
    input_sql = ' UNION ALL '.join(('SELECT ? AS target_tconst, ? AS reason, ? AS priority' for _ in candidate_rows))
    sql = f"\n        WITH pg_candidates AS (\n            {input_sql}\n        ),\n        candidates AS (\n            SELECT\n                c.target_tconst,\n                c.reason,\n                c.priority\n            FROM pg_candidates AS c\n        ),\n        ranked AS (\n            SELECT\n                target_tconst,\n                MIN(priority) AS priority,\n                string_agg(DISTINCT reason, ', ' ORDER BY reason) AS reasons\n            FROM candidates\n            WHERE target_tconst IS NOT NULL\n            GROUP BY 1\n        ),\n        detail_flags AS (\n            SELECT\n                tconst,\n                MAX(CASE WHEN locale = 'en-US' THEN 1 ELSE 0 END) AS has_en,\n                MAX(CASE WHEN locale = 'cs-CZ' THEN 1 ELSE 0 END) AS has_cs,\n                MAX(CASE WHEN locale = 'en-US' THEN poster_path WHEN locale = 'cs-CZ' THEN poster_path ELSE NULL END) AS poster_path,\n                MAX(CASE WHEN locale = 'en-US' THEN backdrop_path WHEN locale = 'cs-CZ' THEN backdrop_path ELSE NULL END) AS backdrop_path\n            FROM app.tmdb_title_details\n            GROUP BY 1\n        ),\n        asset_flags AS (\n            SELECT\n                tconst,\n                MAX(CASE WHEN asset_kind = 'poster' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_poster,\n                MAX(CASE WHEN asset_kind = 'backdrop' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_backdrop\n            FROM app.tmdb_assets\n            GROUP BY 1\n        )\n        SELECT\n            r.target_tconst,\n            t.title_type,\n            t.primary_title,\n            t.start_year,\n            r.priority,\n            r.reasons\n        FROM ranked AS r\n        JOIN app.catalog_titles AS t ON t.tconst = r.target_tconst\n        LEFT JOIN detail_flags AS d ON d.tconst = r.target_tconst\n        LEFT JOIN asset_flags AS a ON a.tconst = r.target_tconst\n        LEFT JOIN app.tmdb_title_map AS m ON m.tconst = r.target_tconst\n        WHERE (\n            ? = TRUE\n            OR NOT (\n                COALESCE(d.has_en, 0) = 1\n                AND COALESCE(d.has_cs, 0) = 1\n                AND (COALESCE(d.poster_path, '') = '' OR COALESCE(a.has_poster, 0) = 1)\n                AND (COALESCE(d.backdrop_path, '') = '' OR COALESCE(a.has_backdrop, 0) = 1)\n            )\n        )\n          AND COALESCE(m.sync_status, '') <> 'not_found'\n        ORDER BY r.priority, t.start_year DESC NULLS LAST, t.primary_title\n    "
    params: list[Any] = []
    for row in candidate_rows:
        params.extend(row)
    params.append(include_complete)
    rows = conn.execute(sql, params).fetchall()
    return [{'tconst': row[0], 'title_type': row[1], 'primary_title': row[2], 'start_year': row[3], 'priority': row[4], 'reasons': row[5].split(', ') if row[5] else []} for row in rows]

def _get_priority_tmdb_target_items(conn, priority_tconsts: Sequence[str]) -> list[dict[str, Any]]:
    priority_set = [str(tconst).strip() for tconst in priority_tconsts if str(tconst).strip()]
    if not priority_set:
        return []
    rows = fetch_catalog_brief_rows(priority_set)
    return [{'tconst': row[0], 'title_type': row[1], 'primary_title': row[2], 'start_year': row[3], 'priority': 0, 'reasons': ['search_target']} for row in rows]

def _merge_tmdb_target_items(primary: Sequence[dict[str, Any]], secondary: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for source in (primary, secondary):
        for item in source:
            tconst = str(item['tconst'])
            existing = merged.get(tconst)
            if existing is None:
                merged[tconst] = {**item, 'reasons': list(item.get('reasons') or [])}
                continue
            existing['priority'] = min(int(existing['priority']), int(item['priority']))
            existing['reasons'] = sorted(set(existing.get('reasons') or []).union(item.get('reasons') or []))
            if existing.get('start_year') is None and item.get('start_year') is not None:
                existing['start_year'] = item['start_year']
            if not existing.get('primary_title') and item.get('primary_title'):
                existing['primary_title'] = item['primary_title']
            if not existing.get('title_type') and item.get('title_type'):
                existing['title_type'] = item['title_type']
    return sorted(merged.values(), key=_tmdb_target_sort_key)

def _merge_runtime_candidate_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in rows:
        tconst = str(item['tconst'])
        existing = merged.get(tconst)
        if existing is None:
            merged[tconst] = {'tconst': tconst, 'priority': int(item['priority']), 'reasons': list(item.get('reasons') or [])}
            continue
        existing['priority'] = min(int(existing['priority']), int(item['priority']))
        existing['reasons'] = sorted(set(existing.get('reasons') or []).union(item.get('reasons') or []))
    return list(merged.values())

def _catalog_title_rows_by_tconsts(conn, tconsts: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not tconsts:
        return {}
    rows = fetch_catalog_brief_rows([str(tconst) for tconst in tconsts])
    return {str(row[0]): {'tconst': str(row[0]), 'title_type': row[1], 'primary_title': row[2], 'start_year': row[3]} for row in rows}

def _episode_series_map(conn, tconsts: Sequence[str]) -> dict[str, str]:
    if not tconsts:
        return {}
    return fetch_episode_series_map([str(tconst) for tconst in tconsts])

def _compute_actor_affinity_scores(conn, tconsts: Sequence[str]) -> dict[str, float]:
    if not tconsts:
        return {}
    affinities = fetch_positive_person_affinities()
    if not affinities:
        return {}
    title_placeholders = ', '.join(('?' for _ in tconsts))
    person_ids = sorted(affinities)
    person_placeholders = ', '.join(('?' for _ in person_ids))
    rows = conn.execute(f"\n        SELECT tconst, nconst, ordering\n        FROM app.title_credits\n        WHERE credit_group = 'cast'\n          AND tconst IN ({title_placeholders})\n          AND nconst IN ({person_placeholders})\n        ", [*tconsts, *person_ids]).fetchall()
    totals: dict[str, tuple[float, float]] = {}
    for tconst, nconst, ordering in rows:
        weight = 1.0 if ordering is None or ordering <= 0 else 1.0 / sqrt(float(ordering))
        current_sum, current_weight = totals.get(str(tconst), (0.0, 0.0))
        totals[str(tconst)] = (current_sum + float(affinities[str(nconst)]) * weight, current_weight + weight)
    return {tconst: score_sum / weight_sum for tconst, (score_sum, weight_sum) in totals.items() if weight_sum > 0}

def _get_runtime_postgres_candidate_items(conn) -> list[dict[str, Any]]:
    candidate_rows: list[dict[str, Any]] = []
    event_tconsts = sorted({str(event['tconst']) for event in fetch_all_watch_events() if event.get('tconst')})
    title_rows = _catalog_title_rows_by_tconsts(conn, event_tconsts)
    for tconst in title_rows:
        candidate_rows.append({'tconst': tconst, 'priority': 1, 'reasons': ['watched_title']})
    series_map = _episode_series_map(conn, event_tconsts)
    for series_tconst in sorted(set(series_map.values())):
        candidate_rows.append({'tconst': series_tconst, 'priority': 1, 'reasons': ['watched_series']})
    state_tconsts = sorted({str(state['tconst']) for state in list_in_progress_content_states(limit=None) if state.get('tconst')})
    title_rows = _catalog_title_rows_by_tconsts(conn, state_tconsts)
    for tconst in title_rows:
        candidate_rows.append({'tconst': tconst, 'priority': 2, 'reasons': ['in_progress_title']})
    series_map = _episode_series_map(conn, state_tconsts)
    for series_tconst in sorted(set(series_map.values())):
        candidate_rows.append({'tconst': series_tconst, 'priority': 2, 'reasons': ['in_progress_series']})
    list_kind_by_id = {str(item['id']): str(item['list_kind']) for item in fetch_user_lists()}
    active_items = fetch_active_user_list_items()
    item_tconsts = sorted({str(item['tconst']) for item in active_items if item.get('tconst')})
    title_rows = _catalog_title_rows_by_tconsts(conn, item_tconsts)
    series_map = _episode_series_map(conn, item_tconsts)
    for item in active_items:
        tconst = item.get('tconst')
        if not tconst:
            continue
        tconst = str(tconst)
        list_kind = list_kind_by_id.get(str(item['list_id']))
        source_origin = str(item.get('source_origin') or '')
        if list_kind == 'watchlist':
            priority = 3
            direct_reason = 'watchlist'
            series_reason = 'watchlist_series_from_episode'
        elif source_origin == 'seed_plex_library':
            priority = 3
            direct_reason = 'plex_library'
            series_reason = 'plex_library_series_from_episode'
        elif list_kind == 'custom':
            priority = 4
            direct_reason = 'custom_list'
            series_reason = 'custom_list_series_from_episode'
        else:
            continue
        if tconst in title_rows:
            candidate_rows.append({'tconst': tconst, 'priority': priority, 'reasons': [direct_reason]})
        series_tconst = series_map.get(tconst)
        if series_tconst:
            candidate_rows.append({'tconst': series_tconst, 'priority': priority, 'reasons': [series_reason]})
    merged = _merge_runtime_candidate_rows(candidate_rows)
    title_meta = _catalog_title_rows_by_tconsts(conn, [item['tconst'] for item in merged])
    return [{**item, **title_meta[item['tconst']]} for item in merged if item['tconst'] in title_meta]

def _tmdb_target_sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
    start_year = item.get('start_year')
    return (int(item.get('priority') or 0), 1 if start_year is None else 0, -(int(start_year) if start_year is not None else 0), str(item.get('primary_title') or ''))

def get_title_detail_cache_targets(limit: int | None=None, include_ready: bool=False) -> list[dict[str, Any]]:
    candidate_items = get_tmdb_enrichment_targets(include_complete=True)
    ui_config = get_ui_config()
    primary_locale, fallback_locale = ui_config.tmdb_locale_order
    flags_by_tconst = fetch_tmdb_completion_flags([str(item['tconst']) for item in candidate_items], primary_locale=primary_locale, fallback_locale=fallback_locale)
    items: list[dict[str, Any]] = []
    for item in candidate_items:
        tconst = str(item['tconst'])
        if not _tmdb_flags_indicate_complete(flags_by_tconst.get(tconst), primary_locale=primary_locale, fallback_locale=fallback_locale):
            continue
        cache_path = _title_detail_cache_path(tconst)
        if not cache_path.exists():
            cache_status = 'missing'
        else:
            try:
                cached = json.loads(cache_path.read_text(encoding='utf-8'))
                cache_status = 'ready' if isinstance(cached, dict) and cached.get('tconst') == tconst else 'invalid'
            except (OSError, json.JSONDecodeError):
                cache_status = 'invalid'
        if cache_status == 'ready' and (not include_ready):
            continue
        items.append({'tconst': item['tconst'], 'title_type': item['title_type'], 'primary_title': item['primary_title'], 'start_year': item['start_year'], 'priority': item['priority'], 'reasons': item.get('reasons') or [], 'cache_status': cache_status, 'cache_path': cache_path.as_posix()})
        if limit is not None and len(items) >= limit:
            break
    return items

def _get_relevant_people_candidates(limit: int | None=None) -> list[dict[str, Any]]:
    main_cast_limit = 8
    return fetch_relevant_people_candidate_rows(main_cast_limit=main_cast_limit, limit=limit)

def get_person_detail_cache_targets(limit: int | None=None, include_ready: bool=False) -> list[dict[str, Any]]:
    candidates = _get_relevant_people_candidates(limit=limit)
    if not candidates:
        return []
    items: list[dict[str, Any]] = []
    for row in candidates:
        nconst = str(row['nconst'])
        fingerprint = _person_cache_source_fingerprint(None, nconst)
        cache_status = 'missing'
        if fingerprint is not None:
            cache_status = _person_detail_cache_status(nconst, fingerprint)
        if not include_ready and cache_status == 'ready':
            continue
        items.append({'nconst': nconst, 'name': row['name'], 'birth_year': row['birth_year'], 'primary_profession': row['primary_profession'], 'credit_count': row['credit_count'], 'group_priority': row['group_priority'], 'cache_status': cache_status})
    return items

def create_import_preview(source: str, filename: str, content: bytes, max_rows: int | None=None) -> dict[str, Any]:
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
    batch = fetch_import_batch_record(batch_id)
    if batch is None:
        return None
    return {**batch, 'rows': fetch_import_batch_rows(batch_id, limit=100)}

def commit_import_batch(batch_id: str) -> dict[str, Any]:
    batch = fetch_import_batch_record(batch_id)
    if batch is None:
        raise ValueError('Import batch neexistuje.')
    if batch['status'] == 'committed':
        return {'batch_id': batch_id, 'committed': 0, 'status': 'already_committed'}
    result = commit_import_batch_postgres(batch_id=batch_id, committed_at=_now_iso())
    return {'batch_id': batch_id, 'committed': int(result['inserted_events']), 'skipped': int(result['skipped_events']), 'status': str(result['batch_status'])}

def inspect_trakt_export(export_dir: str='trakt-export') -> dict[str, Any]:
    from filmy.db_legacy import inspect_trakt_export as _impl
    return _impl(export_dir)

def sync_trakt_export(export_dir: str='trakt-export') -> dict[str, Any]:
    from filmy.db_legacy import sync_trakt_export as _impl
    return _impl(export_dir)

def get_trakt_sync_runs(limit: int=20) -> list[dict[str, Any]]:
    from filmy.db_legacy import get_trakt_sync_runs as _impl
    return _impl(limit=limit)

def get_trakt_sync_run(sync_run_id: str) -> dict[str, Any] | None:
    from filmy.db_legacy import get_trakt_sync_run as _impl
    return _impl(sync_run_id)

def get_trakt_sync_changes(sync_run_id: str | None=None, previous_sync_id: str | None=None, limit: int=100) -> dict[str, Any]:
    from filmy.db_legacy import get_trakt_sync_changes as _impl
    return _impl(sync_run_id=sync_run_id, previous_sync_id=previous_sync_id, limit=limit)

def get_watch_history(limit: int=100, source: str | None=None) -> list[dict[str, Any]]:
    return fetch_watch_history_postgres(limit=limit, source=source)
RECENTLY_WATCHED_VIEW_ID = 'view:recently-watched'
WATCHED_VIEW_ID = 'view:watched'
HOT_WATCHLIST_VIEW_ID = 'view:hot-watchlist'

def _fetch_watch_view_page(limit: int, offset: int, *, cutoff_days: int | None) -> dict[str, Any]:
    return _fetch_watch_view_page_from_postgres(limit, offset, cutoff_days=cutoff_days)

def _fetch_watch_view_page_from_postgres(limit: int, offset: int, *, cutoff_days: int | None) -> dict[str, Any]:
    total, rows = fetch_watch_view_page_rows(limit=limit, offset=offset, cutoff_days=cutoff_days)
    items = [{'tconst': row[0], 'title_type': row[1], 'title': row[2], 'year': row[3], 'season_number': None, 'episode_number': None, 'series_title': None, 'poster_url': _poster_url_from_local_path(row[4] or row[5]), 'last_watched_on': row[6], 'last_watched_at': row[7], 'end_year': None, 'runtime_minutes': None} for row in rows]
    return {'total': total, 'items': items, 'limit': limit, 'offset': offset}

def get_recently_watched_page(limit: int=50, offset: int=0) -> dict[str, Any]:
    from filmy.db_library import get_recently_watched_page as _impl
    return _impl(limit=limit, offset=offset)

def get_watched_page(limit: int=50, offset: int=0) -> dict[str, Any]:
    from filmy.db_library import get_watched_page as _impl
    return _impl(limit=limit, offset=offset)

def get_trakt_ratings(limit: int=100, active_only: bool=True) -> list[dict[str, Any]]:
    from filmy.db_legacy import get_trakt_ratings as _impl
    return _impl(limit=limit, active_only=active_only)

def get_trakt_list_overview(include_items: bool=False, active_only: bool=True) -> dict[str, Any]:
    from filmy.db_legacy import get_trakt_list_overview as _impl
    return _impl(include_items=include_items, active_only=active_only)

def get_trakt_collection(limit: int=100, active_only: bool=True) -> list[dict[str, Any]]:
    from filmy.db_legacy import get_trakt_collection as _impl
    return _impl(limit=limit, active_only=active_only)

def get_trakt_status() -> dict[str, Any]:
    from filmy.db_legacy import get_trakt_status as _impl
    return _impl()

def inspect_imdb_lists(export_dir: str='imdb_lists') -> dict[str, Any]:
    from filmy.db_legacy import inspect_imdb_lists as _impl
    return _impl(export_dir)

def sync_imdb_lists(export_dir: str='imdb_lists') -> dict[str, Any]:
    from filmy.db_legacy import sync_imdb_lists as _impl
    return _impl(export_dir)

def get_imdb_lists_status() -> dict[str, Any]:
    from filmy.db_legacy import get_imdb_lists_status as _impl
    return _impl()

def get_imdb_watchlist(limit: int=100, active_only: bool=True) -> list[dict[str, Any]]:
    from filmy.db_legacy import get_imdb_watchlist as _impl
    return _impl(limit=limit, active_only=active_only)

def get_imdb_favorite_people(limit: int=100, active_only: bool=True) -> list[dict[str, Any]]:
    from filmy.db_legacy import get_imdb_favorite_people as _impl
    return _impl(limit=limit, active_only=active_only)

def inspect_plex_source() -> dict[str, Any]:
    from filmy.db_legacy import inspect_plex_source as _impl
    return _impl()

def sync_plex_source(section_limit: int | None=None, item_limit_per_section: int | None=None) -> dict[str, Any]:
    from filmy.db_legacy import sync_plex_source as _impl
    return _impl(section_limit=section_limit, item_limit_per_section=item_limit_per_section)

def get_plex_status() -> dict[str, Any]:
    from filmy.db_legacy import get_plex_status as _impl
    return _impl()

def _upsert_plex_library_item(conn, sync_run_id: str, section: dict[str, Any], snapshot: dict[str, Any]) -> None:
    from filmy.db_legacy import _upsert_plex_library_item as _impl
    return _impl(conn, sync_run_id, section, snapshot)

def _sync_plex_item_to_local_library(conn, list_id: str, sync_run_id: str, snapshot: dict[str, Any], now: str) -> bool:
    from filmy.db_legacy import _sync_plex_item_to_local_library as _impl
    return _impl(conn, list_id, sync_run_id, snapshot, now)

def _sync_plex_watch_state(conn, snapshot: dict[str, Any]) -> bool:
    from filmy.db_legacy import _sync_plex_watch_state as _impl
    return _impl(conn, snapshot)

def _sync_plex_content_state(conn, snapshot: dict[str, Any], now: str) -> bool:
    from filmy.db_legacy import _sync_plex_content_state as _impl
    return _impl(conn, snapshot, now)

def get_local_library_status() -> dict[str, Any]:
    from filmy.db_library import get_local_library_status as _impl
    return _impl()

def _poster_url_from_detail(detail: dict[str, Any] | None) -> str | None:
    tmdb = (detail or {}).get('tmdb') or {}
    poster_asset = _latest_tmdb_asset_by_kind(tmdb.get('assets') or [], 'poster')
    local_path = _resolve_tmdb_asset_local_path(poster_asset)
    if not local_path:
        return None
    return _poster_url_from_local_path(local_path)

def _backdrop_url_from_detail(detail: dict[str, Any] | None) -> str | None:
    tmdb = (detail or {}).get('tmdb') or {}
    backdrop_asset = _latest_tmdb_asset_by_kind(tmdb.get('assets') or [], 'backdrop')
    local_path = _resolve_tmdb_asset_local_path(backdrop_asset)
    if not local_path:
        return None
    return _poster_url_from_local_path(local_path)

def _latest_tmdb_asset_by_kind(assets: list[dict[str, Any]], asset_kind: str) -> dict[str, Any] | None:
    for asset in assets:
        if str(asset.get('asset_kind')) == asset_kind:
            return asset
    return None

def _resolve_tmdb_asset_local_path(asset: dict[str, Any] | None) -> str | None:
    if not asset:
        return None
    local_path_value = str(asset.get('local_path') or '').strip()
    relative_path_value = str(asset.get('relative_path') or '').strip()
    if local_path_value:
        local_path = Path(local_path_value)
        if local_path.exists():
            return local_path.as_posix()
    if relative_path_value:
        relative_path = Path(relative_path_value)
        candidate = ASSETS_DIR / relative_path
        if candidate.exists():
            return candidate.as_posix()
    return local_path_value or ((ASSETS_DIR / Path(relative_path_value)).as_posix() if relative_path_value else None)

def _poster_url_from_local_path(local_path_value: str | None) -> str | None:
    return _asset_url_from_local_path(local_path_value, assets_root=ASSETS_DIR, mount_path='/assets/tmdb')

def _asset_url_from_local_path(local_path_value: str | None, *, assets_root: Path, mount_path: str) -> str | None:
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
    from filmy.db_library import get_continue_watching_items as _impl
    return _impl(limit=limit)

def get_hot_watchlist_page(limit: int=50, offset: int=0) -> dict[str, Any]:
    from filmy.db_library import get_hot_watchlist_page as _impl
    return _impl(limit=limit, offset=offset)

def get_user_list_items_page(list_id: str, limit: int=50, offset: int=0) -> dict[str, Any]:
    from filmy.db_library import get_user_list_items_page as _impl
    return _impl(list_id, limit=limit, offset=offset)

def get_user_list_items(list_id: str, limit: int=12) -> list[dict[str, Any]]:
    from filmy.db_library import get_user_list_items as _impl
    return _impl(list_id, limit=limit)

def format_czech_datetime(value: Any) -> str | None:
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
    from filmy.db_legacy import _sync_trakt_history as _impl
    return _impl(conn, sync_run_id, files)

def _sync_trakt_ratings(conn, sync_run_id: str, files: list[dict[str, Any]]) -> dict[str, int]:
    from filmy.db_legacy import _sync_trakt_ratings as _impl
    return _impl(conn, sync_run_id, files)

def _sync_trakt_lists(conn, sync_run_id: str, metadata_files: list[dict[str, Any]], custom_list_files: list[dict[str, Any]], watchlist_files: list[dict[str, Any]]) -> dict[str, int]:
    from filmy.db_legacy import _sync_trakt_lists as _impl
    return _impl(conn, sync_run_id, metadata_files, custom_list_files, watchlist_files)

def _sync_trakt_collection(conn, sync_run_id: str, files: list[dict[str, Any]]) -> dict[str, int]:
    from filmy.db_legacy import _sync_trakt_collection as _impl
    return _impl(conn, sync_run_id, files)

def _read_last_activities(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    from filmy.db_legacy import _read_last_activities as _impl
    return _impl(files)

def _get_catalog_refresh_state(conn) -> tuple[bool, bool]:
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
    now = _now_iso()
    rows = [{'source_key': source.key, 'source_path': source.path.as_posix(), 'source_mtime': source.stat_mtime, 'source_size': source.stat_size, 'source_sha256': source.sha256, 'recorded_at': now} for source in SOURCE_FILES]
    replace_imdb_manifest_rows(rows)
    return

def _store_catalog_refresh_meta(conn) -> None:
    replace_catalog_refresh_meta_rows([{'source_key': source.key, 'fingerprint': f'{source.stat_mtime}:{source.stat_size}'} for source in SOURCE_FILES])
    return

def _ensure_user_list(conn, list_id: str, name: str, list_kind: str, source_origin: str, source_ref: str, now: str, *, description: str | None=None, preferred_slug: str | None=None) -> str:
    slug = preferred_slug or _slugify(name) or list_id
    conn.execute('\n        INSERT INTO app.user_lists (id, slug, name, description, list_kind, source_origin, source_ref, created_at, updated_at)\n        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n        ON CONFLICT (id) DO UPDATE SET\n            slug = excluded.slug,\n            name = excluded.name,\n            description = CASE\n                WHEN excluded.description IS NOT NULL THEN excluded.description\n                ELSE app.user_lists.description\n            END,\n            list_kind = excluded.list_kind,\n            updated_at = excluded.updated_at\n        ', [list_id, slug, name, description, list_kind, source_origin, source_ref, now, now])
    return list_id

def _upsert_user_list_item(conn, *, list_id: str, canonical_key: str, tconst: str | None, media_type: str, imdb_id: str | None, tmdb_id: int | None, trakt_id: int | None, parent_tconst: str | None, parent_title: str | None, title: str | None, season_number: int | None, episode_number: int | None, rank: int | None, added_at: str | None, notes: str | None, source_origin: str, source_ref: str | None, now: str) -> None:
    item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{list_id}|{canonical_key}'))
    conn.execute('\n        INSERT INTO app.user_list_items (\n            id, list_id, canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title,\n            title, season_number, episode_number, rank, added_at, notes, source_origin, source_ref,\n            is_archived, created_at, updated_at\n        )\n        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, ?, ?)\n        ON CONFLICT (list_id, canonical_key) DO UPDATE SET\n            tconst = COALESCE(app.user_list_items.tconst, excluded.tconst),\n            imdb_id = COALESCE(app.user_list_items.imdb_id, excluded.imdb_id),\n            tmdb_id = COALESCE(app.user_list_items.tmdb_id, excluded.tmdb_id),\n            trakt_id = COALESCE(app.user_list_items.trakt_id, excluded.trakt_id),\n            parent_tconst = COALESCE(app.user_list_items.parent_tconst, excluded.parent_tconst),\n            parent_title = COALESCE(app.user_list_items.parent_title, excluded.parent_title),\n            title = COALESCE(app.user_list_items.title, excluded.title),\n            rank = COALESCE(app.user_list_items.rank, excluded.rank),\n            added_at = CASE\n                WHEN app.user_list_items.is_archived THEN COALESCE(excluded.added_at, app.user_list_items.added_at)\n                ELSE COALESCE(app.user_list_items.added_at, excluded.added_at)\n            END,\n            notes = COALESCE(app.user_list_items.notes, excluded.notes),\n            is_archived = FALSE,\n            updated_at = excluded.updated_at\n        ', [item_id, list_id, canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title, title, season_number, episode_number, rank, added_at, notes, source_origin, source_ref, now, now])

def _upsert_user_rating(conn, *, canonical_key: str, tconst: str | None, media_type: str, imdb_id: str | None, tmdb_id: int | None, trakt_id: int | None, parent_tconst: str | None, parent_title: str | None, title: str | None, season_number: int | None, episode_number: int | None, rating: int, rated_at: str | None, source_origin: str, source_ref: str | None, now: str, liked_notes: str | None=None, disliked_notes: str | None=None) -> None:
    conn.execute('\n        INSERT INTO app.user_ratings (\n            canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title, title,\n            season_number, episode_number, rating, liked_notes, disliked_notes, rated_at,\n            source_origin, source_ref, created_at, updated_at\n        )\n        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n        ON CONFLICT (canonical_key) DO UPDATE SET\n            tconst = COALESCE(app.user_ratings.tconst, excluded.tconst),\n            imdb_id = COALESCE(app.user_ratings.imdb_id, excluded.imdb_id),\n            tmdb_id = COALESCE(app.user_ratings.tmdb_id, excluded.tmdb_id),\n            trakt_id = COALESCE(app.user_ratings.trakt_id, excluded.trakt_id),\n            parent_tconst = COALESCE(app.user_ratings.parent_tconst, excluded.parent_tconst),\n            parent_title = COALESCE(app.user_ratings.parent_title, excluded.parent_title),\n            title = COALESCE(app.user_ratings.title, excluded.title),\n            rating = excluded.rating,\n            liked_notes = excluded.liked_notes,\n            disliked_notes = excluded.disliked_notes,\n            rated_at = COALESCE(excluded.rated_at, app.user_ratings.rated_at),\n            updated_at = excluded.updated_at\n        ', [canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title, title, season_number, episode_number, rating, liked_notes, disliked_notes, rated_at, source_origin, source_ref, now, now])

def _catalog_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {'tconst': row[0], 'title_type': row[1], 'primary_title': row[2], 'original_title': row[3], 'start_year': row[4], 'end_year': row[5] if len(row) > 9 else None, 'runtime_minutes': row[6] if len(row) > 9 else row[5], 'genres': (row[7] if len(row) > 9 else row[6]).split(',') if (row[7] if len(row) > 9 else row[6]) else [], 'average_rating': row[8] if len(row) > 9 else row[7], 'num_votes': row[9] if len(row) > 9 else row[8]}

def _build_local_media_identity(detail: dict[str, Any]) -> dict[str, Any]:
    if detail['kind'] == 'episode':
        return {'tconst': detail['tconst'], 'media_type': 'episode', 'imdb_id': detail['tconst'], 'tmdb_id': (detail.get('tmdb') or {}).get('tmdb_id'), 'parent_tconst': detail.get('series_tconst'), 'parent_title': None, 'title': detail.get('primary_title'), 'season_number': detail.get('season_number'), 'episode_number': detail.get('episode_number')}
    return {'tconst': detail['tconst'], 'media_type': 'title', 'imdb_id': detail['tconst'], 'tmdb_id': (detail.get('tmdb') or {}).get('tmdb_id'), 'parent_tconst': None, 'parent_title': None, 'title': detail.get('primary_title'), 'season_number': None, 'episode_number': None}

def _get_library_summary_for_tconst(tconst: str) -> dict[str, Any]:
    title = fetch_catalog_title_row(tconst)
    if title is not None:
        return _fetch_library_summary(None, tconst, title[1])
    episode = fetch_catalog_episode_row(tconst)
    if episode is not None:
        return _fetch_library_summary(None, tconst, 'tvEpisode')
    raise ValueError('Titul nebyl nalezen.')

def _fetch_aliases(conn, tconst: str) -> list[dict[str, Any]]:
    rows = fetch_title_alias_rows(tconst, limit=20)
    return [{'title': row[0], 'region': row[1], 'language': row[2], 'types': row[3], 'is_original_title': row[4]} for row in rows]

def _fetch_tmdb(conn, tconst: str) -> dict[str, Any] | None:
    ui_config = get_ui_config()
    primary_locale, fallback_locale = ui_config.tmdb_locale_order
    snapshot = fetch_tmdb_payload_snapshot(tconst, primary_locale=primary_locale, fallback_locale=fallback_locale)
    if snapshot is None:
        return None
    mapping = snapshot['mapping']
    return {'media_type': mapping['tmdb_media_type'], 'tmdb_id': mapping['tmdb_id'], 'matched_by': mapping['matched_by'], 'matched_at': mapping['matched_at'], 'sync_status': mapping['sync_status'], 'last_error': mapping['last_error'], 'details': snapshot['details'], 'detail_locales': snapshot['detail_locales'], 'providers': snapshot['providers'], 'assets': snapshot['assets']}

def _fetch_content_state(conn, tconst: str) -> dict[str, Any] | None:
    state = fetch_content_state_postgres(tconst)
    if state is None:
        return None
    return {'interest_state': state['interest_state'], 'last_previewed_at': state['last_previewed_at'], 'last_watched_at': state['last_watched_at'], 'updated_at': state['updated_at']}

def _fetch_library_summary(conn, tconst: str, title_type: str | None) -> dict[str, Any]:
    return fetch_library_summary_snapshot(tconst, title_type)

def _fetch_watch_stats_from_postgres(tconst: str, title_type: str | None) -> tuple[int, datetime | None]:
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
    reader = csv.DictReader(io.StringIO(text))
    if source == 'netflix':
        return [_parse_netflix_row(row) for row in reader]
    if source == 'trakt':
        return [_parse_trakt_row(row) for row in reader]
    raise ValueError(f'Nepodporovaný source: {source}')

def _parse_netflix_row(row: dict[str, str | None]) -> dict[str, Any]:
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
    title = (row.get('title') or row.get('Title') or '').strip()
    year = _safe_int(row.get('year') or row.get('Year'))
    watched_on = (row.get('watched_at') or row.get('Watched At') or row.get('watched_on') or '').strip()
    season_number = _safe_int(row.get('season') or row.get('Season'))
    episode_number = _safe_int(row.get('episode') or row.get('Episode'))
    imdb_id = (row.get('imdb_id') or row.get('IMDb ID') or '').strip() or None
    tmdb_id = _safe_int(row.get('tmdb_id') or row.get('TMDB ID'))
    return {'parsed_title': title, 'parsed_year': year, 'parsed_watched_on': watched_on or None, 'parsed_season_number': season_number, 'parsed_episode_number': episode_number, 'parsed_imdb_id': imdb_id, 'parsed_tmdb_id': tmdb_id, 'raw': row}

def _resolve_import_row_postgres(source: str, row: dict[str, Any], resolver_cache: dict[tuple[Any, ...], dict[str, Any]] | None=None, resolution_context: dict[str, Any] | None=None) -> dict[str, Any]:
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
    if len(title) < 6:
        return None
    for idx in range(len(title) - 5):
        chunk = title[idx:idx + 6]
        if chunk.startswith('(') and chunk.endswith(')') and chunk[1:5].isdigit():
            return int(chunk[1:5])
    return None

def _extract_season_number(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search('season\\s+(\\d+)', value, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def _extract_episode_number(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch('episode\\s+(\\d+)', value.strip(), flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def _parse_netflix_date(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ('%m/%d/%y', '%m/%d/%Y'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value

def _safe_int(value: Any) -> int | None:
    if value in (None, '', '\\N'):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None

def _parse_unix_timestamp(value: Any) -> str | None:
    parsed = _safe_int(value)
    if parsed is None:
        return None
    return datetime.fromtimestamp(parsed, UTC).isoformat()

def _safe_float(value: Any) -> float | None:
    if value in (None, '', '\\N'):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None

def _parse_iso_date(value: Any) -> str | None:
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
    return _normalize_match_key(value).replace(' ', '-')

def _plex_source_key(rating_key: str) -> str:
    return f'plex:{rating_key}'

def _plex_item_is_watched(snapshot: dict[str, Any]) -> bool:
    view_count = _safe_int(snapshot.get('view_count')) or 0
    viewed_leaf_count = _safe_int(snapshot.get('viewed_leaf_count')) or 0
    leaf_count = _safe_int(snapshot.get('leaf_count')) or 0
    return view_count > 0 or (leaf_count > 0 and viewed_leaf_count >= leaf_count)

def _plex_item_is_in_progress(snapshot: dict[str, Any]) -> bool:
    if _plex_item_is_watched(snapshot):
        return False
    viewed_leaf_count = _safe_int(snapshot.get('viewed_leaf_count')) or 0
    leaf_count = _safe_int(snapshot.get('leaf_count')) or 0
    return leaf_count > 0 and viewed_leaf_count > 0

def _plex_fingerprint(server_client_identifier: str, sections: list[dict[str, Any]]) -> str:
    payload = {'server_client_identifier': server_client_identifier, 'sections': [{'key': section.get('key'), 'type': section.get('type'), 'title': section.get('title'), 'updatedAt': section.get('updatedAt'), 'scannedAt': section.get('scannedAt'), 'contentChangedAt': section.get('contentChangedAt')} for section in sections]}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

def _canonical_media_key(media_type: str | None, tconst: str | None, imdb_id: str | None, tmdb_id: int | None, trakt_id: int | None, season_number: int | None, episode_number: int | None) -> str:
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
    if resolver_cache is not None:
        resolver_cache[cache_key] = resolution
    return resolution

def _build_resolution_context_postgres(source: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
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
    title = (row.get('parsed_title') or '').strip().lower()
    title_key = _normalize_match_key(row.get('parsed_title'))
    if not title:
        return None
    return resolution_context['title_map'].get(title) or resolution_context['normalized_title_map'].get(title_key)

def _build_netflix_alias_context_postgres(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    title_keys = sorted({_normalize_match_key(row.get('parsed_title')) for row in rows if row.get('parsed_title')})
    if not title_keys:
        return None
    title_map = fetch_title_alias_lookup_matches(title_keys)
    if not title_map:
        return None
    return {'title_map': title_map}

def _resolve_netflix_alias_resolution_postgres(row: dict[str, Any], alias_context: dict[str, Any]) -> dict[str, Any] | None:
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
    raw_path = Path(export_dir)
    path = raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path
    path = path.resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f'Adresář s Trakt exportem neexistuje: {path}')
    return path

def _sync_imdb_watchlist(conn, sync_run_id: str, file_info: dict[str, Any] | None) -> dict[str, int]:
    from filmy.db_legacy import _sync_imdb_watchlist as _impl
    return _impl(conn, sync_run_id, file_info)

def _sync_imdb_favorite_people(conn, sync_run_id: str, file_info: dict[str, Any] | None) -> dict[str, int]:
    from filmy.db_legacy import _sync_imdb_favorite_people as _impl
    return _impl(conn, sync_run_id, file_info)

def _loads_json_or_none(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)

def _dumps_json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)

def _fetch_change_rows(conn, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    rows = conn.execute(sql, params).fetchall()
    return [{'entity_id': row[0], 'media_type': row[1], 'parent_title': row[2], 'title': row[3], 'changed_at': row[4]} for row in rows]

def _trakt_list_item_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {'source_key': row[0], 'media_type': row[1], 'imdb_id': row[2], 'tmdb_id': row[3], 'tconst': row[4], 'parent_title': row[5], 'title': row[6], 'season_number': row[7], 'episode_number': row[8], 'rank': row[9], 'listed_at': row[10], 'notes': row[11], 'my_rating': row[12], 'is_active': row[13]}

def _has_trakt_snapshot(conn, table_name: str, sync_run_id: str) -> bool:
    return conn.execute(f'SELECT COUNT(*) FROM {table_name} WHERE sync_run_id = ?', [sync_run_id]).fetchone()[0] > 0

def _backfill_trakt_snapshots_for_run(conn, sync_run_id: str) -> None:
    if not _has_trakt_snapshot(conn, 'old.trakt_history_snapshot', sync_run_id):
        conn.execute('\n            INSERT INTO old.trakt_history_snapshot (sync_run_id, history_id)\n            SELECT ?, history_id\n            FROM old.trakt_history_events\n            WHERE is_active = TRUE AND last_seen_sync_id = ?\n            ', [sync_run_id, sync_run_id])
    if not _has_trakt_snapshot(conn, 'old.trakt_ratings_snapshot', sync_run_id):
        conn.execute('\n            INSERT INTO old.trakt_ratings_snapshot (sync_run_id, source_key)\n            SELECT ?, source_key\n            FROM old.trakt_ratings\n            WHERE is_active = TRUE AND last_seen_sync_id = ?\n            ', [sync_run_id, sync_run_id])
    if not _has_trakt_snapshot(conn, 'old.trakt_list_items_snapshot', sync_run_id):
        conn.execute('\n            INSERT INTO old.trakt_list_items_snapshot (sync_run_id, source_key)\n            SELECT ?, source_key\n            FROM old.trakt_list_items\n            WHERE is_active = TRUE AND last_seen_sync_id = ?\n            ', [sync_run_id, sync_run_id])
    if not _has_trakt_snapshot(conn, 'old.trakt_collection_snapshot', sync_run_id):
        conn.execute('\n            INSERT INTO old.trakt_collection_snapshot (sync_run_id, source_key)\n            SELECT ?, source_key\n            FROM old.trakt_collection_items\n            WHERE is_active = TRUE AND last_seen_sync_id = ?\n            ', [sync_run_id, sync_run_id])

def _snapshot_change_count(conn, snapshot_table: str, key_column: str, left_sync_id: str, right_sync_id: str) -> int:
    return conn.execute(f'\n        SELECT COUNT(*)\n        FROM {snapshot_table} AS cur\n        LEFT JOIN {snapshot_table} AS prev\n          ON prev.sync_run_id = ? AND prev.{key_column} = cur.{key_column}\n        WHERE cur.sync_run_id = ? AND prev.{key_column} IS NULL\n        ', [right_sync_id, left_sync_id]).fetchone()[0]

def _snapshot_change_rows(conn, snapshot_table: str, snapshot_key_column: str, entity_table: str, entity_key_column: str, media_type_column: str, parent_title_column: str, title_column: str, changed_at_column: str, left_sync_id: str, right_sync_id: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(f'\n        SELECT\n            ent.{entity_key_column} AS entity_id,\n            ent.{media_type_column} AS media_type,\n            ent.{parent_title_column} AS parent_title,\n            ent.{title_column} AS title,\n            ent.{changed_at_column} AS changed_at\n        FROM {snapshot_table} AS cur\n        LEFT JOIN {snapshot_table} AS prev\n          ON prev.sync_run_id = ? AND prev.{snapshot_key_column} = cur.{snapshot_key_column}\n        JOIN {entity_table} AS ent\n          ON ent.{entity_key_column} = cur.{snapshot_key_column}\n        WHERE cur.sync_run_id = ? AND prev.{snapshot_key_column} IS NULL\n        ORDER BY ent.{changed_at_column} DESC NULLS LAST\n        LIMIT ?\n        ', [right_sync_id, left_sync_id, limit]).fetchall()
    return [{'entity_id': row[0], 'media_type': row[1], 'parent_title': row[2], 'title': row[3], 'changed_at': row[4]} for row in rows]

def _describe_trakt_file(path: Path) -> dict[str, Any]:
    payload = _load_json_file(path)
    stat = path.stat()
    return {'name': path.name, 'path': path.as_posix(), 'relative_path': path.name, 'category': _categorize_trakt_file(path.name), 'item_count': len(payload) if isinstance(payload, list) else 1, 'size': stat.st_size, 'mtime': int(stat.st_mtime), 'sha256': _file_sha256(path)}

def _categorize_trakt_file(name: str) -> str:
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
    if name == 'watchlist.csv':
        return 'watchlist'
    if name == 'favorite_person.csv':
        return 'favorite_people'
    return 'ignored'

def _fingerprint_trakt_files(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(f"{item['relative_path']}|{item['size']}|{item['mtime']}|{item['sha256']}\n".encode('utf-8'))
    return digest.hexdigest()

def _load_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))

def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))

def _count_csv_rows(path: Path) -> int:
    return len(_read_csv_rows(path))

def _parse_trakt_list_id_from_filename(name: str) -> str:
    match = re.match('lists-list-(\\d+)-', name)
    return match.group(1) if match else 'unknown'

def _extract_trakt_media(item: dict[str, Any]) -> dict[str, Any]:
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
    return path.resolve().as_posix().replace("'", "''")

def _normalize_match_key(value: Any, *, strip_leading_articles: bool=False) -> str:
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
    return datetime.now(UTC).isoformat()
