from __future__ import annotations

"""Person-focused DB operations extracted from the `filmy.db` facade.

This module keeps public person lookup and presentation routines together while
the rest of the application still imports them through `filmy.db`. The
implementation intentionally delegates to shared private helpers in `filmy.db`
so the refactor can proceed in small verified steps without breaking callers.
"""

import importlib
from typing import Any

from filmy.database import run_duckdb_read
from filmy.runtime_postgres import app_state_uses_postgres, fetch_person_affinity_rating


def _db():
    return importlib.import_module("filmy.db")


def describe_person_by_query(query: str) -> dict[str, Any] | None:
    lookup = lookup_person_by_query(query=query, candidates_limit=5)
    if lookup is None:
        return None
    presentation = get_person_presentation(lookup["selected_nconst"])
    if presentation is None:
        return None
    selected = dict(presentation)
    selected["query"] = query
    selected["match"] = dict(lookup["selected"])
    return selected


def lookup_person_by_query(query: str, candidates_limit: int = 5) -> dict[str, Any] | None:
    db = _db()
    recalled = db._lookup_person_from_search_recall(query, candidates_limit=max(candidates_limit, 1))
    if recalled is not None:
        return recalled
    candidates = db._search_people_for_lookup(query=query, limit=max(candidates_limit, 1) * 5)
    should_expand = not candidates or db._should_expand_people_to_fuzzy(query, candidates)
    if should_expand:
        fuzzy_candidates = db._search_people_for_lookup_fuzzy(query=query, limit=max(candidates_limit, 1) * 5)
        candidates = db._merge_lookup_candidates(candidates, fuzzy_candidates)
    if not candidates:
        return None

    selected = db._pick_best_person_match(query, candidates)
    if not db._is_confident_person_lookup(query, selected):
        wide_candidates = db._search_people_for_lookup_levenshtein(query=query, limit=max(candidates_limit, 1) * 5)
        candidates = db._merge_lookup_candidates(candidates, wide_candidates)
        if not candidates:
            return None
        selected = db._pick_best_person_match(query, candidates)
    else:
        selected = db._pick_best_person_match(query, candidates)

    selected_key = selected["nconst"]
    ordered_candidates = sorted(
        candidates,
        key=lambda item: (0 if item["nconst"] == selected_key else 1, -(item.get("birth_year") or 0), item["primary_name"]),
    )
    result = {
        "query": query,
        "selected_nconst": selected_key,
        "selected": db._build_person_lookup_candidate(selected, query=query, is_selected=True),
        "candidates": [
            db._build_person_lookup_candidate(candidate, query=query, is_selected=(candidate["nconst"] == selected_key))
            for candidate in ordered_candidates[: max(candidates_limit, 1)]
        ],
        "candidate_count": len(candidates),
    }
    db._remember_person_lookup(query, selected)
    return result


def get_person_presentation(nconst: str) -> dict[str, Any] | None:
    db = _db()
    _ensure_person_biography_if_needed(nconst)

    if db.catalog_backend_uses_postgres():
        cached = db._load_cached_person_presentation(None, nconst)
        if cached is not None:
            return cached
        presentation = db._fetch_person_cache_source_detail(None, nconst)
        if presentation is None:
            return None
        cache_fingerprint = db._person_cache_source_fingerprint(None, nconst, presentation)
        db._store_cached_person_presentation(nconst, presentation, cache_fingerprint)
        return presentation

    def read_cached(conn):
        cached = db._load_cached_person_presentation(conn, nconst)
        if cached is not None:
            return ("cached", cached)
        presentation = db._fetch_person_cache_source_detail(conn, nconst)
        if presentation is None:
            return ("missing", None)
        return ("present", presentation)

    status, payload = run_duckdb_read(read_cached)
    if status == "cached":
        return payload
    if status == "missing" or payload is None:
        return None
    presentation = payload

    cache_fingerprint = run_duckdb_read(
        lambda conn: db._person_cache_source_fingerprint(conn, nconst, presentation)
    )
    db._store_cached_person_presentation(nconst, presentation, cache_fingerprint)
    return presentation


def _ensure_person_biography_if_needed(nconst: str) -> None:
    db = _db()
    affinity_rating = fetch_person_affinity_rating(nconst) if app_state_uses_postgres() else run_duckdb_read(lambda conn: db._get_person_affinity_rating(conn, nconst))
    if affinity_rating <= 0:
        return

    try:
        from filmy.integrations.tmdb import fetch_person_biography, get_person_biography_status

        status = get_person_biography_status(nconst)
        if status["status"] in {"fetched", "no_biography", "not_found"}:
            return
        fetch_person_biography(nconst, fetch_reason="person_detail_affinity")
    except Exception:
        # Biography is optional enrichment; a fetch problem must not block the page.
        return


def render_person_presentation(presentation: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(str(presentation["name"]))

    meta_bits: list[str] = []
    if presentation.get("birth_year") is not None:
        meta_bits.append(str(presentation["birth_year"]))
    if presentation.get("death_year") is not None:
        meta_bits.append(str(presentation["death_year"]))
    if presentation.get("primary_profession"):
        meta_bits.append(str(presentation["primary_profession"]))
    if meta_bits:
        lines.append(", ".join(meta_bits))

    known_for_items = presentation.get("known_for_items") or []
    known_for = presentation.get("known_for_titles") or ""
    if known_for_items:
        lines.append("")
        lines.append("Known for")
        lines.append(", ".join(item["title"] for item in known_for_items))
    elif known_for:
        lines.append("")
        lines.append("Known for")
        lines.append(known_for)

    biography = (presentation.get("biography") or {}).get("text")
    if biography:
        lines.append("")
        lines.append("Biography")
        lines.append(str(biography))

    filmography = presentation.get("filmography") or {}
    sections = [
        ("Directed", filmography.get("directed") or []),
        ("Created by", filmography.get("created") or []),
        ("Written", filmography.get("written") or []),
        ("Acted in", filmography.get("acted") or []),
    ]
    for section_title, items in sections:
        if not items:
            continue
        lines.append("")
        lines.append(section_title)
        for item in items[:20]:
            year = f" ({item['start_year']})" if item.get("start_year") is not None else ""
            role = f" as {item['character']}" if item.get("character") else ""
            lines.append(f"{item['title']}{year}{role}")

    other_items = filmography.get("other") or []
    if other_items:
        lines.append("")
        lines.append("Other credits")
        for item in other_items[:10]:
            year = f" ({item['start_year']})" if item.get("start_year") is not None else ""
            lines.append(f"{item['title']}{year}")

    lines.append("")
    lines.append(f"Total credits: {presentation.get('credit_count') or 0}")
    return "\n".join(lines)
