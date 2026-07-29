"""Lokalni knihovna, seznamy a watched/read model helpery."""

from __future__ import annotations

"""Local-library DB operations extracted from the `filmy.db` facade.

The goal is to separate mutations and list-oriented read models from the very
large legacy module while preserving the existing public API. Runtime imports
back to `filmy.db` are deliberate here: they let us reuse stable internal
helpers first and only later decide which helpers deserve their own dedicated
module.
"""
import importlib
import threading
import time
from copy import deepcopy
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from filmy.runtime_postgres import apply_title_session_effects, archive_user_list_group, archive_user_list_item, clear_ai_suggestions_list_items as clear_ai_suggestions_list_items_postgres, create_user_list as create_user_list_postgres, delete_user_list as delete_user_list_postgres, delete_title_role_signals as delete_title_role_signals_postgres, fetch_all_watch_events, fetch_existing_watch_tconsts, fetch_active_user_list_items, fetch_continue_watching_catalog_rows, fetch_episode_series_map, fetch_hot_watchlist_page_rows, fetch_library_status_projection, fetch_library_status_snapshot, fetch_list_action_rules, fetch_person_affinity_rating, fetch_person_catalog_row, fetch_series_episode_rows, fetch_title_card_rows, fetch_title_role_signals as fetch_title_role_signals_postgres, fetch_user_list, fetch_user_list_item_counts, fetch_user_list_page_rows, fetch_user_lists, finalize_title_session, delete_user_rating as delete_user_rating_postgres, fetch_latest_ratings_for_tconsts, insert_title_session_action, insert_watch_events as insert_watch_events_postgres, list_in_progress_content_states, queue_title_session_action_effects, record_watched as record_watched_postgres, slug_exists, upsert_person_affinity, upsert_title_session, upsert_title_role_signal, upsert_user_list_item, upsert_user_rating as upsert_user_rating_postgres, update_content_state as update_content_state_postgres, update_user_list_description as update_user_list_description_postgres

def _db():
    """Vrat pozde nacitany modul `filmy.db` kvuli stabilni facade vrstve."""
    return importlib.import_module('filmy.db')


def _invalidate_title_cache(db: Any, *tconsts: str | None) -> None:
    """Invaliduj title presentation cache pro dotcene tituly nebo globalne."""
    seen: set[str] = set()
    for tconst in tconsts:
        cleaned = (tconst or '').strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        invalidate = getattr(db, 'invalidate_title_presentation_cache', None)
        if invalidate is not None:
            invalidate(cleaned)
        else:
            db.clear_title_presentation_cache()
    if not seen:
        db.clear_title_presentation_cache()


def _resolve_existing_list_id(list_id: str | None) -> str | None:
    """Over a vrat existujici list id, jinak `None`."""

    cleaned = (list_id or '').strip()
    if not cleaned:
        return None
    row = fetch_user_list(cleaned)
    if row is None:
        return None
    return str(row['id'])


def _infer_source_list_id(*, source_list_id: str | None=None, return_to_url: str | None=None) -> str | None:
    """Najdi zdrojovy seznam z explicitni hodnoty nebo z `return_to` URL."""

    resolved_explicit = _resolve_existing_list_id(source_list_id)
    if resolved_explicit is not None:
        return resolved_explicit
    pending = [(return_to_url or '').strip()]
    visited: set[str] = set()
    while pending:
        raw_value = pending.pop(0)
        if not raw_value or raw_value in visited:
            continue
        visited.add(raw_value)
        decoded_value = unquote(raw_value)
        if decoded_value != raw_value and decoded_value not in visited:
            pending.append(decoded_value)
        parsed = urlparse(decoded_value)
        path_parts = [part for part in parsed.path.split('/') if part]
        if len(path_parts) >= 2 and path_parts[0] == 'lists':
            resolved = _resolve_existing_list_id(path_parts[1])
            if resolved is not None:
                return resolved
        query = parse_qs(parsed.query, keep_blank_values=False)
        for candidate_key in ('source_list_id', 'list_id'):
            for value in query.get(candidate_key, []):
                resolved = _resolve_existing_list_id(value)
                if resolved is not None:
                    return resolved
        for nested_value in query.get('return_to', []):
            if nested_value not in visited:
                pending.append(nested_value)
    return None


def _build_title_session_media_payload(
    *,
    media: dict[str, Any],
    canonical_key: str,
    source_ref: str,
    source_origin: str='local_app',
) -> dict[str, Any]:
    """Sestav sdileny action payload pro title-session effecty."""

    return {
        'canonical_key': canonical_key,
        'tconst': media['tconst'],
        'media_type': media['media_type'],
        'imdb_id': media['imdb_id'],
        'tmdb_id': media['tmdb_id'],
        'trakt_id': None,
        'parent_tconst': media['parent_tconst'],
        'parent_title': media['parent_title'],
        'title': media['title'],
        'season_number': media['season_number'],
        'episode_number': media['episode_number'],
        'source_origin': source_origin,
        'source_ref': source_ref,
    }


def _build_title_session_group_payload(
    *,
    items: list[dict[str, Any]],
    display_tconst: str,
    source_ref_prefix: str,
) -> dict[str, Any]:
    """Sestav action payload pro copy/move cele display skupiny."""

    group_items: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        added_at = item.get('added_at')
        group_items.append(
            {
                'item_id': f'{source_ref_prefix}:item:{index}',
                'canonical_key': str(item['canonical_key']),
                'tconst': item.get('tconst'),
                'media_type': str(item['media_type']),
                'imdb_id': item.get('imdb_id'),
                'tmdb_id': item.get('tmdb_id'),
                'trakt_id': item.get('trakt_id'),
                'parent_tconst': item.get('parent_tconst'),
                'parent_title': item.get('parent_title'),
                'title': item.get('title'),
                'season_number': item.get('season_number'),
                'episode_number': item.get('episode_number'),
                'rank': item.get('rank'),
                'added_at': added_at.isoformat() if added_at else None,
                'notes': item.get('notes'),
                'source_origin': str(item.get('source_origin') or 'title_session'),
                'source_ref': item.get('source_ref') or source_ref_prefix,
            }
        )
    representative = group_items[0]
    return {
        **representative,
        'tconst': display_tconst,
        'display_tconst': display_tconst,
        'group_items': group_items,
        'group_size': len(group_items),
        'source_ref': source_ref_prefix,
    }


def _count_effect_results(effect_rows: list[dict[str, Any]], effect_type: str, result: str='applied') -> int:
    """Spocitej, kolik effectu daneho typu skoncilo zvolenym vysledkem."""

    return sum(
        1
        for row in effect_rows
        if row.get('result') == result and str((row.get('effect') or {}).get('effect_type') or '') == effect_type
    )


