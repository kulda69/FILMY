"""Homepage a dashboard HTML routy."""

from __future__ import annotations

import logging
from time import perf_counter
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from starlette.responses import HTMLResponse

from filmy.app_shared import (
    apply_html_cache_headers,
    background_supervisor,
    build_breadcrumb_target,
    card_action_move_targets,
    format_czech_datetime,
    launch_homepage_warmup,
    selected_panel_page,
    templates,
)
from filmy.config import get_ui_config
from filmy.db import get_favorite_traits, get_latest_genre_scores, get_local_library_status, get_user_list_items_page
from filmy.db_library import AI_INPUT_ROLE_OPTIONS

from .web_shared import list_filter_params

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/", response_class=HTMLResponse)
async def root(request: Request, list_id: str | None = Query(default=None), available_in_cz: bool = Query(default=False)):
    """Vykresli domovku s vybranym list panelem a AI navrhy."""
    from filmy.routers import web as compat

    started_at = perf_counter()
    ui_config = get_ui_config()
    library_status = compat.get_local_library_status()
    visible_lists = library_status["visible_lists"]
    filter_params = list_filter_params(available_in_cz=available_in_cz)
    selected_list = None
    if visible_lists:
        if list_id is not None:
            selected_list = next((item for item in visible_lists if item["id"] == list_id), None)
        if selected_list is None:
            selected_list = next((item for item in visible_lists if item["list_kind"] == "watchlist"), None)
        if selected_list is None:
            selected_list = visible_lists[0]
    selected_list_limit = ui_config.my_lists_selected_limit
    ai_suggestions_limit = ui_config.continue_watching_limit
    selected_list_page = compat.selected_panel_page(selected_list, limit=selected_list_limit, available_in_cz=available_in_cz)
    ai_suggestions_page = compat.get_user_list_items_page("ai-suggestions", limit=ai_suggestions_limit)
    ai_suggestions_items = ai_suggestions_page["items"]
    active_traits = compat.get_favorite_traits(active_only=True)
    latest_genre_scores = compat.get_latest_genre_scores(limit=8)
    selected_list_show_all_url = None
    if selected_list:
        if selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "watched":
            selected_list_show_all_url = "/views/watched"
        elif selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "hot_watchlist":
            selected_list_show_all_url = f"/views/hot-watchlist?{urlencode(filter_params)}" if filter_params else "/views/hot-watchlist"
        elif selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "recently_watched":
            selected_list_show_all_url = "/views/recently-watched"
        else:
            selected_list_show_all_url = f"/lists/{selected_list['id']}?{urlencode(filter_params)}" if filter_params else f"/lists/{selected_list['id']}"
    home_crumb = {"url": "/", "label": "Home"}
    selected_list_return_to = (
        build_breadcrumb_target(
            f"/?{urlencode({'list_id': selected_list['id'], **filter_params})}#lists-section",
            trail=[home_crumb],
            label=str(selected_list["name"]),
        )
        if selected_list
        else build_breadcrumb_target("/#lists-section", trail=[home_crumb], label="Home")
    )
    ai_suggestions_return_to = build_breadcrumb_target(
        "/",
        trail=[home_crumb],
        label="AI návrhy",
        fragment="ai-suggestions-rail",
    )
    compat.launch_homepage_warmup(
        [item["tconst"] for item in ai_suggestions_items] + [item["tconst"] for item in selected_list_page["items"]]
    )
    response = templates.TemplateResponse(
        request,
        "home.html",
        {
            "library_status": library_status,
            "selected_list": selected_list,
            "selected_list_items": selected_list_page["items"],
            "selected_list_total": selected_list_page["total"],
            "selected_list_limit": selected_list_page["limit"],
            "selected_list_has_more": selected_list_page["total"] > selected_list_page["limit"],
            "selected_list_show_all_url": selected_list_show_all_url,
            "selected_list_move_targets": card_action_move_targets(visible_lists, selected_list),
            "selected_list_actions_enabled": bool(selected_list),
            "selected_list_return_to": selected_list_return_to,
            "selected_list_detail_return_to": selected_list_return_to,
            "selected_list_filters": selected_list_page.get("filters") or {"available_in_cz": available_in_cz},
            "ai_input_role_options": AI_INPUT_ROLE_OPTIONS,
            "ai_suggestions_items": ai_suggestions_items,
            "ai_suggestions_total": ai_suggestions_page["total"],
            "ai_suggestions_limit": ai_suggestions_page["limit"],
            "ai_suggestions_has_more": ai_suggestions_page["total"] > ai_suggestions_page["limit"],
            "ai_suggestions_return_to": ai_suggestions_return_to,
            "suggestion_scores_generated_at": latest_genre_scores["generated_at"] if latest_genre_scores else None,
            "favorite_traits_active_count": len(active_traits),
            "background": compat.background_supervisor.homepage_snapshot(),
            "format_czech_datetime": format_czech_datetime,
        },
    )
    logger.info("route=/ selected_list=%r elapsed_ms=%.1f", list_id, (perf_counter() - started_at) * 1000)
    return apply_html_cache_headers(response)
