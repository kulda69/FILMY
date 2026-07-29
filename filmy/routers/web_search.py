"""HTML routy pro vyhledavani titulu a osob."""

from __future__ import annotations

import logging
from time import perf_counter

from fastapi import APIRouter, Query, Request
from starlette.responses import HTMLResponse

from filmy.app_shared import (
    apply_html_cache_headers,
    build_breadcrumb_context,
    present_person_search_result_card,
    present_search_result_card,
    signal_metadata_pipeline,
    templates,
)
from filmy.db import (
    get_person_presentation,
    get_title_card_summaries_for_tconsts,
    get_title_presentation,
    lookup_person_by_query,
    lookup_title_by_query,
)

from .web_shared import present_search_title_card_from_summary

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/search", response_class=HTMLResponse)
async def search_results_page(
    request: Request,
    q: str | None = Query(default=None, min_length=1),
    mode: str = Query(default="auto", pattern="^(auto|wide)$"),
    search_scope: str = Query(default="all", pattern="^(all|titles|people)$"),
    title_type: str | None = Query(default=None, pattern="^(movie|tvMovie|tvSeries|tvMiniSeries)$"),
):
    """Vykresli title/person search vysledky a lehke alternativy."""
    started_at = perf_counter()
    breadcrumb_context = build_breadcrumb_context(
        request,
        "Search",
        default_trail=[{"url": "/", "label": "Home"}],
    )
    page_return_to = str(breadcrumb_context["page_return_to"])

    lookup = None
    primary_result = None
    alternate_results: list[dict[str, object]] = []
    person_lookup = None
    person_primary_result = None
    person_alternate_results: list[dict[str, object]] = []
    searched_query = (q or "").strip()
    candidates_limit = 8 if mode == "wide" else 5
    alternate_limit = 5 if mode == "wide" else 3

    if searched_query:
        if search_scope in {"all", "titles"}:
            lookup = lookup_title_by_query(
                query=searched_query,
                title_type=title_type,
                candidates_limit=candidates_limit,
                allow_expensive_fallback=(mode == "wide"),
            )
            if lookup is not None:
                selected_presentation = get_title_presentation(lookup["selected_tconst"])
                if selected_presentation is not None:
                    selected = dict(selected_presentation)
                    selected["query"] = searched_query
                    selected["match"] = dict(lookup["selected"])
                    primary_result = present_search_result_card(
                        selected,
                        match=selected.get("match"),
                        return_to=page_return_to,
                    )
                    signal_metadata_pipeline("search_title_lookup", target_tconst=str(lookup["selected_tconst"]))

                scored_candidates = sorted(
                    [candidate for candidate in lookup["candidates"] if candidate.get("fuzzy_score") is not None],
                    key=lambda item: float(item.get("fuzzy_score") or 0.0),
                    reverse=True,
                )
                alternate_candidates = [candidate for candidate in scored_candidates if candidate["tconst"] != lookup["selected_tconst"]][:alternate_limit]
                alternate_summaries = get_title_card_summaries_for_tconsts([str(candidate["tconst"]) for candidate in alternate_candidates])
                alternate_count = 0
                for candidate in alternate_candidates:
                    candidate_summary = alternate_summaries.get(str(candidate["tconst"]))
                    if candidate_summary is None:
                        continue
                    alternate_results.append(
                        present_search_title_card_from_summary(candidate_summary, match=candidate, return_to=page_return_to)
                    )
                    alternate_count += 1
                    if alternate_count >= alternate_limit:
                        break

        if search_scope in {"all", "people"}:
            person_lookup = lookup_person_by_query(query=searched_query, candidates_limit=candidates_limit)
            if person_lookup is not None:
                selected_person_presentation = get_person_presentation(person_lookup["selected_nconst"])
                if selected_person_presentation is not None:
                    selected_person = dict(selected_person_presentation)
                    selected_person["query"] = searched_query
                    selected_person["match"] = dict(person_lookup["selected"])
                    person_primary_result = present_person_search_result_card(
                        selected_person,
                        match=selected_person.get("match"),
                        return_to=page_return_to,
                    )
                scored_people = sorted(
                    [candidate for candidate in person_lookup["candidates"] if candidate.get("fuzzy_score") is not None],
                    key=lambda item: float(item.get("fuzzy_score") or 0.0),
                    reverse=True,
                )
                person_alternate_limit = 4 if mode == "wide" else 2
                person_alternate_count = 0
                for candidate in scored_people:
                    if candidate["nconst"] == person_lookup["selected_nconst"]:
                        continue
                    candidate_presentation = get_person_presentation(candidate["nconst"])
                    if candidate_presentation is None:
                        continue
                    person_alternate_results.append(
                        present_person_search_result_card(
                            candidate_presentation,
                            match=candidate,
                            return_to=page_return_to,
                        )
                    )
                    person_alternate_count += 1
                    if person_alternate_count >= person_alternate_limit:
                        break

    response = templates.TemplateResponse(
        request,
        "search_results.html",
        {
            **breadcrumb_context,
            "nav_search_query": searched_query,
            "nav_search_scope": search_scope,
            "search_query": searched_query,
            "search_mode": mode,
            "search_scope": search_scope,
            "search_title_type": title_type,
            "search_lookup": lookup,
            "search_primary_result": primary_result,
            "search_alternate_results": alternate_results,
            "person_search_lookup": person_lookup,
            "person_search_primary_result": person_primary_result,
            "person_search_alternate_results": person_alternate_results,
        },
    )
    logger.info(
        "route=/search mode=%s query=%r title_candidates=%s person_candidates=%s elapsed_ms=%.1f",
        mode,
        searched_query,
        lookup.get("candidate_count") if lookup else 0,
        person_lookup.get("candidate_count") if person_lookup else 0,
        (perf_counter() - started_at) * 1000,
    )
    return apply_html_cache_headers(response)
