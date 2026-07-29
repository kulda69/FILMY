"""Sdilene dependency a pomocne utility pro HTML routy."""

from __future__ import annotations

from urllib.parse import quote_plus

from filmy.db import get_title_overviews_for_tconsts
from filmy.suggestion_engine import match_traits_for_text

PREFERENCE_PRIORITY_MIN = 1
PREFERENCE_PRIORITY_MAX = 10

DEFAULT_FAVORITE_TRAITS: tuple[str, ...] = (
    "cerebral",
    "mind-bending",
    "thought-provoking",
    "dark",
    "gritty",
    "tense",
    "atmospheric",
    "slow-burn",
    "mysterious",
    "twisty",
    "emotional",
    "melancholic",
    "haunting",
    "romantic",
    "feel-good",
    "uplifting",
    "heartwarming",
    "funny",
    "witty",
    "stylized",
    "visually striking",
    "intense",
    "suspenseful",
    "character-driven",
    "dialogue-driven",
    "psychological",
    "dystopian",
    "coming-of-age",
    "queer",
    "high-concept",
)


def list_filter_params(*, available_in_cz: bool) -> dict[str, str]:
    """Vrat query parametry pro list/view filtry.

    Routery casto skladaji stejne URL s volitelnym prepinacem
    `available_in_cz`. Centralizace udrzuje jednotne query parametry
    a zjednodusuje skladani breadcrumb i pagination URL.
    """
    params: dict[str, str] = {}
    if available_in_cz:
        params["available_in_cz"] = "1"
    return params


def build_genre_signal_cards(
    suggestion_scores: list[dict[str, object]],
    active_traits: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Preved zazanam poslednich genre score na lehke UI karty.

    Vstupni snapshot uz obsahuje titulove dukazy, ale pro zobrazeni
    potrebujeme jeste dopocitat nejzretelnejsi matched traits a detail URL.
    Tato transformace je ciste prezencni a drzi pohromade pravidla pro
    homepage i suggestion overview.
    """
    overview_tconsts: list[str] = []
    for item in suggestion_scores:
        for title_item in (item.get("contributing_titles") or [])[:6]:
            if isinstance(title_item, dict) and title_item.get("tconst"):
                overview_tconsts.append(str(title_item["tconst"]))
    overview_by_tconst = get_title_overviews_for_tconsts(overview_tconsts)

    cards: list[dict[str, object]] = []
    for item in suggestion_scores:
        contributing_titles = item.get("contributing_titles") or []
        matched_traits: list[str] = []
        for title_item in contributing_titles[:6]:
            if not isinstance(title_item, dict) or not title_item.get("tconst"):
                continue
            overview = overview_by_tconst.get(str(title_item["tconst"])) or ""
            if not overview:
                continue
            matched_traits.extend(match_traits_for_text(overview, active_traits))
        top_traits = sorted(dict.fromkeys(matched_traits))
        cards.append(
            {
                **item,
                "top_traits": top_traits[:3],
                "genre_detail_url": f"/views/suggestions/genres/{quote_plus(str(item['genre']))}",
            }
        )
    return cards


def present_suggestion_title_card(
    item: dict[str, object],
    summary: dict[str, object],
    *,
    return_to: str,
    score_key: str,
    reason_label: str,
) -> dict[str, object]:
    """Sloz jednotny payload suggestion karty z batch summary dat."""
    return {
        "tconst": summary.get("tconst"),
        "title": summary.get("title"),
        "original_title": summary.get("original_title"),
        "kind_label": summary.get("kind_label"),
        "year": summary.get("year"),
        "imdb_rating": item.get("average_rating"),
        "poster_url": summary.get("poster_url"),
        "main_cast_line": summary.get("main_cast_line"),
        "directed_by_line": summary.get("directed_by_line"),
        "matched_traits": item.get("matched_traits") or [],
        "user_lists": [],
        "detail_url": f"/titles/{summary.get('tconst')}?return_to={quote_plus(return_to)}",
        "suggestion_score_percent": round(float(item.get(score_key) or 0.0) * 100),
        "reason_label": reason_label,
    }


def present_search_title_card_from_summary(
    summary: dict[str, object],
    *,
    match: dict[str, object] | None,
    return_to: str,
) -> dict[str, object]:
    """Preved lehke batch title summary na payload search alternativy."""
    fuzzy_score = match.get("fuzzy_score") if match else None
    return {
        "tconst": summary.get("tconst"),
        "title": summary.get("title"),
        "original_title": summary.get("original_title"),
        "kind_label": summary.get("kind_label"),
        "year": summary.get("year"),
        "runtime_minutes": summary.get("runtime_minutes"),
        "genres": summary.get("genres") or [],
        "imdb_rating": summary.get("imdb_rating"),
        "imdb_votes": summary.get("imdb_votes"),
        "poster_url": summary.get("poster_url"),
        "overview": None,
        "directed_by_line": summary.get("directed_by_line"),
        "written_by_line": None,
        "main_cast_line": summary.get("main_cast_line"),
        "created_by_line": None,
        "available_in_czechia": [],
        "watched_count": 0,
        "user_lists": [],
        "user_rating": None,
        "detail_url": f"/titles/{summary.get('tconst')}?return_to={quote_plus(return_to)}",
        "is_exact_match": bool(match and match.get("is_exact_match")),
        "fuzzy_score": fuzzy_score,
        "fuzzy_score_percent": round(float(fuzzy_score) * 100) if fuzzy_score is not None else None,
        "match_kind": "Exact" if match and match.get("is_exact_match") else "Closest",
    }
