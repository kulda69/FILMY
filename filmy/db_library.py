from __future__ import annotations
'Local-library DB operations extracted from the `filmy.db` facade.\n\nThe goal is to separate mutations and list-oriented read models from the very\nlarge legacy module while preserving the existing public API. Runtime imports\nback to `filmy.db` are deliberate here: they let us reuse stable internal\nhelpers first and only later decide which helpers deserve their own dedicated\nmodule.\n'
import importlib
import threading
import time
from copy import deepcopy
from datetime import datetime
from typing import Any
from filmy.runtime_postgres import archive_user_list_group, archive_user_list_item, clear_ai_suggestions_list_items as clear_ai_suggestions_list_items_postgres, create_user_list as create_user_list_postgres, delete_user_list as delete_user_list_postgres, delete_title_role_signals as delete_title_role_signals_postgres, fetch_all_watch_events, fetch_existing_watch_tconsts, fetch_active_user_list_items, fetch_continue_watching_catalog_rows, fetch_episode_series_map, fetch_hot_watchlist_page_rows, fetch_person_catalog_row, fetch_title_role_signals as fetch_title_role_signals_postgres, fetch_library_status_projection, fetch_library_status_snapshot, fetch_person_affinity_rating, fetch_series_episode_rows, fetch_title_card_rows, fetch_user_list, fetch_user_list_page_rows, fetch_user_list_item_counts, fetch_user_lists, delete_user_rating as delete_user_rating_postgres, fetch_latest_ratings_for_tconsts, insert_watch_events as insert_watch_events_postgres, list_in_progress_content_states, record_watched as record_watched_postgres, slug_exists, upsert_user_rating as upsert_user_rating_postgres, upsert_user_list_item, upsert_person_affinity, upsert_title_role_signal, update_content_state as update_content_state_postgres, update_user_list_description as update_user_list_description_postgres

def _db():
    return importlib.import_module('filmy.db')

def _invalidate_title_cache(db: Any, *tconsts: str | None) -> None:
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

def _get_cached_local_library_status() -> dict[str, Any] | None:
    with _local_library_status_cache_lock:
        if _local_library_status_cache is None or time.time() - _local_library_status_cached_at > _LOCAL_LIBRARY_STATUS_CACHE_TTL_SECONDS:
            return None
        return deepcopy(_local_library_status_cache)

def _store_cached_local_library_status(value: dict[str, Any]) -> None:
    global _local_library_status_cache, _local_library_status_cached_at
    with _local_library_status_cache_lock:
        _local_library_status_cache = deepcopy(value)
        _local_library_status_cached_at = time.time()

def _order_group_items_for_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = list(items)
    ordered.sort(key=lambda item: item.get('title') or item.get('parent_title') or item.get('tconst') or '')
    ordered.sort(key=lambda item: item.get('added_at') or datetime.min, reverse=True)
    ordered.sort(key=lambda item: (item.get('rank') is None, item.get('rank') if item.get('rank') is not None else 0))
    return ordered

def _load_episode_series_map(conn, tconsts: list[str]) -> dict[str, str]:
    if not tconsts:
        return {}
    db = _db()
    return fetch_episode_series_map(tconsts)

def _load_watched_display_tconsts(conn) -> set[str]:
    watched_tconsts = sorted({str(item['tconst']) for item in fetch_all_watch_events() if item.get('tconst')})
    if not watched_tconsts:
        return set()
    episode_series_map = _load_episode_series_map(conn, watched_tconsts)
    return {episode_series_map.get(tconst, tconst) for tconst in watched_tconsts}

def _group_postgres_list_items(conn, *, list_id: str, exclude_watched: bool=False) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    list_row = fetch_user_list(list_id)
    if list_row is None:
        return (None, [])
    active_items = [item for item in fetch_active_user_list_items() if item['list_id'] == list_id]
    if not active_items:
        return (list_row, [])
    tconsts = [str(item['tconst']) for item in active_items if item.get('tconst')]
    episode_series_map = _load_episode_series_map(conn, tconsts)
    watched_display_tconsts = _load_watched_display_tconsts(conn) if exclude_watched else set()
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
        ordered_items = _order_group_items_for_list(items)
        representative = ordered_items[0]
        groups.append({'display_tconst': display_tconst, 'media_type': representative.get('media_type'), 'title': representative.get('title'), 'parent_title': representative.get('parent_title'), 'season_number': None, 'episode_number': None, 'rank': representative.get('rank'), 'added_at': representative.get('added_at'), 'notes': representative.get('notes'), 'list_name': list_row['name'], 'list_kind': list_row['list_kind']})
    return (list_row, _order_group_items_for_list(groups))

