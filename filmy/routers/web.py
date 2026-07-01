from __future__ import annotations

from math import ceil
from urllib.parse import quote_plus, urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import HTMLResponse, RedirectResponse

from filmy.app_shared import (
    background_supervisor,
    build_breadcrumb_context,
    build_breadcrumb_target,
    card_action_move_targets,
    count_missing_portraits,
    detail_return_target,
    format_czech_datetime,
    group_tmdb_providers,
    launch_homepage_warmup,
    launch_person_portrait_warmup,
    present_episode_seasons,
    present_main_cast,
    present_person_search_result_card,
    present_search_result_card,
    present_title_aliases,
    present_title_episodes,
    safe_back_target,
    selected_panel_page,
    templates,
    tmdb_asset_url,
)
from filmy.config import get_ui_config
from filmy.db import (
    get_catalog_genres,
    get_content_detail,
    get_continue_watching_items,
    get_favorite_genres,
    get_favorite_traits,
    get_hot_watchlist_page,
    get_latest_genre_scores,
    get_local_library_status,
    get_person_presentation,
    get_recently_watched_page,
    get_title_presentation,
    get_user_list_items_page,
    get_watched_page,
    lookup_person_by_query,
    lookup_title_by_query,
    replace_favorite_genres,
    replace_favorite_traits,
    update_user_list_description,
)
from filmy.imdb_refresh import get_imdb_refresh_snapshot, start_imdb_refresh_job

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def root(request: Request, list_id: str | None = Query(default=None)):
    ui_config = get_ui_config()
    library_status = get_local_library_status()
    visible_lists = library_status["visible_lists"]
    selected_list = None
    if visible_lists:
        if list_id is not None:
            selected_list = next((item for item in visible_lists if item["id"] == list_id), None)
        if selected_list is None:
            selected_list = next((item for item in visible_lists if item["list_kind"] == "watchlist"), None)
        if selected_list is None:
            selected_list = visible_lists[0]
    selected_list_limit = ui_config.my_lists_selected_limit
    continue_limit = ui_config.continue_watching_limit
    selected_list_page = selected_panel_page(selected_list, limit=selected_list_limit)
    continue_watching = get_continue_watching_items(limit=continue_limit)
    latest_genre_scores = get_latest_genre_scores(limit=8)
    launch_homepage_warmup([item["tconst"] for item in continue_watching] + [item["tconst"] for item in selected_list_page["items"]])
    selected_list_show_all_url = None
    if selected_list:
        if selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "watched":
            selected_list_show_all_url = "/views/watched"
        elif selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "hot_watchlist":
            selected_list_show_all_url = "/views/hot-watchlist"
        elif selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "recently_watched":
            selected_list_show_all_url = "/views/recently-watched"
        else:
            selected_list_show_all_url = f"/lists/{selected_list['id']}"
    home_crumb = {"url": "/", "label": "Home"}
    selected_list_return_to = (
        build_breadcrumb_target(
            f"/?list_id={selected_list['id']}#lists-section",
            trail=[home_crumb],
            label=str(selected_list["name"]),
        )
        if selected_list
        else build_breadcrumb_target("/#lists-section", trail=[home_crumb], label="Home")
    )
    continue_watching_return_to = build_breadcrumb_target(
        "/",
        trail=[home_crumb],
        label="Continue Watching",
        fragment="continue-watching-rail",
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
            "continue_watching": continue_watching,
            "continue_watching_return_to": continue_watching_return_to,
            "suggestion_scores": latest_genre_scores["items"] if latest_genre_scores else [],
            "suggestion_scores_generated_at": latest_genre_scores["generated_at"] if latest_genre_scores else None,
            "background": background_supervisor.homepage_snapshot(),
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/search", response_class=HTMLResponse)
async def search_results_page(
    request: Request,
    q: str | None = Query(default=None, min_length=1),
    mode: str = Query(default="auto", pattern="^(auto|wide)$"),
    title_type: str | None = Query(default=None, pattern="^(movie|tvMovie|tvSeries|tvMiniSeries)$"),
):
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

    if searched_query:
        lookup = lookup_title_by_query(query=searched_query, title_type=title_type, candidates_limit=candidates_limit)
        if lookup is not None:
            selected = dict(lookup["selected"])
            primary_result = present_search_result_card(
                selected,
                match=selected.get("match"),
                return_to=page_return_to,
            )

            scored_candidates = sorted(
                [candidate for candidate in lookup["candidates"] if candidate.get("fuzzy_score") is not None],
                key=lambda item: float(item.get("fuzzy_score") or 0.0),
                reverse=True,
            )
            for candidate in scored_candidates:
                if candidate["tconst"] == lookup["selected_tconst"]:
                    continue
                candidate_presentation = get_title_presentation(candidate["tconst"])
                if candidate_presentation is None:
                    continue
                alternate_results.append(
                    present_search_result_card(
                        candidate_presentation,
                        match=candidate,
                        return_to=page_return_to,
                    )
                )
        person_lookup = lookup_person_by_query(query=searched_query, candidates_limit=candidates_limit)
        if person_lookup is not None:
            selected_person = dict(person_lookup["selected"])
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

    response = templates.TemplateResponse(
        request,
        "search_results.html",
        {
            **breadcrumb_context,
            "nav_search_query": searched_query,
            "search_query": searched_query,
            "search_mode": mode,
            "search_title_type": title_type,
            "search_lookup": lookup,
            "search_primary_result": primary_result,
            "search_alternate_results": alternate_results,
            "person_search_lookup": person_lookup,
            "person_search_primary_result": person_primary_result,
            "person_search_alternate_results": person_alternate_results,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/lists/{list_id}", response_class=HTMLResponse)
async def list_detail(request: Request, list_id: str, page: int = Query(default=1, ge=1)):
    limit = 50
    offset = (page - 1) * limit
    list_page = get_user_list_items_page(list_id, limit=limit, offset=offset)
    selected_list = list_page["list"]
    if selected_list is None:
        raise HTTPException(status_code=404, detail="Seznam nebyl nalezen.")

    total = list_page["total"]
    total_pages = max(ceil(total / limit), 1)
    current_page = min(page, total_pages)
    if current_page != page:
        offset = (current_page - 1) * limit
        list_page = get_user_list_items_page(list_id, limit=limit, offset=offset)

    launch_homepage_warmup([item["tconst"] for item in list_page["items"]])
    list_return_to = build_breadcrumb_target(
        f"/lists/{list_id}?page={current_page}",
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
            "selected_list_prev_url": f"/lists/{list_id}?page={current_page - 1}" if current_page > 1 else None,
            "selected_list_next_url": f"/lists/{list_id}?page={current_page + 1}" if current_page < total_pages else None,
            "selected_list_move_targets": card_action_move_targets(get_local_library_status()["visible_lists"], selected_list),
            "selected_list_actions_enabled": True,
            "selected_list_return_to": list_return_to,
            "selected_list_detail_return_to": list_return_to,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/views/recently-watched", response_class=HTMLResponse)
async def recently_watched_detail(request: Request, page: int = Query(default=1, ge=1)):
    limit = 50
    offset = (page - 1) * limit
    list_page = get_recently_watched_page(limit=limit, offset=offset)
    selected_list = list_page["list"]
    total = list_page["total"]
    total_pages = max(ceil(total / limit), 1)
    current_page = min(page, total_pages)
    if current_page != page:
        offset = (current_page - 1) * limit
        list_page = get_recently_watched_page(limit=limit, offset=offset)

    launch_homepage_warmup([item["tconst"] for item in list_page["items"]])
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
            "selected_list_move_targets": card_action_move_targets(get_local_library_status()["visible_lists"], selected_list),
            "selected_list_actions_enabled": True,
            "selected_list_return_to": list_return_to,
            "selected_list_detail_return_to": list_return_to,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/views/hot-watchlist", response_class=HTMLResponse)
async def hot_watchlist_detail(request: Request, page: int = Query(default=1, ge=1)):
    limit = 50
    offset = (page - 1) * limit
    list_page = get_hot_watchlist_page(limit=limit, offset=offset)
    selected_list = list_page["list"]
    total = list_page["total"]
    total_pages = max(ceil(total / limit), 1)
    current_page = min(page, total_pages)
    if current_page != page:
        offset = (current_page - 1) * limit
        list_page = get_hot_watchlist_page(limit=limit, offset=offset)

    launch_homepage_warmup([item["tconst"] for item in list_page["items"]])
    list_return_to = build_breadcrumb_target(
        f"/views/hot-watchlist?page={current_page}",
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
            "selected_list_prev_url": f"/views/hot-watchlist?page={current_page - 1}" if current_page > 1 else None,
            "selected_list_next_url": f"/views/hot-watchlist?page={current_page + 1}" if current_page < total_pages else None,
            "selected_list_move_targets": card_action_move_targets(get_local_library_status()["visible_lists"], selected_list),
            "selected_list_actions_enabled": True,
            "selected_list_return_to": list_return_to,
            "selected_list_detail_return_to": list_return_to,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/views/watched", response_class=HTMLResponse)
async def watched_detail(request: Request, page: int = Query(default=1, ge=1)):
    limit = 50
    offset = (page - 1) * limit
    list_page = get_watched_page(limit=limit, offset=offset)
    selected_list = list_page["list"]
    total = list_page["total"]
    total_pages = max(ceil(total / limit), 1)
    current_page = min(page, total_pages)
    if current_page != page:
        offset = (current_page - 1) * limit
        list_page = get_watched_page(limit=limit, offset=offset)

    launch_homepage_warmup([item["tconst"] for item in list_page["items"]])
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
            "selected_list_move_targets": card_action_move_targets(get_local_library_status()["visible_lists"], selected_list),
            "selected_list_actions_enabled": True,
            "selected_list_return_to": list_return_to,
            "selected_list_detail_return_to": list_return_to,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/system/favorite-genres", response_class=HTMLResponse)
async def favorite_genres_page(
    request: Request,
    return_to: str | None = Query(default=None),
    saved: int = Query(default=0),
):
    favorite_items = get_favorite_genres(active_only=False)
    favorite_by_genre = {item["genre"]: item for item in favorite_items}
    genre_rows = []
    for item in get_catalog_genres():
        favorite = favorite_by_genre.get(item["genre"])
        genre_rows.append(
            {
                "genre": item["genre"],
                "title_count": item["title_count"],
                "priority": favorite["preference_rank"] if favorite and favorite.get("is_active") else None,
                "is_favorite": bool(favorite and favorite.get("is_active")),
            }
        )

    genre_rows.sort(
        key=lambda item: (
            item["priority"] is None,
            item["priority"] if item["priority"] is not None else 10_000,
            item["genre"].lower(),
        )
    )

    breadcrumb_context = build_breadcrumb_context(
        request,
        "Favorite Genres",
        return_to=return_to,
        default_trail=[{"url": "/", "label": "Home"}],
    )
    response = templates.TemplateResponse(
        request,
        "favorite_genres.html",
        {
            **breadcrumb_context,
            "return_to": breadcrumb_context["page_return_to"],
            "saved": bool(saved),
            "genre_rows": genre_rows,
            "favorite_count": sum(1 for item in genre_rows if item["priority"] is not None),
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/system/imdb-refresh", response_class=HTMLResponse)
async def imdb_refresh_page(
    request: Request,
    return_to: str | None = Query(default=None),
    started: int = Query(default=0),
):
    breadcrumb_context = build_breadcrumb_context(
        request,
        "IMDb Refresh",
        return_to=return_to,
        default_trail=[{"url": "/", "label": "Home"}],
    )
    snapshot = get_imdb_refresh_snapshot()
    response = templates.TemplateResponse(
        request,
        "imdb_refresh.html",
        {
            **breadcrumb_context,
            "return_to": breadcrumb_context["page_return_to"],
            "started": bool(started),
            "refresh_snapshot": snapshot,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.post("/system/imdb-refresh/start")
async def imdb_refresh_start(request: Request):
    form = await request.form()
    return_to = safe_back_target(str(form.get("return_to") or "")) or "/system/imdb-refresh"
    start_imdb_refresh_job()
    response = RedirectResponse(
        url=f"/system/imdb-refresh?{urlencode({'return_to': return_to, 'started': 1})}",
        status_code=303,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.post("/system/favorite-genres")
async def favorite_genres_save(request: Request):
    form = await request.form()
    return_to = safe_back_target(str(form.get("return_to") or "")) or "/"

    favorites: list[dict[str, object]] = []
    for item in get_catalog_genres():
        raw_priority = str(form.get(f"priority_{item['genre']}") or "").strip()
        if not raw_priority:
            continue
        try:
            priority = int(raw_priority)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Priorita pro zanr '{item['genre']}' musi byt cele cislo.") from exc
        if priority <= 0:
            raise HTTPException(status_code=400, detail=f"Priorita pro zanr '{item['genre']}' musi byt vetsi nez nula.")
        favorites.append(
            {
                "genre": item["genre"],
                "preference_rank": priority,
                "weight": 1.0,
            }
        )

    favorites.sort(key=lambda item: (int(item["preference_rank"]), str(item["genre"]).lower()))
    replace_favorite_genres(
        favorites,
        source_origin="local_app",
        source_ref="system.favorite_genres",
        archive_missing=True,
    )
    response = RedirectResponse(
        url=f"/system/favorite-genres?{urlencode({'return_to': return_to, 'saved': 1})}",
        status_code=303,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/system/favorite-traits", response_class=HTMLResponse)
async def favorite_traits_page(
    request: Request,
    return_to: str | None = Query(default=None),
    saved: int = Query(default=0),
):
    trait_rows = sorted(
        get_favorite_traits(active_only=False),
        key=lambda item: (
            item["preference_rank"] is None,
            item["preference_rank"] if item["preference_rank"] is not None else 10_000,
            str(item["trait"]).lower(),
        ),
    )
    for _ in range(8):
        trait_rows.append(
            {
                "trait": "",
                "weight": 1.0,
                "preference_rank": None,
                "is_active": True,
            }
        )

    breadcrumb_context = build_breadcrumb_context(
        request,
        "Favorite Traits",
        return_to=return_to,
        default_trail=[{"url": "/", "label": "Home"}],
    )
    response = templates.TemplateResponse(
        request,
        "favorite_traits.html",
        {
            **breadcrumb_context,
            "return_to": breadcrumb_context["page_return_to"],
            "saved": bool(saved),
            "trait_rows": trait_rows,
            "favorite_count": sum(1 for item in trait_rows if item.get("trait") and item.get("preference_rank") is not None),
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/system/background-jobs", response_class=HTMLResponse)
async def background_jobs_page(
    request: Request,
    return_to: str | None = Query(default=None),
):
    breadcrumb_context = build_breadcrumb_context(
        request,
        "Background Jobs",
        return_to=return_to,
        default_trail=[{"url": "/", "label": "Home"}],
    )
    response = templates.TemplateResponse(
        request,
        "background_jobs.html",
        {
            **breadcrumb_context,
            "background": background_supervisor.homepage_snapshot(),
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.post("/system/favorite-traits")
async def favorite_traits_save(request: Request):
    form = await request.form()
    return_to = safe_back_target(str(form.get("return_to") or "")) or "/"

    traits: list[dict[str, object]] = []
    for index in range(1, 65):
        raw_trait = str(form.get(f"trait_{index}") or "").strip()
        raw_priority = str(form.get(f"priority_{index}") or "").strip()
        if not raw_trait and not raw_priority:
            continue
        if not raw_trait:
            raise HTTPException(status_code=400, detail=f"Radek {index}: chybi nazev traitu.")
        if not raw_priority:
            raise HTTPException(status_code=400, detail=f"Radek {index}: chybi priorita.")
        try:
            priority = int(raw_priority)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Radek {index}: priorita musi byt cele cislo.") from exc
        if priority <= 0:
            raise HTTPException(status_code=400, detail=f"Radek {index}: priorita musi byt vetsi nez nula.")
        traits.append(
            {
                "trait": raw_trait,
                "preference_rank": priority,
                "weight": 1.0,
            }
        )

    deduped_by_trait: dict[str, dict[str, object]] = {}
    for item in sorted(traits, key=lambda item: (int(item["preference_rank"]), str(item["trait"]).lower())):
        deduped_by_trait[str(item["trait"])] = item

    replace_favorite_traits(
        list(deduped_by_trait.values()),
        source_origin="local_app",
        source_ref="system.favorite_traits",
        archive_missing=True,
    )
    response = RedirectResponse(
        url=f"/system/favorite-traits?{urlencode({'return_to': return_to, 'saved': 1})}",
        status_code=303,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/titles/{tconst}", response_class=HTMLResponse)
async def title_detail_page(request: Request, tconst: str, return_to: str | None = Query(default=None)):
    presentation = get_title_presentation(tconst)
    detail = get_content_detail(tconst)
    if presentation is None or detail is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")

    breadcrumb_context = build_breadcrumb_context(request, str(presentation["title"]), return_to=return_to)
    parent_return_to = str(breadcrumb_context["page_return_to"])
    main_cast = present_main_cast(presentation.get("main_cast") or [])
    launch_person_portrait_warmup(main_cast)
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
            "title_main_cast_pending_count": main_cast_pending_count,
            "title_detail": detail,
            **breadcrumb_context,
            "title_return_to": detail_return_target(f"/titles/{tconst}", parent_return_to),
            "poster_url": presentation.get("poster_url"),
            "backdrop_url": tmdb_asset_url(detail, "backdrop"),
            "provider_groups": group_tmdb_providers(detail),
            "tmdb_details": ((detail.get("tmdb") or {}).get("details") or {}),
            "library_state": presentation.get("library_state") or {},
            "content_state": detail.get("content_state") or {},
            "title_action_targets": [item for item in get_local_library_status()["visible_lists"] if item.get("item_type") == "list"],
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/titles/{tconst}/main-cast", response_class=HTMLResponse)
async def title_main_cast_partial(request: Request, tconst: str, return_to: str | None = Query(default=None)):
    presentation = get_title_presentation(tconst)
    if presentation is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")

    main_cast = present_main_cast(presentation.get("main_cast") or [])
    launch_person_portrait_warmup(main_cast)
    response = templates.TemplateResponse(
        request,
        "_title_main_cast.html",
        {
            "title_item": presentation,
            "title_main_cast": main_cast,
            "title_main_cast_pending_count": count_missing_portraits(main_cast),
            "title_return_to": detail_return_target(
                f"/titles/{tconst}",
                return_to or build_breadcrumb_target(f"/titles/{tconst}", label=str(presentation["title"])),
            ),
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/people/{nconst}", response_class=HTMLResponse)
async def person_detail_page(request: Request, nconst: str, return_to: str | None = Query(default=None)):
    presentation = get_person_presentation(nconst)
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
