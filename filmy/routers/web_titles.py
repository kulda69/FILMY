"""HTML routy pro detail titulu a osob."""

from __future__ import annotations

import logging
from time import perf_counter
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import HTMLResponse

from filmy.app_shared import (
    attach_title_role_signals,
    build_breadcrumb_context,
    build_breadcrumb_target,
    count_missing_portraits,
    detail_return_target,
    format_czech_datetime,
    group_tmdb_providers,
    launch_person_portrait_warmup,
    launch_person_presentation_warmup,
    present_episode_seasons,
    present_main_cast,
    present_title_aliases,
    present_title_episodes,
    signal_metadata_pipeline,
    templates,
)
from filmy.db import (
    get_latest_ai_recommendation_for_title,
    get_local_library_status,
    get_person_presentation,
    get_title_people_panel,
    get_title_presentation,
    get_title_role_signals,
)
from filmy.db_library import TITLE_ROLE_SIGNAL_POLARITY_OPTIONS, TITLE_ROLE_SIGNAL_TYPE_OPTIONS
from filmy.integrations.tmdb import get_tmdb_status

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/titles/{tconst}", response_class=HTMLResponse)
async def title_detail_page(request: Request, tconst: str, return_to: str | None = Query(default=None)):
    """Vykresli detail titulu z lokalni presentation vrstvy."""
    from filmy.routers import web as compat

    started_at = perf_counter()
    presentation = compat.get_title_presentation(tconst)
    if presentation is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")

    compat.signal_metadata_pipeline("title_detail_open", target_tconst=tconst)
    tmdb_status = compat.get_tmdb_status(tconst)
    tmdb_assets = tmdb_status.get("assets") or {}
    background_fetch_pending = not bool(tmdb_status.get("has_mapping")) or any(not asset.get("exists") for asset in tmdb_assets.values())

    breadcrumb_context = build_breadcrumb_context(request, str(presentation["title"]), return_to=return_to)
    parent_return_to = str(breadcrumb_context["page_return_to"])
    role_signals = compat.get_title_role_signals(tconst)
    ai_recommendation = compat.get_latest_ai_recommendation_for_title(tconst)
    main_cast = attach_title_role_signals(present_main_cast(presentation.get("main_cast") or []), role_signals)
    compat.launch_person_presentation_warmup(main_cast)
    compat.launch_person_portrait_warmup(main_cast)
    main_cast_pending_count = count_missing_portraits(main_cast)

    response = templates.TemplateResponse(
        request,
        "title_detail.html",
        {
            "title_item": presentation,
            "title_aliases_display": present_title_aliases(presentation),
            "title_episode_items": present_title_episodes(presentation.get("episodes") or []),
            "title_episode_seasons": present_episode_seasons(presentation.get("episodes") or []),
            "title_main_cast": main_cast,
            "title_role_signals": role_signals,
            "ai_recommendation": ai_recommendation,
            "title_role_signal_type_options": TITLE_ROLE_SIGNAL_TYPE_OPTIONS,
            "title_role_signal_polarity_options": TITLE_ROLE_SIGNAL_POLARITY_OPTIONS,
            "title_main_cast_pending_count": main_cast_pending_count,
            **breadcrumb_context,
            "title_return_to": detail_return_target(f"/titles/{tconst}", parent_return_to),
            "title_action_return_to": f"/titles/{tconst}?{urlencode({'return_to': str(breadcrumb_context['back_url'])})}",
            "poster_url": presentation.get("poster_url"),
            "backdrop_url": presentation.get("backdrop_url"),
            "provider_groups": group_tmdb_providers({"tmdb": {"providers": presentation.get("tmdb_providers") or []}}),
            "tmdb_details": presentation.get("tmdb_details") or {},
            "library_state": presentation.get("library_state") or {},
            "content_state": presentation.get("content_state") or {},
            "title_action_targets": [item for item in compat.get_local_library_status()["visible_lists"] if item.get("item_type") == "list"],
            "background_fetch_pending": background_fetch_pending,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    logger.info("route=/titles/%s elapsed_ms=%.1f", tconst, (perf_counter() - started_at) * 1000)
    return response


@router.get("/titles/{tconst}/main-cast", response_class=HTMLResponse)
async def title_main_cast_partial(request: Request, tconst: str, return_to: str | None = Query(default=None)):
    """Vykresli jen partial main cast bloku pro detail titulu."""
    from filmy.routers import web as compat

    people_panel = compat.get_title_people_panel(tconst)
    if people_panel is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")

    role_signals = compat.get_title_role_signals(tconst)
    main_cast = attach_title_role_signals(present_main_cast(people_panel.get("main_cast") or []), role_signals)
    compat.launch_person_presentation_warmup(main_cast)
    compat.launch_person_portrait_warmup(main_cast)
    response = templates.TemplateResponse(
        request,
        "_title_main_cast.html",
        {
            "title_item": {"tconst": people_panel["tconst"]},
            "title_main_cast": main_cast,
            "title_role_signals": role_signals,
            "title_role_signal_type_options": TITLE_ROLE_SIGNAL_TYPE_OPTIONS,
            "title_role_signal_polarity_options": TITLE_ROLE_SIGNAL_POLARITY_OPTIONS,
            "title_main_cast_pending_count": count_missing_portraits(main_cast),
            "title_return_to": detail_return_target(
                f"/titles/{tconst}",
                return_to or build_breadcrumb_target(f"/titles/{tconst}", label="Title detail"),
            ),
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/people/{nconst}", response_class=HTMLResponse)
async def person_detail_page(request: Request, nconst: str, return_to: str | None = Query(default=None)):
    """Vykresli detail osoby a filmograficke sekce."""
    from filmy.routers import web as compat

    presentation = compat.get_person_presentation(nconst)
    if presentation is None:
        raise HTTPException(status_code=404, detail="Osoba nebyla nalezena.")

    filmography = presentation.get("filmography") or {}
    breadcrumb_context = build_breadcrumb_context(request, str(presentation["name"]), return_to=return_to)
    response = templates.TemplateResponse(
        request,
        "person_detail.html",
        {
            "person_item": presentation,
            **breadcrumb_context,
            "person_return_to": breadcrumb_context["page_return_to"],
            "filmography_sections": [
                {"title": "Directed", "items": filmography.get("directed") or []},
                {"title": "Created by", "items": filmography.get("created") or []},
                {"title": "Written", "items": filmography.get("written") or []},
                {"title": "Acted in", "items": filmography.get("acted") or []},
                {"title": "Other credits", "items": filmography.get("other") or []},
            ],
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response