def _upsert_group_items_to_user_list(*, db: Any, items: list[dict[str, Any]], target_list_id: str, now: str) -> None:
    """Zapis vsechny polozky jedne display skupiny do ciloveho seznamu."""

    for item in items:
        added_at = item.get('added_at')
        upsert_user_list_item(
            item_id=str(db.uuid.uuid4()),
            list_id=target_list_id,
            canonical_key=str(item['canonical_key']),
            tconst=item.get('tconst'),
            media_type=str(item['media_type']),
            imdb_id=item.get('imdb_id'),
            tmdb_id=item.get('tmdb_id'),
            trakt_id=item.get('trakt_id'),
            parent_tconst=item.get('parent_tconst'),
            parent_title=item.get('parent_title'),
            title=item.get('title'),
            season_number=item.get('season_number'),
            episode_number=item.get('episode_number'),
            rank=item.get('rank'),
            added_at=added_at.isoformat() if added_at else None,
            notes=item.get('notes'),
            source_origin=str(item['source_origin']),
            source_ref=item.get('source_ref'),
            now=now,
        )


def _archive_group_items_from_user_list(items: list[dict[str, Any]], *, source_list_id: str, now: str) -> None:
    """Deaktivuj vsechny polozky jedne display skupiny ve zdrojovem seznamu."""

    for item in items:
        archive_user_list_item(source_list_id, str(item['canonical_key']), now)


def _run_title_session_action(
    *,
    db: Any,
    tconst: str,
    trigger_action: str,
    action_payload: dict[str, Any],
    now: str,
    source_list_id: str | None=None,
    return_to_url: str | None=None,
    target_list_id: str | None=None,
    rating_value: int | None=None,
    notes_text: str | None=None,
    auto_finalize: bool=True,
    session_scope: str='title_detail',
) -> dict[str, Any] | None:
    """Zkus provest zapis pres title-session workflow, jinak vrat `None`."""

    resolved_source_list_id = _infer_source_list_id(
        source_list_id=source_list_id,
        return_to_url=return_to_url,
    )
    if resolved_source_list_id is None:
        return None
    rules = fetch_list_action_rules(
        source_list_id=resolved_source_list_id,
        trigger_action=trigger_action,
        target_list_id=target_list_id,
        enabled_only=True,
    )
    if not rules:
        return None
    session_id = f'title-session:{db.uuid.uuid4()}'
    action_id = f'title-session-action:{db.uuid.uuid4()}'
    session = upsert_title_session(
        session_id=session_id,
        tconst=tconst,
        status='open',
        opened_from=resolved_source_list_id,
        return_to_url=return_to_url,
        source_list_id=resolved_source_list_id,
        session_scope=session_scope,
        started_at=now,
    )
    action = insert_title_session_action(
        action_id=action_id,
        session_id=session_id,
        tconst=tconst,
        source_list_id=resolved_source_list_id,
        trigger_action=trigger_action,
        target_list_id=target_list_id,
        rating_value=rating_value,
        notes_text=notes_text,
        action_payload=action_payload,
        action_order=10,
        created_at=now,
    )
    queued = queue_title_session_action_effects(action_id, queued_at=now)
    immediate = apply_title_session_effects(
        session_id,
        phase='immediate',
        executed_at=now,
        effect_status='pending',
    )
    finalized = finalize_title_session(session_id, finalized_at=now) if auto_finalize else None
    return {
        'source_list_id': resolved_source_list_id,
        'session': session,
        'action': action,
        'queued': queued,
        'immediate': immediate,
        'finalized': finalized,
    }

_LOCAL_LIBRARY_STATUS_CACHE_TTL_SECONDS = 5.0
_local_library_status_cache_lock = threading.Lock()
_local_library_status_cache: dict[str, Any] | None = None
_local_library_status_cached_at = 0.0
AI_INPUT_ROLE_OPTIONS: tuple[dict[str, str], ...] = ({'value': 'strong_positive', 'label': 'Silně se mi líbí'}, {'value': 'interested_owned', 'label': 'Mám / dal jsem si s tím práci'}, {'value': 'interested_planned', 'label': 'Chci vidět / stojí za pozornost'}, {'value': 'in_progress', 'label': 'Rozkoukané'}, {'value': 'negative', 'label': 'Nelíbí / nechci podobné'}, {'value': 'external_suggestion', 'label': 'Návrh od AI'}, {'value': 'ignore', 'label': 'Nepoužívat pro AI'})
AI_INPUT_ROLE_VALUES = frozenset((option['value'] for option in AI_INPUT_ROLE_OPTIONS))
TITLE_ROLE_SIGNAL_TYPE_OPTIONS: tuple[dict[str, str], ...] = ({'value': 'character', 'label': 'Postava'}, {'value': 'dialogue', 'label': 'Dialogy'}, {'value': 'behavior', 'label': 'Chování'}, {'value': 'relationship_dynamic', 'label': 'Vztahová dynamika'}, {'value': 'performance', 'label': 'Herecké provedení'}, {'value': 'visual_appeal', 'label': 'Vzhled'}, {'value': 'attraction', 'label': 'Přitažlivost'}, {'value': 'other', 'label': 'Jiné'})
TITLE_ROLE_SIGNAL_TYPE_VALUES = frozenset((option['value'] for option in TITLE_ROLE_SIGNAL_TYPE_OPTIONS))
TITLE_ROLE_SIGNAL_POLARITY_OPTIONS: tuple[dict[str, str], ...] = ({'value': 'positive', 'label': 'Pozitivní'}, {'value': 'negative', 'label': 'Negativní'}, {'value': 'mixed', 'label': 'Smíšené'})
TITLE_ROLE_SIGNAL_POLARITY_VALUES = frozenset((option['value'] for option in TITLE_ROLE_SIGNAL_POLARITY_OPTIONS))


