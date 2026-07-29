"""HTML routy pro AI a zanrove suggestion pohledy."""

from __future__ import annotations

from math import ceil
from urllib.parse import quote_plus

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import HTMLResponse

from filmy.app_shared import apply_html_cache_headers, build_breadcrumb_context, format_czech_datetime, templates
from filmy.db import (
    fetch_watch_stats_for_tconsts,
    get_favorite_traits,
    get_genre_suggestion_candidates,
    get_home_suggestion_sections,
    get_latest_genre_scores,
    get_title_card_summaries_for_tconsts,
    get_title_overviews_for_tconsts,
)
from filmy.suggestion_engine import match_traits_for_text

from .web_shared import build_genre_signal_cards, present_suggestion_title_card

router = APIRouter()


@router.get("/views/suggestions", response_class=HTMLResponse)
async def suggestions_overview_page(request: Request):
    """Vykresli prehled suggestion sekci pro domovske drilly."""
    breadcrumb_context = build_breadcrumb_context(
        request,
        "Suggestions",
        default_trail=[{"url": "/", "label": "Home"}],
    )
    suggestion_sections = get_home_suggestion_sections(limit_per_section=4)
    active_traits = suggestion_sections["active_traits"]
    latest_genre_scores = get_latest_genre_scores(limit=8)
    genre_signal_cards = build_genre_signal_cards(latest_genre_scores["items"] if latest_genre_scores else [], active_traits)
    suggestions_return_to = "/views/suggestions"
    summary_by_tconst = get_title_card_summaries_for_tconsts(
        [
            *(str(item["tconst"]) for item in suggestion_sections["trait_matches"] if item.get("tconst")),
            *(str(item["tconst"]) for item in suggestion_sections["new_on_imdb"] if item.get("tconst")),
        ]
    )

    trait_suggestion_cards: list[dict[str, object]] = []
    for item in suggestion_sections["trait_matches"]:
        summary = summary_by_tconst.get(str(item["tconst"]))
        if summary is None:
            continue
        trait_suggestion_cards.append(
            present_suggestion_title_card(
                item,
                summary,
                return_to=suggestions_return_to,
                score_key="total_score",
                reason_label="Trait match",
            )
        )

    new_imdb_cards: list[dict[str, object]] = []
    for item in suggestion_sections["new_on_imdb"]:
        summary = summary_by_tconst.get(str(item["tconst"]))
        if summary is None:
            continue
        new_imdb_cards.append(
            present_suggestion_title_card(
                item,
                summary,
                return_to=suggestions_return_to,
                score_key="total_score",
                reason_label="New on IMDb",
            )
        )

    response = templates.TemplateResponse(
        request,
        "suggestions_overview.html",
        {
            **breadcrumb_context,
            "suggestion_scores": genre_signal_cards,
            "suggestion_scores_generated_at": latest_genre_scores["generated_at"] if latest_genre_scores else None,
            "trait_suggestion_cards": trait_suggestion_cards,
            "new_imdb_cards": new_imdb_cards,
            "favorite_traits_active_count": len(active_traits),
            "format_czech_datetime": format_czech_datetime,
        },
    )
    return apply_html_cache_headers(response)


@router.get("/views/suggestions/traits", response_class=HTMLResponse)
async def trait_suggestions_detail(request: Request, page: int = 1):
    """Vykresli strankovany seznam trait-based suggestion kandidatu."""
    limit = 24
    all_sections = get_home_suggestion_sections(limit_per_section=None)
    all_items = all_sections["trait_matches"]
    total = len(all_items)
    total_pages = max(ceil(total / limit), 1)
    current_page = min(page, total_pages)
    offset = (current_page - 1) * limit
    page_items = all_items[offset : offset + limit]

    list_return_to = f"/views/suggestions/traits?page={current_page}"
    summary_by_tconst = get_title_card_summaries_for_tconsts([str(item["tconst"]) for item in page_items if item.get("tconst")])
    suggestion_cards: list[dict[str, object]] = []
    for item in page_items:
        summary = summary_by_tconst.get(str(item["tconst"]))
        if summary is None:
            continue
        suggestion_cards.append(
            present_suggestion_title_card(
                item,
                summary,
                return_to=list_return_to,
                score_key="total_score",
                reason_label="Trait match",
            )
        )

    response = templates.TemplateResponse(
        request,
        "suggestion_list_detail.html",
        {
            "page_title": "Matches your traits",
            "page_description": "Tituly, ktere nejvic sedi na tvoje aktivni traits.",
            "suggestion_cards": suggestion_cards,
            "suggestion_total": total,
            "suggestion_page": current_page,
            "suggestion_total_pages": total_pages,
            "suggestion_has_previous": current_page > 1,
            "suggestion_has_next": current_page < total_pages,
            "suggestion_prev_url": f"/views/suggestions/traits?page={current_page - 1}" if current_page > 1 else None,
            "suggestion_next_url": f"/views/suggestions/traits?page={current_page + 1}" if current_page < total_pages else None,
            "active_traits_count": len(all_sections["active_traits"]),
        },
    )
    return apply_html_cache_headers(response)


