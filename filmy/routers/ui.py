from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from starlette.responses import HTMLResponse, RedirectResponse

from filmy.app_shared import (
    background_supervisor,
    format_czech_datetime,
    redirect_back,
    signal_metadata_pipeline,
    templates,
)
from filmy.db import (
    add_title_to_user_list,
    clear_user_rating,
    copy_group_to_user_list,
    create_user_list,
    delete_group_from_user_list,
    move_group_between_user_lists,
    record_watch_event,
    record_watch_events_through_episode,
    set_person_affinity_rating,
    set_user_rating,
    update_user_list_description,
)
from filmy.imdb_refresh import get_imdb_refresh_snapshot

router = APIRouter()


@router.post("/ui/list-actions/delete")
async def ui_list_action_delete(
    list_id: str = Form(),
    display_tconst: str = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        delete_group_from_user_list(list_id, display_tconst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    signal_metadata_pipeline("ui_list_delete")
    return redirect_back(return_to)


@router.post("/ui/list-actions/move")
async def ui_list_action_move(
    source_list_id: str = Form(),
    target_list_id: str = Form(),
    display_tconst: str = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        move_group_between_user_lists(source_list_id, target_list_id, display_tconst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    signal_metadata_pipeline("ui_list_move")
    return redirect_back(return_to)


@router.post("/ui/list-actions/copy")
async def ui_list_action_copy(
    source_list_id: str = Form(),
    target_list_id: str = Form(),
    display_tconst: str = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        copy_group_to_user_list(source_list_id, target_list_id, display_tconst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    signal_metadata_pipeline("ui_list_copy")
    return redirect_back(return_to)


@router.post("/ui/list-actions/add")
async def ui_list_action_add(
    tconst: str = Form(),
    target_list_id: str = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        add_title_to_user_list(tconst, target_list_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    signal_metadata_pipeline("ui_list_add")
    return redirect_back(return_to)


@router.post("/ui/list-actions/watched")
async def ui_list_action_watched(
    tconst: str = Form(),
    list_id: str | None = Form(default=None),
    display_tconst: str | None = Form(default=None),
    return_to: str | None = Form(default=None),
):
    try:
        record_watch_event(tconst, add_to_watched_list=True)
        if list_id and display_tconst:
            delete_group_from_user_list(list_id, display_tconst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    signal_metadata_pipeline("ui_mark_watched")
    return redirect_back(return_to)


@router.post("/ui/list-actions/rating")
async def ui_list_action_rating(
    tconst: str = Form(),
    rating: int = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        set_user_rating(tconst, rating)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    signal_metadata_pipeline("ui_rating_set")
    return redirect_back(return_to)


@router.post("/ui/list-actions/rating/clear")
async def ui_list_action_rating_clear(
    tconst: str = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        clear_user_rating(tconst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    signal_metadata_pipeline("ui_rating_clear")
    return redirect_back(return_to)


@router.post("/ui/people/rating")
async def ui_person_affinity_rating(
    nconst: str = Form(),
    rating: int = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        set_person_affinity_rating(nconst, rating)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return redirect_back(return_to)


@router.post("/ui/title-episodes/watched-through")
async def ui_title_episode_watched_through(
    episode_tconst: str = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        record_watch_events_through_episode(episode_tconst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    signal_metadata_pipeline("ui_episode_watched_through")
    return redirect_back(return_to)


@router.post("/ui/lists/create")
async def ui_create_list(name: str = Form(), description: str | None = Form(default=None)):
    try:
        created = create_user_list(name, description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    signal_metadata_pipeline("ui_list_create")
    response = RedirectResponse(url=f"/?list_id={created['id']}#lists-section", status_code=303)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.post("/ui/lists/update-description")
async def ui_update_list_description(
    list_id: str = Form(),
    description: str | None = Form(default=None),
    return_to: str | None = Form(default=None),
):
    try:
        update_user_list_description(list_id, description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    signal_metadata_pipeline("ui_list_update_description")
    return redirect_back(return_to)


@router.get("/ui/cards/background-activity", response_class=HTMLResponse)
async def ui_background_activity_card(request: Request):
    return templates.TemplateResponse(
        request,
        "_background_activity_card.html",
        {
            "background": background_supervisor.homepage_snapshot(),
            "format_czech_datetime": format_czech_datetime,
        },
    )


@router.get("/ui/cards/imdb-refresh-status", response_class=HTMLResponse)
async def ui_imdb_refresh_status_card(request: Request):
    return templates.TemplateResponse(
        request,
        "_imdb_refresh_status.html",
        {
            "refresh_snapshot": get_imdb_refresh_snapshot(),
            "format_czech_datetime": format_czech_datetime,
        },
    )
