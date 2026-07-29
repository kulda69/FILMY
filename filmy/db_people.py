"""Person detail a filmography facade vyclenena z `filmy.db`."""

from __future__ import annotations
'Person-focused DB operations extracted from the `filmy.db` facade.\n\nThis module keeps public person lookup and presentation routines together while\nthe rest of the application still imports them through `filmy.db`. The\nimplementation intentionally delegates to shared private helpers in `filmy.db`\nso the refactor can proceed in small verified steps without breaking callers.\n'
import importlib
from typing import Any
from filmy.runtime_postgres import fetch_person_affinity_rating

def _db():
    """Nacti `filmy.db` az pri behu, aby nevznikal cyklicky import."""

    return importlib.import_module('filmy.db')

def describe_person_by_query(query: str) -> dict[str, Any] | None:
    """Najdi nejvhodnejsi osobu pro textovy dotaz."""

    from filmy.db_lookup import describe_person_by_query as _impl
    return _impl(query)

def lookup_person_by_query(query: str, candidates_limit: int=5) -> dict[str, Any] | None:
    """Proved person lookup s kandidaty a vybranou osobou."""

    from filmy.db_lookup import lookup_person_by_query as _impl
    return _impl(query, candidates_limit=candidates_limit)

def get_person_presentation(nconst: str) -> dict[str, Any] | None:
    """Vrat cachovanou nebo novou prezentaci osoby."""

    from filmy.db_presentation import (
        _fetch_person_cache_source_detail,
        _load_cached_person_presentation,
        _person_cache_source_fingerprint,
        _store_cached_person_presentation,
    )

    _ensure_person_biography_if_needed(nconst)
    cached = _load_cached_person_presentation(None, nconst)
    if cached is not None:
        return cached
    presentation = _fetch_person_cache_source_detail(None, nconst)
    if presentation is None:
        return None
    cache_fingerprint = _person_cache_source_fingerprint(None, nconst, presentation)
    _store_cached_person_presentation(nconst, presentation, cache_fingerprint)
    return presentation

def _ensure_person_biography_if_needed(nconst: str) -> None:
    """Dotahni biografii jen pro osoby s kladnou affinity, pokud stale chybi."""

    affinity_rating = fetch_person_affinity_rating(nconst)
    if affinity_rating <= 0:
        return
    try:
        from filmy.integrations.tmdb import fetch_person_biography, get_person_biography_status
        status = get_person_biography_status(nconst)
        if status['status'] in {'fetched', 'no_biography', 'not_found'}:
            return
        fetch_person_biography(nconst, fetch_reason='person_detail_affinity')
    except Exception:
        return

def render_person_presentation(presentation: dict[str, Any]) -> str:
    """Preved datovou prezentaci osoby na textovy vystup."""

    from filmy.db_presentation import render_person_presentation as _impl
    return _impl(presentation)