class LocalLibraryReadModelSupport:
    """Drzi sdilenou logiku pro cache a listove read modely lokalni knihovny."""

    def get_cached_status(self) -> dict[str, Any] | None:
        """Vrat cached snapshot lokalni knihovny, pokud jeste neexpirval."""
        with _local_library_status_cache_lock:
            if _local_library_status_cache is None or time.time() - _local_library_status_cached_at > _LOCAL_LIBRARY_STATUS_CACHE_TTL_SECONDS:
                return None
            return deepcopy(_local_library_status_cache)

    def store_cached_status(self, value: dict[str, Any]) -> None:
        """Uloz novy snapshot lokalni knihovny do kratke in-memory cache."""
        global _local_library_status_cache, _local_library_status_cached_at
        with _local_library_status_cache_lock:
            _local_library_status_cache = deepcopy(value)
            _local_library_status_cached_at = time.time()

    def order_group_items_for_list(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Serad polozky skupiny pro stabilni zobrazeni v UI."""
        ordered = list(items)
        ordered.sort(key=lambda item: item.get('title') or item.get('parent_title') or item.get('tconst') or '')
        ordered.sort(key=lambda item: item.get('added_at') or datetime.min, reverse=True)
        ordered.sort(key=lambda item: (item.get('rank') is None, item.get('rank') if item.get('rank') is not None else 0))
        return ordered

    def load_episode_series_map(self, conn: Any, tconsts: list[str]) -> dict[str, str]:
        """Nacti mapovani epizoda -> serial pro zadane tituly."""
        if not tconsts:
            return {}
        return fetch_episode_series_map(tconsts)

    def load_watched_display_tconsts(self, conn: Any) -> set[str]:
        """Vrat mnozinu display tconstu, ktere jsou uz povazovane za zhlednute."""
        watched_tconsts = sorted({str(item['tconst']) for item in fetch_all_watch_events() if item.get('tconst')})
        if not watched_tconsts:
            return set()
        episode_series_map = self.load_episode_series_map(conn, watched_tconsts)
        return {episode_series_map.get(tconst, tconst) for tconst in watched_tconsts}

    def group_postgres_list_items(self, conn: Any, *, list_id: str, exclude_watched: bool = False) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Seskup aktivni PG list polozky pod jeden display titul."""
        list_row = fetch_user_list(list_id)
        if list_row is None:
            return (None, [])
        active_items = [item for item in fetch_active_user_list_items() if item['list_id'] == list_id]
        if not active_items:
            return (list_row, [])
        tconsts = [str(item['tconst']) for item in active_items if item.get('tconst')]
        episode_series_map = self.load_episode_series_map(conn, tconsts)
        watched_display_tconsts = self.load_watched_display_tconsts(conn) if exclude_watched else set()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in active_items:
            display_tconst = (episode_series_map.get(str(item['tconst'])) if item.get('tconst') else None) or item.get('tconst') or item.get('parent_tconst')
            if not display_tconst:
                continue
            if exclude_watched and display_tconst in watched_display_tconsts:
                continue
            item_copy = dict(item)
            item_copy['display_tconst'] = str(display_tconst)
            grouped.setdefault(str(display_tconst), []).append(item_copy)
        groups: list[dict[str, Any]] = []
        for display_tconst, items in grouped.items():
            ordered_items = self.order_group_items_for_list(items)
            representative = ordered_items[0]
            groups.append({'display_tconst': display_tconst, 'media_type': representative.get('media_type'), 'title': representative.get('title'), 'parent_title': representative.get('parent_title'), 'season_number': None, 'episode_number': None, 'rank': representative.get('rank'), 'added_at': representative.get('added_at'), 'notes': representative.get('notes'), 'list_name': list_row['name'], 'list_kind': list_row['list_kind']})
        return (list_row, self.order_group_items_for_list(groups))

    def load_group_cards(self, conn: Any, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Dopln k listovym skupinam lehka kartova metadata titulu."""
        if not groups:
            return []
        display_tconsts = [str(group['display_tconst']) for group in groups]
        rows = fetch_title_card_rows(display_tconsts)
        cards_by_tconst = {str(row[0]): {'title_type': row[1], 'year': row[2], 'resolved_title': row[3], 'poster_relative_path': row[4], 'poster_local_path': row[5]} for row in rows if row[4] or row[5]}
        return [group | cards_by_tconst[group['display_tconst']] for group in groups if group['display_tconst'] in cards_by_tconst]

    def get_group_items_for_list(self, conn: Any, *, list_id: str, display_tconst: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Vrat konkretni aktivni polozky, ktere patri do jedne display skupiny."""
        list_row = fetch_user_list(list_id)
        if list_row is None:
            return (None, [])
        active_items = [item for item in fetch_active_user_list_items() if item['list_id'] == list_id]
        tconsts = [str(item['tconst']) for item in active_items if item.get('tconst')]
        episode_series_map = self.load_episode_series_map(conn, tconsts)
        matching_items: list[dict[str, Any]] = []
        for item in active_items:
            item_display_tconst = (episode_series_map.get(str(item['tconst'])) if item.get('tconst') else None) or item.get('tconst') or item.get('parent_tconst')
            if str(item_display_tconst) != str(display_tconst):
                continue
            matching_items.append(dict(item))
        return (list_row, self.order_group_items_for_list(matching_items))


_READ_MODELS = LocalLibraryReadModelSupport()


def _get_cached_local_library_status() -> dict[str, Any] | None:
    """Kompatibilni wrapper pro kratkou cache lokalniho library snapshotu."""
    return _READ_MODELS.get_cached_status()

def _store_cached_local_library_status(value: dict[str, Any]) -> None:
    """Kompatibilni wrapper pro ulozeni library snapshotu do cache."""
    _READ_MODELS.store_cached_status(value)

def _order_group_items_for_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Kompatibilni wrapper pro razeni polozek jedne listove skupiny."""
    return _READ_MODELS.order_group_items_for_list(items)

def _load_episode_series_map(conn, tconsts: list[str]) -> dict[str, str]:
    """Kompatibilni wrapper pro mapovani epizod na serialove display ID."""
    return _READ_MODELS.load_episode_series_map(conn, tconsts)

def _load_watched_display_tconsts(conn) -> set[str]:
    """Kompatibilni wrapper pro mnozinu jiz zhlednutych display titulu."""
    return _READ_MODELS.load_watched_display_tconsts(conn)

def _group_postgres_list_items(conn, *, list_id: str, exclude_watched: bool=False) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Kompatibilni wrapper pro seskupeni PG list polozek pod display titul."""
    return _READ_MODELS.group_postgres_list_items(conn, list_id=list_id, exclude_watched=exclude_watched)

def _load_group_cards(conn, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Kompatibilni wrapper pro doplneni karet ke skupinam listu."""
    return _READ_MODELS.load_group_cards(conn, groups)

def _get_postgres_group_items_for_list(conn, *, list_id: str, display_tconst: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Kompatibilni wrapper pro nacteni konkretni display skupiny ze seznamu."""
    return _READ_MODELS.get_group_items_for_list(conn, list_id=list_id, display_tconst=display_tconst)

def update_content_state(tconst: str, interest_state: str) -> dict[str, Any]:
    """Zmen interest/content state titulu a invaliduj jeho presentation cache."""
    db = _db()
    now = db._now_iso()
    result = update_content_state_postgres(tconst, interest_state, now)
    _invalidate_title_cache(db, tconst)
    return result

def set_watchlist_state(tconst: str, *, in_watchlist: bool, notes: str | None=None) -> dict[str, Any]:
    """Pridej nebo odeber titul z hlavniho watchlistu."""
    db = _db()
    detail = db.get_content_detail(tconst)
    if detail is None:
        raise ValueError('Titul nebyl nalezen.')
    now = db._now_iso()
    media = db._build_local_media_identity(detail)
    canonical_key = db._canonical_media_key(media['media_type'], media['tconst'], media['imdb_id'], media['tmdb_id'], None, media['season_number'], media['episode_number'])
    if in_watchlist:
        upsert_user_list_item(item_id=str(db.uuid.uuid4()), list_id='watchlist', canonical_key=canonical_key, tconst=media['tconst'], media_type=media['media_type'], imdb_id=media['imdb_id'], tmdb_id=media['tmdb_id'], trakt_id=None, parent_tconst=media['parent_tconst'], parent_title=media['parent_title'], title=media['title'], season_number=media['season_number'], episode_number=media['episode_number'], rank=None, added_at=now, notes=notes, source_origin='local_app', source_ref=f'manual_watchlist:{tconst}', now=now)
    else:
        archive_user_list_item('watchlist', canonical_key, now)
    _invalidate_title_cache(db, tconst, media.get('parent_tconst'))
    return {'tconst': tconst, 'in_watchlist': in_watchlist, 'updated_at': now, 'library': db._get_library_summary_for_tconst(tconst)}

def add_title_to_user_list(tconst: str, list_id: str, *, notes: str | None=None) -> dict[str, Any]:
    """Pridej titul do konkretniho uzivatelskeho seznamu."""
    db = _db()
    detail = db.get_content_detail(tconst)
    if detail is None:
        raise ValueError('Titul nebyl nalezen.')
    now = db._now_iso()
    media = db._build_local_media_identity(detail)
    canonical_key = db._canonical_media_key(media['media_type'], media['tconst'], media['imdb_id'], media['tmdb_id'], None, media['season_number'], media['episode_number'])
    target_list = fetch_user_list(list_id)
    if target_list is None:
        raise ValueError('Cílový seznam nebyl nalezen.')
    upsert_user_list_item(item_id=str(db.uuid.uuid4()), list_id=list_id, canonical_key=canonical_key, tconst=media['tconst'], media_type=media['media_type'], imdb_id=media['imdb_id'], tmdb_id=media['tmdb_id'], trakt_id=None, parent_tconst=media['parent_tconst'], parent_title=media['parent_title'], title=media['title'], season_number=media['season_number'], episode_number=media['episode_number'], rank=None, added_at=now, notes=notes, source_origin='local_app', source_ref=f'manual_add_to_list:{tconst}', now=now)
    _invalidate_title_cache(db, tconst, media.get('parent_tconst'))
    return {'tconst': tconst, 'list_id': list_id, 'updated_at': now, 'library': db._get_library_summary_for_tconst(tconst)}

def set_user_rating(tconst: str, rating: int, *, liked_notes: str | None=None, disliked_notes: str | None=None, source_list_id: str | None=None, return_to_url: str | None=None) -> dict[str, Any]:
    """Uloz lokalni rating titulu vcetne volitelnych slovnich poznamek."""
    db = _db()
    if rating < 1 or rating > 10:
        raise ValueError('Rating musí být mezi 1 a 10.')
    detail = db.get_content_detail(tconst)
    if detail is None:
        raise ValueError('Titul nebyl nalezen.')
    now = db._now_iso()
    media = db._build_local_media_identity(detail)
    existing_rating = (detail.get('library') or {}).get('rating') or {}
    cleaned_liked_notes = existing_rating.get('liked_notes') if liked_notes is None else ((liked_notes or '').strip() or None)
    cleaned_disliked_notes = existing_rating.get('disliked_notes') if disliked_notes is None else ((disliked_notes or '').strip() or None)
    canonical_key = db._canonical_media_key(media['media_type'], media['tconst'], media['imdb_id'], media['tmdb_id'], None, media['season_number'], media['episode_number'])
    action_payload = _build_title_session_media_payload(
        media=media,
        canonical_key=canonical_key,
        source_ref=f'manual_rating:{tconst}',
    ) | {
        'rating_value': rating,
        'rated_at': now,
        'liked_notes': cleaned_liked_notes,
        'disliked_notes': cleaned_disliked_notes,
    }
    session_result = _run_title_session_action(
        db=db,
        tconst=tconst,
        trigger_action='set_rating',
        action_payload=action_payload,
        now=now,
        source_list_id=source_list_id,
        return_to_url=return_to_url,
        rating_value=rating,
        notes_text=cleaned_liked_notes or cleaned_disliked_notes,
        auto_finalize=True,
    )
    if session_result is None:
        upsert_user_rating_postgres(canonical_key=canonical_key, tconst=media['tconst'], media_type=media['media_type'], imdb_id=media['imdb_id'], tmdb_id=media['tmdb_id'], trakt_id=None, parent_tconst=media['parent_tconst'], parent_title=media['parent_title'], title=media['title'], season_number=media['season_number'], episode_number=media['episode_number'], rating=rating, liked_notes=cleaned_liked_notes, disliked_notes=cleaned_disliked_notes, rated_at=now, source_origin='local_app', source_ref=f'manual_rating:{tconst}', now=now)
    _invalidate_title_cache(db, tconst, media.get('parent_tconst'))
    result = {'tconst': tconst, 'rating': rating, 'rated_at': now, 'library': db._get_library_summary_for_tconst(tconst)}
    if session_result is not None:
        result['session_id'] = session_result['session']['session_id']
        result['workflow'] = 'title_session'
    return result

def set_person_affinity_rating(nconst: str, rating: int) -> dict[str, Any]:
    """Uloz lokalni oblibenost osoby v rozsahu 0..10."""
    db = _db()
    if rating < 0 or rating > 10:
        raise ValueError('Rating musí být mezi 0 a 10.')
    row = fetch_person_catalog_row(nconst)
    if row is None:
        raise ValueError('Osoba nebyla nalezena.')
    now = db._now_iso()
    person_key = f'nconst:{nconst}'
    is_favorite = False
    created_at = now
    try:
        existing_rating = fetch_person_affinity_rating(nconst)
        if existing_rating > 0:
            created_at = now
    except Exception:
        pass
    upsert_person_affinity(person_key=person_key, nconst=row[0], name=row[1], known_for=row[2], birth_date=str(row[3]) if row[3] is not None else None, source_ref=f'manual_person_rating:{nconst}', is_favorite=is_favorite, affinity_rating=rating, created_at=created_at, updated_at=now)
    db.get_person_presentation(nconst)
    return {'nconst': nconst, 'rating': rating, 'updated_at': now}

def set_title_role_signal(tconst: str, *, nconst: str | None=None, character_name: str | None=None, signal_type: str='character', polarity: str='positive', strength: int=8, notes: str | None=None) -> dict[str, Any]:
    """Store a title-specific character/role signal separate from title rating."""
    db = _db()
    cleaned_tconst = (tconst or '').strip()
    cleaned_nconst = (nconst or '').strip() or None
    cleaned_character_name = (character_name or '').strip() or None
    cleaned_signal_type = (signal_type or '').strip() or 'character'
    cleaned_polarity = (polarity or '').strip() or 'positive'
    cleaned_notes = (notes or '').strip() or None
    try:
        cleaned_strength = int(strength)
    except (TypeError, ValueError) as exc:
        raise ValueError('Síla signálu musí být číslo mezi 0 a 10.') from exc
    if cleaned_strength < 0 or cleaned_strength > 10:
        raise ValueError('Síla signálu musí být mezi 0 a 10.')
    if cleaned_signal_type not in TITLE_ROLE_SIGNAL_TYPE_VALUES:
        raise ValueError('Neznámý typ signálu role/postavy.')
    if cleaned_polarity not in TITLE_ROLE_SIGNAL_POLARITY_VALUES:
        raise ValueError('Neznámá polarita signálu role/postavy.')
    if not cleaned_tconst:
        raise ValueError('Titul nebyl zadán.')
    if not fetch_title_card_rows([cleaned_tconst]):
        raise ValueError('Titul nebyl nalezen.')
    if cleaned_nconst is not None and fetch_person_catalog_row(cleaned_nconst) is None:
        raise ValueError('Osoba nebyla nalezena.')
    if cleaned_nconst is None and cleaned_character_name is None:
        raise ValueError('Zadej osobu nebo jméno postavy.')
    identity_part = cleaned_nconst or 'bez-osoby'
    character_part = db._slugify(cleaned_character_name or 'bez-postavy') or 'bez-postavy'
    signal_key = f'role-signal:{cleaned_tconst}:{identity_part}:{character_part}:{cleaned_signal_type}'
    now = db._now_iso()
    result = upsert_title_role_signal(signal_key=signal_key, tconst=cleaned_tconst, nconst=cleaned_nconst, character_name=cleaned_character_name, signal_type=cleaned_signal_type, polarity=cleaned_polarity, strength=cleaned_strength, notes=cleaned_notes, source_ref=f'manual_role_signal:{cleaned_tconst}', now=now)
    db.clear_title_presentation_cache()
    return result

def replace_title_role_signals(tconst: str, *, nconst: str | None=None, character_name: str | None=None, signal_types: list[str] | tuple[str, ...] | None=None, polarity: str='positive', strength: int=8, notes: str | None=None) -> dict[str, Any]:
    """Replace all checked signal types for one role/person in one title."""
    cleaned_types = tuple(dict.fromkeys(((value or '').strip() for value in signal_types or [] if (value or '').strip())))
    if not cleaned_types:
        raise ValueError('Vyber aspoň jeden typ signálu role/postavy.')
    unknown_types = [value for value in cleaned_types if value not in TITLE_ROLE_SIGNAL_TYPE_VALUES]
    if unknown_types:
        raise ValueError('Neznámý typ signálu role/postavy.')
    db = _db()
    cleaned_tconst = (tconst or '').strip()
    cleaned_nconst = (nconst or '').strip() or None
    cleaned_character_name = (character_name or '').strip() or None
    cleaned_polarity = (polarity or '').strip() or 'positive'
    cleaned_notes = (notes or '').strip() or None
    try:
        cleaned_strength = int(strength)
    except (TypeError, ValueError) as exc:
        raise ValueError('Síla signálu musí být číslo mezi 0 a 10.') from exc
    if cleaned_strength < 0 or cleaned_strength > 10:
        raise ValueError('Síla signálu musí být mezi 0 a 10.')
    if cleaned_polarity not in TITLE_ROLE_SIGNAL_POLARITY_VALUES:
        raise ValueError('Neznámá polarita signálu role/postavy.')
    if not cleaned_tconst:
        raise ValueError('Titul nebyl zadán.')
    if not fetch_title_card_rows([cleaned_tconst]):
        raise ValueError('Titul nebyl nalezen.')
    if cleaned_nconst is not None and fetch_person_catalog_row(cleaned_nconst) is None:
        raise ValueError('Osoba nebyla nalezena.')
    if cleaned_nconst is None and cleaned_character_name is None:
        raise ValueError('Zadej osobu nebo jméno postavy.')
    deleted_count = delete_title_role_signals_postgres(tconst=cleaned_tconst, nconst=cleaned_nconst, character_name=cleaned_character_name)
    saved = [set_title_role_signal(cleaned_tconst, nconst=cleaned_nconst, character_name=cleaned_character_name, signal_type=signal_type, polarity=cleaned_polarity, strength=cleaned_strength, notes=cleaned_notes) for signal_type in cleaned_types]
    db.clear_title_presentation_cache()
    return {'tconst': cleaned_tconst, 'nconst': cleaned_nconst, 'character_name': cleaned_character_name, 'signal_types': list(cleaned_types), 'saved': saved, 'deleted_count': deleted_count}

def delete_title_role_signals(tconst: str, *, nconst: str | None=None, character_name: str | None=None) -> dict[str, Any]:
    """Smaz vsechny signaly role/postavy pro vybrany titul a identitu."""
    db = _db()
    cleaned_tconst = (tconst or '').strip()
    cleaned_nconst = (nconst or '').strip() or None
    cleaned_character_name = (character_name or '').strip() or None
    if not cleaned_tconst:
        raise ValueError('Titul nebyl zadán.')
    if cleaned_nconst is None and cleaned_character_name is None:
        raise ValueError('Zadej osobu nebo jméno postavy.')
    deleted_count = delete_title_role_signals_postgres(tconst=cleaned_tconst, nconst=cleaned_nconst, character_name=cleaned_character_name)
    db.clear_title_presentation_cache()
    return {'tconst': cleaned_tconst, 'nconst': cleaned_nconst, 'character_name': cleaned_character_name, 'deleted_count': deleted_count}

def get_title_role_signals(tconst: str) -> list[dict[str, Any]]:
    """Vrat vsechny ulozene signaly role/postavy pro jeden titul."""
    cleaned_tconst = (tconst or '').strip()
    if not cleaned_tconst:
        return []
    return fetch_title_role_signals_postgres(cleaned_tconst)

def clear_user_rating(tconst: str) -> dict[str, Any]:
    """Smaz lokalni rating titulu a vrat aktualizovany library snapshot."""
    db = _db()
    detail = db.get_content_detail(tconst)
    if detail is None:
        raise ValueError('Titul nebyl nalezen.')
    media = db._build_local_media_identity(detail)
    canonical_key = db._canonical_media_key(media['media_type'], media['tconst'], media['imdb_id'], media['tmdb_id'], None, media['season_number'], media['episode_number'])
    now = db._now_iso()
    delete_user_rating_postgres(canonical_key)
    _invalidate_title_cache(db, tconst, media.get('parent_tconst'))
    return {'tconst': tconst, 'rating': None, 'updated_at': now, 'library': db._get_library_summary_for_tconst(tconst)}

def record_watch_event(tconst: str, *, watched_on: str | None=None, notes: str | None=None, add_to_watched_list: bool=False, archive_from_list_id: str | None=None, archive_display_tconst: str | None=None, return_to_url: str | None=None) -> dict[str, Any]:
    """Zapis jeden watch event a pripadne archivuj souvisejici list polozku."""
    db = _db()
    detail = db.get_content_detail(tconst)
    if detail is None:
        raise ValueError('Titul nebyl nalezen.')
    now = db._now_iso()
    event_id = str(db.uuid.uuid4())
    event_scope = 'episode' if detail['kind'] == 'episode' else 'title'
    effective_watched_on = watched_on or now[:10]
    try:
        db.datetime.strptime(effective_watched_on, '%Y-%m-%d')
    except ValueError as exc:
        raise ValueError('watched_on musí být ISO datum ve formátu YYYY-MM-DD.') from exc
    media = db._build_local_media_identity(detail)
    canonical_key = db._canonical_media_key(media['media_type'], media['tconst'], media['imdb_id'], media['tmdb_id'], None, media['season_number'], media['episode_number'])
    effective_archive_list_id = archive_from_list_id
    effective_archive_canonical_key: str | None = None
    if effective_archive_list_id:
        effective_archive_canonical_key = canonical_key
    elif detail['kind'] != 'episode':
        effective_archive_list_id = 'watchlist'
        effective_archive_canonical_key = canonical_key
    action_payload = _build_title_session_media_payload(
        media=media,
        canonical_key=canonical_key,
        source_ref=f'manual_watch:{tconst}',
    ) | {
        'event_id': event_id,
        'event_scope': event_scope,
        'watched_on': effective_watched_on,
        'notes': notes,
        'display_tconst': archive_display_tconst,
    }
    session_result = _run_title_session_action(
        db=db,
        tconst=tconst,
        trigger_action='mark_watched',
        action_payload=action_payload,
        now=now,
        source_list_id=effective_archive_list_id,
        return_to_url=return_to_url,
        notes_text=notes,
        auto_finalize=True,
    )
    if session_result is None:
        action_result = record_watched_postgres(event_id=event_id, tconst=tconst, event_scope=event_scope, watched_on=effective_watched_on, notes=notes, created_at=now, archive_from_list_id=effective_archive_list_id, archive_canonical_key=effective_archive_canonical_key, archive_display_tconst=archive_display_tconst)
        archived_items = action_result['archived_items']
    else:
        finalize_effects = ((session_result.get('finalized') or {}).get('finalize') or {}).get('effects') or []
        archived_items = _count_effect_results(finalize_effects, 'deactivate_source_membership')
    _invalidate_title_cache(db, tconst, media.get('parent_tconst'), archive_display_tconst)
    result = {'id': event_id, 'tconst': tconst, 'event_scope': event_scope, 'watched_on': effective_watched_on, 'created_at': now, 'archived_items': archived_items, 'library': db._get_library_summary_for_tconst(tconst)}
    if session_result is not None:
        result['session_id'] = session_result['session']['session_id']
        result['workflow'] = 'title_session'
    return result

def record_watch_events_through_episode(episode_tconst: str, *, watched_on: str | None=None, notes: str | None=None) -> dict[str, Any]:
    """Oznac jako zhlednute vsechny epizody serialu az po zadanou epizodu."""
    db = _db()
    detail = db.get_content_detail(episode_tconst)
    if detail is None or detail.get('kind') != 'episode':
        raise ValueError('Epizoda nebyla nalezena.')
    series_tconst = detail.get('series_tconst')
    season_number = detail.get('season_number')
    episode_number = detail.get('episode_number')
    if not series_tconst or season_number is None or episode_number is None:
        raise ValueError('Epizoda nema uplny serialovy kontext.')
    now = db._now_iso()
    effective_watched_on = watched_on or now[:10]
    try:
        db.datetime.strptime(effective_watched_on, '%Y-%m-%d')
    except ValueError as exc:
        raise ValueError('watched_on musi byt ISO datum ve formatu YYYY-MM-DD.') from exc
    episode_rows = fetch_series_episode_rows(series_tconst)
    candidate_ids = [str(row[0]) for row in episode_rows if row[0] and (row[1] is not None and row[1] < season_number or (row[1] == season_number and row[2] is not None and (row[2] <= episode_number)))]
    existing_ids = fetch_existing_watch_tconsts(candidate_ids)
    watched_ids = [tconst for tconst in candidate_ids if tconst not in existing_ids]
    if watched_ids:
        insert_watch_events_postgres([{'id': str(db.uuid.uuid4()), 'tconst': tconst, 'event_scope': 'episode', 'watched_on': effective_watched_on, 'notes': notes} for tconst in watched_ids], created_at=now)
    _invalidate_title_cache(db, episode_tconst, series_tconst, *watched_ids)
    return {'series_tconst': series_tconst, 'target_episode_tconst': episode_tconst, 'watched_on': effective_watched_on, 'watched_count': len(watched_ids), 'watched_tconsts': watched_ids, 'library': db._get_library_summary_for_tconst(series_tconst)}

def delete_group_from_user_list(list_id: str, display_tconst: str) -> dict[str, Any]:
    """Archivuj celou display skupinu ze zvoleneho seznamu."""
    db = _db()
    now = db._now_iso()
    result = archive_user_list_group(list_id=list_id, display_tconst=display_tconst, now=now)
    if not result['list_found']:
        raise ValueError('Seznam nebyl nalezen.')
    affected_rows = int(result['archived_items'])
    if affected_rows == 0:
        raise ValueError('V seznamu nebyla nalezena žádná položka k odstranění.')
    db.clear_title_presentation_cache()
    return {'list_id': list_id, 'display_tconst': display_tconst, 'updated_at': now, 'affected_rows': affected_rows}

def move_group_between_user_lists(source_list_id: str, target_list_id: str, display_tconst: str) -> dict[str, Any]:
    """Presun celou display skupinu z jednoho seznamu do druheho."""
    db = _db()
    if source_list_id == target_list_id:
        raise ValueError('Zdrojový a cílový seznam jsou stejné.')
    now = db._now_iso()
    source_list, items = _get_postgres_group_items_for_list(None, list_id=source_list_id, display_tconst=display_tconst)
    target_list = fetch_user_list(target_list_id)
    if source_list is None:
        raise ValueError('Zdrojový seznam nebyl nalezen.')
    if target_list is None:
        raise ValueError('Cílový seznam nebyl nalezen.')
    if not items:
        raise ValueError('V seznamu nebyla nalezena žádná položka k přesunu.')
    action_payload = _build_title_session_group_payload(
        items=items,
        display_tconst=display_tconst,
        source_ref_prefix=f'manual_move_group:{source_list_id}:{target_list_id}:{display_tconst}',
    )
    session_result = _run_title_session_action(
        db=db,
        tconst=display_tconst,
        trigger_action='move_to_list',
        action_payload=action_payload,
        now=now,
        source_list_id=source_list_id,
        target_list_id=target_list_id,
        notes_text=None,
        auto_finalize=True,
        session_scope='list_row_menu',
    )
    if session_result is None:
        _upsert_group_items_to_user_list(
            db=db,
            items=items,
            target_list_id=target_list_id,
            now=now,
        )
        _archive_group_items_from_user_list(
            items,
            source_list_id=source_list_id,
            now=now,
        )
    db.clear_title_presentation_cache()
    result = {'source_list_id': source_list_id, 'target_list_id': target_list_id, 'display_tconst': display_tconst, 'moved_rows': len(items), 'updated_at': now}
    if session_result is not None:
        result['session_id'] = session_result['session']['session_id']
        result['workflow'] = 'title_session'
    return result

def copy_group_to_user_list(source_list_id: str, target_list_id: str, display_tconst: str) -> dict[str, Any]:
    """Zkopiruj celou display skupinu z jednoho seznamu do druheho."""
    db = _db()
    if source_list_id == target_list_id:
        raise ValueError('Zdrojový a cílový seznam jsou stejné.')
    now = db._now_iso()
    source_list, items = _get_postgres_group_items_for_list(None, list_id=source_list_id, display_tconst=display_tconst)
    target_list = fetch_user_list(target_list_id)
    if source_list is None:
        raise ValueError('Zdrojový seznam nebyl nalezen.')
    if target_list is None:
        raise ValueError('Cílový seznam nebyl nalezen.')
    if not items:
        raise ValueError('V seznamu nebyla nalezena žádná položka ke kopii.')
    action_payload = _build_title_session_group_payload(
        items=items,
        display_tconst=display_tconst,
        source_ref_prefix=f'manual_copy_group:{source_list_id}:{target_list_id}:{display_tconst}',
    )
    session_result = _run_title_session_action(
        db=db,
        tconst=display_tconst,
        trigger_action='copy_to_list',
        action_payload=action_payload,
        now=now,
        source_list_id=source_list_id,
        target_list_id=target_list_id,
        notes_text=None,
        auto_finalize=True,
        session_scope='list_row_menu',
    )
    if session_result is None:
        _upsert_group_items_to_user_list(
            db=db,
            items=items,
            target_list_id=target_list_id,
            now=now,
        )
    db.clear_title_presentation_cache()
    result = {'source_list_id': source_list_id, 'target_list_id': target_list_id, 'display_tconst': display_tconst, 'copied_rows': len(items), 'updated_at': now}
    if session_result is not None:
        result['session_id'] = session_result['session']['session_id']
        result['workflow'] = 'title_session'
    return result

def create_user_list(name: str, description: str | None=None) -> dict[str, Any]:
    """Vytvor novy uzivatelsky seznam s uniknim slugem."""
    db = _db()
    cleaned_name = (name or '').strip()
    if not cleaned_name:
        raise ValueError('Název seznamu nesmí být prázdný.')
    cleaned_description = (description or '').strip() or None
    now = db._now_iso()
    list_id = f'custom-list-{db.uuid.uuid4()}'
    slug_base = db._slugify(cleaned_name) or 'list'
    slug = slug_base
    suffix = 2
    while slug_exists(slug):
        slug = f'{slug_base}-{suffix}'
        suffix += 1
    return create_user_list_postgres(list_id=list_id, slug=slug, name=cleaned_name, description=cleaned_description, now=now)

def update_user_list_description(list_id: str, description: str | None=None, ai_input_role: str | None=None) -> dict[str, Any]:
    """Uprav popis seznamu a jeho volitelnou AI input roli."""
    db = _db()
    cleaned_description = (description or '').strip() or None
    cleaned_ai_input_role = (ai_input_role or '').strip() or None
    now = db._now_iso()
    row = fetch_user_list(list_id)
    if row is None:
        raise ValueError('Seznam nebyl nalezen.')
    if row['list_kind'] != 'custom' and row['id'] != 'watchlist':
        raise ValueError('Popis lze upravit jen u uživatelských seznamů.')
    if cleaned_ai_input_role is not None and cleaned_ai_input_role not in AI_INPUT_ROLE_VALUES:
        raise ValueError('Neznámá role seznamu pro AI tipy.')
    updated = update_user_list_description_postgres(list_id, cleaned_description, cleaned_ai_input_role, now)
    if updated is None:
        raise ValueError('Seznam nebyl nalezen.')
    return updated

def delete_user_list(list_id: str) -> dict[str, Any]:
    """Smaz vlastni uzivatelsky seznam."""
    row = fetch_user_list(list_id)
    if row is None:
        raise ValueError('Seznam nebyl nalezen.')
    if row['list_kind'] != 'custom':
        raise ValueError('Smazat lze jen vlastní playlisty.')
    deleted = delete_user_list_postgres(list_id)
    if deleted is None:
        raise ValueError('Seznam nebyl nalezen.')
    if deleted['list_kind'] != 'custom':
        raise ValueError('Smazat lze jen vlastní playlisty.')
    return deleted

def clear_ai_suggestions_list_items() -> dict[str, Any]:
    """Vymaz vsechny aktivni polozky ze seznamu `AI navrhy`."""
    row = fetch_user_list('ai-suggestions')
    if row is None:
        raise ValueError('Seznam AI návrhy nebyl nalezen.')
    if row.get('ai_input_role') != 'external_suggestion':
        raise ValueError('Vyčistit lze jen seznam AI návrhy.')
    cleared = clear_ai_suggestions_list_items_postgres()
    if cleared is None:
        raise ValueError('Seznam AI návrhy nebyl nalezen.')
    if cleared.get('ai_input_role') != 'external_suggestion':
        raise ValueError('Vyčistit lze jen seznam AI návrhy.')
    _db().clear_title_presentation_cache()
    return cleared

def get_recently_watched_page(limit: int=50, offset: int=0) -> dict[str, Any]:
    """Vrat strankovany systemovy pohled `Recently Watched`."""
    db = _db()
    ui_config = db.get_ui_config()
    page = db._fetch_watch_view_page(limit, offset, cutoff_days=ui_config.recently_watched_days)
    return {'list': {'id': db.RECENTLY_WATCHED_VIEW_ID, 'slug': 'recently-watched', 'name': 'Recently Watched', 'list_kind': 'view', 'item_type': 'view', 'view_kind': 'recently_watched'}, 'total': page['total'], 'items': page['items'], 'limit': page['limit'], 'offset': page['offset']}

def get_hot_watchlist_page(limit: int=50, offset: int=0, available_in_cz: bool=False) -> dict[str, Any]:
    """Vrat strankovany systemovy pohled `Hot Watchlist`."""
    db = _db()
    ui_config = db.get_ui_config()
    hot_limit = ui_config.hot_watchlist_limit
    total, rows = fetch_hot_watchlist_page_rows(hot_limit=hot_limit, limit=limit, offset=offset, available_in_cz=available_in_cz)
    ratings_by_tconst = fetch_latest_ratings_for_tconsts([str(row[0]) for row in rows])
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append({'tconst': row[0], 'media_type': row[1], 'title': row[15], 'parent_title': row[3], 'season_number': row[4], 'episode_number': row[5], 'rank': row[6], 'added_at': row[7], 'notes': row[8], 'list_name': row[9], 'list_kind': row[10], 'poster_url': db._poster_url_from_local_path(row[13] or row[14]), 'title_type': row[11], 'year': row[12], 'end_year': None, 'runtime_minutes': None, 'series_title': row[15], 'user_rating': ratings_by_tconst.get(str(row[0]), {}).get('rating')})
    return {'list': {'id': db.HOT_WATCHLIST_VIEW_ID, 'slug': 'hot-watchlist', 'name': 'Hot Watchlist', 'list_kind': 'view', 'item_type': 'view', 'view_kind': 'hot_watchlist'}, 'total': total, 'items': items, 'limit': limit, 'offset': offset, 'filters': {'available_in_cz': available_in_cz}}

def get_watched_page(limit: int=50, offset: int=0) -> dict[str, Any]:
    """Vrat strankovany systemovy pohled `Watched`."""
    db = _db()
    page = db._fetch_watch_view_page(limit, offset, cutoff_days=None)
    return {'list': {'id': db.WATCHED_VIEW_ID, 'slug': 'watched', 'name': 'Watched', 'list_kind': 'view', 'item_type': 'view', 'view_kind': 'watched'}, 'total': page['total'], 'items': page['items'], 'limit': page['limit'], 'offset': page['offset']}

def get_local_library_status() -> dict[str, Any]:
    """Sloz agregovany snapshot poctu a viditelnych seznamu lokalni knihovny."""
    cached = _get_cached_local_library_status()
    if cached is not None:
        return cached
    db = _db()
    ui_config = db.get_ui_config()
    snapshot = fetch_library_status_snapshot(recently_watched_days=ui_config.recently_watched_days, hot_watchlist_limit=ui_config.hot_watchlist_limit)
    counts = dict(snapshot['counts'])
    base_lists = list(snapshot['base_lists'])
    watchlist_count = int(snapshot['watchlist_count'])
    hot_watchlist_count = int(snapshot['hot_watchlist_count'])
    watched_count = int(snapshot['watched_count'])
    recently_watched_count = int(snapshot['recently_watched_count'])
    counts['watchlist_items'] = watchlist_count
    for item in base_lists:
        if item['id'] == 'watchlist':
            item['item_count'] = watchlist_count
            break
    visible_lists = list(base_lists)
    visible_lists.append({'id': db.HOT_WATCHLIST_VIEW_ID, 'slug': 'hot-watchlist', 'name': 'Hot Watchlist', 'description': f'Last {ui_config.hot_watchlist_limit} titles added to Watchlist.', 'list_kind': 'view', 'item_count': hot_watchlist_count, 'item_type': 'view', 'view_kind': 'hot_watchlist'})
    visible_lists.append({'id': db.WATCHED_VIEW_ID, 'slug': 'watched', 'name': 'Watched', 'description': 'All watched titles from local history.', 'list_kind': 'view', 'item_count': watched_count, 'item_type': 'view', 'view_kind': 'watched'})
    visible_lists.append({'id': db.RECENTLY_WATCHED_VIEW_ID, 'slug': 'recently-watched', 'name': 'Recently Watched', 'description': f'Local history from the last {ui_config.recently_watched_days} days.', 'list_kind': 'view', 'item_count': recently_watched_count, 'item_type': 'view', 'view_kind': 'recently_watched'})

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        """Udrz systemove pohledy v pevnem poradi pred ostatnimi seznamy."""
        if item['id'] == 'watchlist':
            return (0, item['name'].lower())
        if item.get('view_kind') == 'hot_watchlist':
            return (1, item['name'].lower())
        if item.get('view_kind') == 'watched':
            return (2, item['name'].lower())
        if item.get('view_kind') == 'recently_watched':
            return (3, item['name'].lower())
        return (4, item['name'].lower())
    result = {'counts': counts, 'lists': sorted(base_lists, key=sort_key), 'visible_lists': sorted(visible_lists, key=sort_key)}
    _store_cached_local_library_status(result)
    return result

def get_continue_watching_items(limit: int=5) -> list[dict[str, Any]]:
    """Vrat lehky seznam titulu ve stavu `in_progress` pro homepage."""
    db = _db()
    states = list_in_progress_content_states(limit=limit)
    if not states:
        return []
    ordered_tconsts = [state['tconst'] for state in states]
    rows = fetch_continue_watching_catalog_rows(ordered_tconsts)
    details_by_tconst = {row[0]: {'title_type': row[1], 'title': row[2], 'original_title': row[3], 'year': row[4], 'end_year': row[5], 'runtime_minutes': row[6], 'genres': row[7] or [], 'imdb_rating': row[8], 'imdb_votes': row[9], 'series_tconst': row[10], 'season_number': row[11], 'episode_number': row[12], 'series_title': row[13], 'poster_relative_path': row[14], 'poster_local_path': row[15]} for row in rows if row[14] or row[15]}
    items: list[dict[str, Any]] = []
    for state in states:
        detail = details_by_tconst.get(state['tconst'])
        if detail is None:
            continue
        items.append({'tconst': state['tconst'], 'interest_state': state['interest_state'], 'last_previewed_at': state['last_previewed_at'], 'last_watched_at': state['last_watched_at'], 'updated_at': state['updated_at'], **detail, 'poster_url': db._poster_url_from_local_path(detail['poster_relative_path'] or detail['poster_local_path'])})
    return items[:limit]

def get_user_list_items_page(list_id: str, limit: int=50, offset: int=0, available_in_cz: bool=False) -> dict[str, Any]:
    """Vrat strankovanou sadu polozek jednoho seznamu nebo systemoveho view."""
    db = _db()
    list_row, total, rows = fetch_user_list_page_rows(list_id=list_id, limit=limit, offset=offset, exclude_watched=list_id == 'watchlist', available_in_cz=available_in_cz)
    if list_row is None:
        return {'list': None, 'total': 0, 'items': [], 'limit': limit, 'offset': offset, 'filters': {'available_in_cz': available_in_cz}}
    ratings_by_tconst = fetch_latest_ratings_for_tconsts([str(row[0]) for row in rows])
    items: list[dict[str, Any]] = []
    for row in rows:
        item = {'tconst': row[0], 'media_type': row[1], 'title': row[15], 'parent_title': row[3], 'season_number': row[4], 'episode_number': row[5], 'rank': row[6], 'added_at': row[7], 'notes': row[8], 'list_name': row[9], 'list_kind': row[10], 'poster_url': db._poster_url_from_local_path(row[13] or row[14]), 'title_type': row[11], 'year': row[12], 'end_year': None, 'runtime_minutes': None, 'series_title': row[15], 'user_rating': ratings_by_tconst.get(str(row[0]), {}).get('rating')}
        items.append(item)
    return {'list': list_row, 'total': total, 'items': items, 'limit': limit, 'offset': offset, 'filters': {'available_in_cz': available_in_cz}}

def get_user_list_items(list_id: str, limit: int=12) -> list[dict[str, Any]]:
    """Vrat zkracenou prvni stranku polozek seznamu bez dalsi metadata obalky."""
    return get_user_list_items_page(list_id, limit=limit, offset=0)['items']