def _load_group_cards(conn, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not groups:
        return []
    db = _db()
    display_tconsts = [str(group['display_tconst']) for group in groups]
    rows = fetch_title_card_rows(display_tconsts)
    cards_by_tconst = {str(row[0]): {'title_type': row[1], 'year': row[2], 'resolved_title': row[3], 'poster_relative_path': row[4], 'poster_local_path': row[5]} for row in rows if row[4] or row[5]}
    return [group | cards_by_tconst[group['display_tconst']] for group in groups if group['display_tconst'] in cards_by_tconst]

def _get_postgres_group_items_for_list(conn, *, list_id: str, display_tconst: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    list_row = fetch_user_list(list_id)
    if list_row is None:
        return (None, [])
    active_items = [item for item in fetch_active_user_list_items() if item['list_id'] == list_id]
    tconsts = [str(item['tconst']) for item in active_items if item.get('tconst')]
    episode_series_map = _load_episode_series_map(conn, tconsts)
    matching_items: list[dict[str, Any]] = []
    for item in active_items:
        item_display_tconst = (episode_series_map.get(str(item['tconst'])) if item.get('tconst') else None) or item.get('tconst') or item.get('parent_tconst')
        if str(item_display_tconst) != str(display_tconst):
            continue
        matching_items.append(dict(item))
    return (list_row, _order_group_items_for_list(matching_items))

def update_content_state(tconst: str, interest_state: str) -> dict[str, Any]:
    db = _db()
    now = db._now_iso()
    result = update_content_state_postgres(tconst, interest_state, now)
    _invalidate_title_cache(db, tconst)
    return result

def set_watchlist_state(tconst: str, *, in_watchlist: bool, notes: str | None=None) -> dict[str, Any]:
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

def set_user_rating(tconst: str, rating: int, *, liked_notes: str | None=None, disliked_notes: str | None=None) -> dict[str, Any]:
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
    upsert_user_rating_postgres(canonical_key=canonical_key, tconst=media['tconst'], media_type=media['media_type'], imdb_id=media['imdb_id'], tmdb_id=media['tmdb_id'], trakt_id=None, parent_tconst=media['parent_tconst'], parent_title=media['parent_title'], title=media['title'], season_number=media['season_number'], episode_number=media['episode_number'], rating=rating, liked_notes=cleaned_liked_notes, disliked_notes=cleaned_disliked_notes, rated_at=now, source_origin='local_app', source_ref=f'manual_rating:{tconst}', now=now)
    _invalidate_title_cache(db, tconst, media.get('parent_tconst'))
    return {'tconst': tconst, 'rating': rating, 'rated_at': now, 'library': db._get_library_summary_for_tconst(tconst)}

def set_person_affinity_rating(nconst: str, rating: int) -> dict[str, Any]:
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
    cleaned_tconst = (tconst or '').strip()
    if not cleaned_tconst:
        return []
    return fetch_title_role_signals_postgres(cleaned_tconst)

def clear_user_rating(tconst: str) -> dict[str, Any]:
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

def record_watch_event(tconst: str, *, watched_on: str | None=None, notes: str | None=None, add_to_watched_list: bool=False, archive_from_list_id: str | None=None, archive_display_tconst: str | None=None) -> dict[str, Any]:
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
    action_result = record_watched_postgres(event_id=event_id, tconst=tconst, event_scope=event_scope, watched_on=effective_watched_on, notes=notes, created_at=now, archive_from_list_id=effective_archive_list_id, archive_canonical_key=effective_archive_canonical_key, archive_display_tconst=archive_display_tconst)
    _invalidate_title_cache(db, tconst, media.get('parent_tconst'), archive_display_tconst)
    return {'id': action_result['event_id'], 'tconst': tconst, 'event_scope': event_scope, 'watched_on': effective_watched_on, 'created_at': now, 'archived_items': action_result['archived_items'], 'library': db._get_library_summary_for_tconst(tconst)}

def record_watch_events_through_episode(episode_tconst: str, *, watched_on: str | None=None, notes: str | None=None) -> dict[str, Any]:
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
    for item in items:
        upsert_user_list_item(item_id=str(db.uuid.uuid4()), list_id=target_list_id, canonical_key=str(item['canonical_key']), tconst=item.get('tconst'), media_type=str(item['media_type']), imdb_id=item.get('imdb_id'), tmdb_id=item.get('tmdb_id'), trakt_id=item.get('trakt_id'), parent_tconst=item.get('parent_tconst'), parent_title=item.get('parent_title'), title=item.get('title'), season_number=item.get('season_number'), episode_number=item.get('episode_number'), rank=item.get('rank'), added_at=item.get('added_at').isoformat() if item.get('added_at') else None, notes=item.get('notes'), source_origin=str(item['source_origin']), source_ref=item.get('source_ref'), now=now)
    for item in items:
        archive_user_list_item(source_list_id, str(item['canonical_key']), now)
    db.clear_title_presentation_cache()
    return {'source_list_id': source_list_id, 'target_list_id': target_list_id, 'display_tconst': display_tconst, 'moved_rows': len(items), 'updated_at': now}

def copy_group_to_user_list(source_list_id: str, target_list_id: str, display_tconst: str) -> dict[str, Any]:
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
    for item in items:
        upsert_user_list_item(item_id=str(db.uuid.uuid4()), list_id=target_list_id, canonical_key=str(item['canonical_key']), tconst=item.get('tconst'), media_type=str(item['media_type']), imdb_id=item.get('imdb_id'), tmdb_id=item.get('tmdb_id'), trakt_id=item.get('trakt_id'), parent_tconst=item.get('parent_tconst'), parent_title=item.get('parent_title'), title=item.get('title'), season_number=item.get('season_number'), episode_number=item.get('episode_number'), rank=item.get('rank'), added_at=item.get('added_at').isoformat() if item.get('added_at') else None, notes=item.get('notes'), source_origin=str(item['source_origin']), source_ref=item.get('source_ref'), now=now)
    db.clear_title_presentation_cache()
    return {'source_list_id': source_list_id, 'target_list_id': target_list_id, 'display_tconst': display_tconst, 'copied_rows': len(items), 'updated_at': now}

def create_user_list(name: str, description: str | None=None) -> dict[str, Any]:
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
    db = _db()
    ui_config = db.get_ui_config()
    page = db._fetch_watch_view_page(limit, offset, cutoff_days=ui_config.recently_watched_days)
    return {'list': {'id': db.RECENTLY_WATCHED_VIEW_ID, 'slug': 'recently-watched', 'name': 'Recently Watched', 'list_kind': 'view', 'item_type': 'view', 'view_kind': 'recently_watched'}, 'total': page['total'], 'items': page['items'], 'limit': page['limit'], 'offset': page['offset']}

def get_hot_watchlist_page(limit: int=50, offset: int=0) -> dict[str, Any]:
    db = _db()
    ui_config = db.get_ui_config()
    hot_limit = ui_config.hot_watchlist_limit
    total, rows = fetch_hot_watchlist_page_rows(hot_limit=hot_limit, limit=limit, offset=offset)
    ratings_by_tconst = fetch_latest_ratings_for_tconsts([str(row[0]) for row in rows])
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append({'tconst': row[0], 'media_type': row[1], 'title': row[15], 'parent_title': row[3], 'season_number': row[4], 'episode_number': row[5], 'rank': row[6], 'added_at': row[7], 'notes': row[8], 'list_name': row[9], 'list_kind': row[10], 'poster_url': db._poster_url_from_local_path(row[13] or row[14]), 'title_type': row[11], 'year': row[12], 'end_year': None, 'runtime_minutes': None, 'series_title': row[15], 'user_rating': ratings_by_tconst.get(str(row[0]), {}).get('rating')})
    return {'list': {'id': db.HOT_WATCHLIST_VIEW_ID, 'slug': 'hot-watchlist', 'name': 'Hot Watchlist', 'list_kind': 'view', 'item_type': 'view', 'view_kind': 'hot_watchlist'}, 'total': total, 'items': items, 'limit': limit, 'offset': offset}

def get_watched_page(limit: int=50, offset: int=0) -> dict[str, Any]:
    db = _db()
    page = db._fetch_watch_view_page(limit, offset, cutoff_days=None)
    return {'list': {'id': db.WATCHED_VIEW_ID, 'slug': 'watched', 'name': 'Watched', 'list_kind': 'view', 'item_type': 'view', 'view_kind': 'watched'}, 'total': page['total'], 'items': page['items'], 'limit': page['limit'], 'offset': page['offset']}

def get_local_library_status() -> dict[str, Any]:
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

def get_user_list_items_page(list_id: str, limit: int=50, offset: int=0) -> dict[str, Any]:
    db = _db()
    list_row, total, rows = fetch_user_list_page_rows(list_id=list_id, limit=limit, offset=offset, exclude_watched=list_id == 'watchlist')
    if list_row is None:
        return {'list': None, 'total': 0, 'items': [], 'limit': limit, 'offset': offset}
    ratings_by_tconst = fetch_latest_ratings_for_tconsts([str(row[0]) for row in rows])
    items: list[dict[str, Any]] = []
    for row in rows:
        item = {'tconst': row[0], 'media_type': row[1], 'title': row[15], 'parent_title': row[3], 'season_number': row[4], 'episode_number': row[5], 'rank': row[6], 'added_at': row[7], 'notes': row[8], 'list_name': row[9], 'list_kind': row[10], 'poster_url': db._poster_url_from_local_path(row[13] or row[14]), 'title_type': row[11], 'year': row[12], 'end_year': None, 'runtime_minutes': None, 'series_title': row[15], 'user_rating': ratings_by_tconst.get(str(row[0]), {}).get('rating')}
        items.append(item)
    return {'list': list_row, 'total': total, 'items': items, 'limit': limit, 'offset': offset}

def get_user_list_items(list_id: str, limit: int=12) -> list[dict[str, Any]]:
    return get_user_list_items_page(list_id, limit=limit, offset=0)['items']