@router.get("/views/suggestions/genres/{genre}", response_class=HTMLResponse)
async def genre_signal_detail(request: Request, genre: str):
    """Vykresli detail jednoho zanroveho signalu a jeho dukazy."""
    latest = get_latest_genre_scores(limit=None)
    if latest is None:
        raise HTTPException(status_code=404, detail="Žánrové signály zatím nejsou spočítané.")

    resolved_genre = genre.strip()
    genre_item = next((item for item in latest["items"] if str(item.get("genre")) == resolved_genre), None)
    if genre_item is None:
        raise HTTPException(status_code=404, detail="Žánr nebyl v posledním snapshotu nalezen.")

    genre_candidates = get_genre_suggestion_candidates(resolved_genre, limit=24)
    active_traits = genre_candidates["active_traits"]
    genre_return_to = f"/views/suggestions/genres/{quote_plus(resolved_genre)}"

    evidence_rows = [
        row
        for row in [*list(genre_item.get("contributing_titles") or []), *list(genre_item.get("excluded_titles") or [])]
        if isinstance(row, dict) and row.get("tconst")
    ]
    summary_by_tconst = get_title_card_summaries_for_tconsts(
        [*(str(row["tconst"]) for row in genre_candidates["items"] if row.get("tconst")), *(str(row["tconst"]) for row in evidence_rows)]
    )
    overview_by_tconst = get_title_overviews_for_tconsts([str(row["tconst"]) for row in evidence_rows if row.get("tconst")])

    def build_cards(rows: list[dict[str, object]], reason_label: str) -> list[dict[str, object]]:
        cards: list[dict[str, object]] = []
        watch_stats = fetch_watch_stats_for_tconsts([str(row["tconst"]) for row in rows if isinstance(row, dict) and row.get("tconst")])
        for row in rows:
            if not isinstance(row, dict):
                continue
            tconst = str(row.get("tconst") or "").strip()
            if not tconst:
                continue
            summary = summary_by_tconst.get(tconst)
            if summary is None:
                continue
            if (watch_stats.get(tconst) or {}).get("watched_count"):
                continue
            card = present_suggestion_title_card(
                row,
                summary,
                return_to=genre_return_to,
                score_key="title_affinity",
                reason_label=reason_label,
            )
            card["matched_traits"] = match_traits_for_text(overview_by_tconst.get(tconst) or "", active_traits)
            cards.append(card)
        return cards

    recommended_cards = []
    for row in genre_candidates["items"]:
        summary = summary_by_tconst.get(str(row["tconst"]))
        if summary is None:
            continue
        recommended_cards.append(
            present_suggestion_title_card(
                row,
                summary,
                return_to=genre_return_to,
                score_key="candidate_score",
                reason_label="Recommend",
            )
        )
        recommended_cards[-1]["matched_traits"] = row.get("matched_traits") or []

    contributing_cards = build_cards(list(genre_item.get("contributing_titles") or []), "Signal evidence")
    excluded_cards = build_cards(list(genre_item.get("excluded_titles") or []), "Weak fit")

    response = templates.TemplateResponse(
        request,
        "genre_signal_detail.html",
        {
            "genre_item": genre_item,
            "recommended_cards": recommended_cards,
            "contributing_cards": contributing_cards,
            "excluded_cards": excluded_cards,
            "active_traits_count": len(active_traits),
        },
    )
    return apply_html_cache_headers(response)
