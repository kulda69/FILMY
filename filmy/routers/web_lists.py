"""HTML routy pro uzivatelske seznamy a odvozene pohledy."""

from __future__ import annotations

from math import ceil
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import HTMLResponse

from filmy.app_shared import (
    apply_html_cache_headers,
    build_breadcrumb_target,
    card_action_move_targets,
    format_czech_datetime,
    launch_homepage_warmup,
    templates,
)
from filmy.db import get_hot_watchlist_page, get_local_library_status, get_recently_watched_page, get_user_list_items_page, get_watched_page
from filmy.db_library import AI_INPUT_ROLE_OPTIONS

from .web_shared import list_filter_params

router = APIRouter()


@router.get("/lists/{list_id}", response_class=HTMLResponse)
async def list_detail(request: Request, list_id: str, page: int = Query(default=1, ge=1), available_in_cz: bool = Query(default=False)):
    """Vykresli strankovany detail uzivatelskeho seznamu."""
    from filmy.routers import web as compat

    limit = 50
    offset = (page - 1) * limit
    filter_params = list_filter_params(available_in_cz=available_in_cz)
    list_page = compat.get_user_list_items_page(list_id, limit=limit, offset=offset, available_in_cz=available_in_cz)
    selected_list = list_page["list"]
    if selected_list is None:
        raise HTTPException(status_code=404, detail="Seznam nebyl nalezen.")

    total = list_page["total"]
    total_pages = max(ceil(total / limit), 1)
    current_page = min(page, total_pages)
    if current_page != page:
        offset = (current_page - 1) * limit
        list_page = compat.get_user_list_items_page(list_id, limit=limit, offset=offset, available_in_cz=available_in_cz)

    list_return_to = build_breadcrumb_target(
        f"/lists/{list_id}?{urlencode({'page': current_page, **filter_params})}",
        trail=[{"url": "/", "label": "Home"}],
        label=str(selected_list["name"]),
    )
    response = templates.TemplateResponse(
        request,
        "list_detail.html",
        {
            "selected_list": selected_list,
            "selected_list_items": list_page["items"],
            "selected_list_total": total,
            "selected_list_limit": limit,
            "selected_list_page": current_page,
            "selected_list_total_pages": total_pages,
            "selected_list_has_previous": current_page > 1,
            "selected_list_has_next": current_page < total_pages,
            "selected_list_prev_url": f"/lists/{list_id}?{urlencode({'page': current_page - 1, **filter_params})}" if current_page > 1 else None,
            "selected_list_next_url": f"/lists/{list_id}?{urlencode({'page': current_page + 1, **filter_params})}" if current_page < total_pages else None,
            "selected_list_move_targets": card_action_move_targets(compat.get_local_library_status()["visible_lists"], selected_list),
            "selected_list_actions_enabled": True,
            "selected_list_return_to": list_return_to,
            "selected_list_detail_return_to": list_return_to,
            "selected_list_filters": list_page.get("filters") or {"available_in_cz": available_in_cz},
            "ai_input_role_options": AI_INPUT_ROLE_OPTIONS,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    return apply_html_cache_headers(response)


@router.get("/views/recently-watched", response_class=HTMLResponse)
async def recently_watched_detail(request: Request, page: int = Query(default=1, ge=1)):
    """Vykresli strankovany pohled na nedavno videne polozky."""
    from filmy.routers import web as compat

    limit = 50
    offset = (page - 1) * limit
    list_page = compat.get_recently_watched_page(limit=limit, offset=offset)
    selected_list = list_page["list"]
    total = list_page["total"]
    total_pages = max(ceil(total / limit), 1)
    current_page = min(page, total_pages)
    if current_page != page:
        offset = (current_page - 1) * limit
        list_page = compat.get_recently_watched_page(limit=limit, offset=offset)

    list_return_to = build_breadcrumb_target(
        f"/views/recently-watched?page={current_page}",
        trail=[{"url": "/", "label": "Home"}],
        label="Recently Watched",
    )
    response = templates.TemplateResponse(
        request,
        "list_detail.html",
        {
            "selected_list": selected_list,
            "selected_list_items": list_page["items"],
            "selected_list_total": total,
            "selected_list_limit": limit,
            "selected_list_page": current_page,
            "selected_list_total_pages": total_pages,
            "selected_list_has_previous": current_page > 1,
            "selected_list_has_next": current_page < total_pages,
            "selected_list_prev_url": f"/views/recently-watched?page={current_page - 1}" if current_page > 1 else None,
            "selected_list_next_url": f"/views/recently-watched?page={current_page + 1}" if current_page < total_pages else None,
            "selected_list_move_targets": card_action_move_targets(compat.get_local_library_status()["visible_lists"], selected_list),
            "selected_list_actions_enabled": True,
            "selected_list_return_to": list_return_to,
            "selected_list_detail_return_to": list_return_to,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    return apply_html_cache_headers(response)


@router.get("/views/hot-watchlist", response_class=HTMLResponse)
async def hot_watchlist_detail(request: Request, page: int = Query(default=1, ge=1), available_in_cz: bool = Query(default=False)):
    """Vykresli strankovany pohled na hot watchlist s CZ filtrem."""
    from filmy.routers import web as compat

    limit = 50
    offset = (page - 1) * limit
    filter_params = list_filter_params(available_in_cz=available_in_cz)
    list_page = compat.get_hot_watchlist_page(limit=limit, offset=offset, available_in_cz=available_in_cz)
    selected_list = list_page["list"]
    total = list_page["total"]
    total_pages = max(ceil(total / limit), 1)
    current_page = min(page, total_pages)
    if current_page != page:
        offset = (current_page - 1) * limit
        list_page = compat.get_hot_watchlist_page(limit=limit, offset=offset, available_in_cz=available_in_cz)

    list_return_to = build_breadcrumb_target(
        f"/views/hot-watchlist?{urlencode({'page': current_page, **filter_params})}",
        trail=[{"url": "/", "label": "Home"}],
        label="Hot Watchlist",
    )
    response = templates.TemplateResponse(
        request,
        "list_detail.html",
        {
            "selected_list": selected_list,
            "selected_list_items": list_page["items"],
            "selected_list_total": total,
            "selected_list_limit": limit,
            "selected_list_page": current_page,
            "selected_list_total_pages": total_pages,
            "selected_list_has_previous": current_page > 1,
            "selected_list_has_next": current_page < total_pages,
            "selected_list_prev_url": f"/views/hot-watchlist?{urlencode({'page': current_page - 1, **filter_params})}" if current_page > 1 else None,
            "selected_list_next_url": f"/views/hot-watchlist?{urlencode({'page': current_page + 1, **filter_params})}" if current_page < total_pages else None,
            "selected_list_move_targets": card_action_move_targets(compat.get_local_library_status()["visible_lists"], selected_list),
            "selected_list_actions_enabled": True,
            "selected_list_return_to": list_return_to,
            "selected_list_detail_return_to": list_return_to,
            "selected_list_filters": list_page.get("filters") or {"available_in_cz": available_in_cz},
            "format_czech_datetime": format_czech_datetime,
        },
    )
    return apply_html_cache_headers(response)


@router.get("/views/watched", response_class=HTMLResponse)
async def watched_detail(request: Request, page: int = Query(default=1, ge=1)):
    """Vykresli strankovany pohled na videne tituly."""
    from filmy.routers import web as compat

    limit = 50
    offset = (page - 1) * limit
    list_page = compat.get_watched_page(limit=limit, offset=offset)
    selected_list = list_page["list"]
    total = list_page["total"]
    total_pages = max(ceil(total / limit), 1)
    current_page = min(page, total_pages)
    if current_page != page:
        offset = (current_page - 1) * limit
        list_page = compat.get_watched_page(limit=limit, offset=offset)

    compat.launch_homepage_warmup([item["tconst"] for item in list_page["items"]])
    list_return_to = build_breadcrumb_target(
        f"/views/watched?page={current_page}",
        trail=[{"url": "/", "label": "Home"}],
        label="Watched",
    )
    response = templates.TemplateResponse(
        request,
        "list_detail.html",
        {
            "selected_list": selected_list,
            "selected_list_items": list_page["items"],
            "selected_list_total": total,
            "selected_list_limit": limit,
            "selected_list_page": current_page,
            "selected_list_total_pages": total_pages,
            "selected_list_has_previous": current_page > 1,
            "selected_list_has_next": current_page < total_pages,
            "selected_list_prev_url": f"/views/watched?page={current_page - 1}" if current_page > 1 else None,
            "selected_list_next_url": f"/views/watched?page={current_page + 1}" if current_page < total_pages else None,
            "selected_list_move_targets": card_action_move_targets(compat.get_local_library_status()["visible_lists"], selected_list),
            "selected_list_actions_enabled": True,
            "selected_list_return_to": list_return_to,
            "selected_list_detail_return_to": list_return_to,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    return apply_html_cache_headers(response)
