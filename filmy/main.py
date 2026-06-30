from __future__ import annotations

from contextlib import asynccontextmanager
import threading
import time
from math import ceil
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import RedirectResponse
from starlette.responses import HTMLResponse
from starlette.responses import PlainTextResponse
from starlette.templating import Jinja2Templates

from filmy.background_jobs import BackgroundJobSupervisor
from filmy.config import get_ui_config
from filmy.db import (
    ASSETS_DIR,
    DB_PATH,
    RECENTLY_WATCHED_VIEW_ID,
    WATCHED_VIEW_ID,
    clear_user_rating,
    copy_group_to_user_list,
    commit_import_batch,
    create_import_preview,
    create_user_list,
    delete_group_from_user_list,
    ensure_database,
    get_catalog_genres,
    get_catalog_stats,
    get_content_detail,
    format_czech_datetime,
    get_favorite_genres,
    get_favorite_traits,
    get_imdb_favorite_people,
    get_imdb_manifest,
    get_imdb_lists_status,
    get_imdb_watchlist,
    get_import_batch,
    get_latest_genre_scores,
    get_trakt_collection,
    get_trakt_list_overview,
    get_trakt_ratings,
    get_trakt_sync_changes,
    get_trakt_sync_run,
    get_trakt_sync_runs,
    get_trakt_status,
    get_watch_history,
    inspect_imdb_lists,
    inspect_trakt_export,
    get_local_library_status,
    get_continue_watching_items,
    get_person_presentation,
    get_recently_watched_page,
    get_watched_page,
    get_user_list_items_page,
    get_plex_status,
    move_group_between_user_lists,
    describe_person_by_query,
    get_title_presentation,
    lookup_person_by_query,
    lookup_title_by_query,
    record_watch_event,
    record_watch_events_through_episode,
    refresh_catalog,
    replace_favorite_genres,
    replace_favorite_traits,
    search_catalog,
    set_user_rating,
    set_watchlist_state,
    inspect_plex_source,
    sync_plex_source,
    sync_imdb_lists,
    sync_trakt_export,
    update_content_state,
    update_user_list_description,
    describe_title_by_query,
)
from filmy.integrations.tmdb import (
    TmdbApiError,
    TmdbConfigError,
    enrich_library_from_tmdb,
    fetch_assets_for_title,
    fetch_person_portrait,
    get_enrichment_targets,
    sync_title_from_imdb,
)
from filmy.paths import PEOPLE_ASSETS_DIR, PROJECT_ROOT

background_supervisor = BackgroundJobSupervisor()
templates = Jinja2Templates(directory=(PROJECT_ROOT / "templates").as_posix())
_homepage_warmup_lock = threading.Lock()
_homepage_warmup_thread: threading.Thread | None = None
_person_portrait_warmup_lock = threading.Lock()
_person_portrait_warmup_active: set[str] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    background_supervisor.cleanup_orphan_processes()
    ensure_database()
    background_supervisor.start()
    try:
        yield
    finally:
        background_supervisor.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/assets/tmdb", StaticFiles(directory=ASSETS_DIR.as_posix()), name="tmdb_assets")
app.mount("/assets/people", StaticFiles(directory=PEOPLE_ASSETS_DIR.as_posix()), name="people_assets")


class WatchlistUpdateRequest(BaseModel):
    """Local watchlist toggle for one title or episode."""

    in_watchlist: bool
    notes: str | None = None


class RatingUpdateRequest(BaseModel):
    """Local user rating on the 1-10 IMDb-like scale."""

    rating: int = Field(ge=1, le=10)


class WatchEventCreateRequest(BaseModel):
    """Append a local watch event."""

    watched_on: str | None = Field(default=None, description="ISO date YYYY-MM-DD.")
    notes: str | None = None


def _launch_homepage_warmup(tconsts: list[str]) -> None:
    unique_tconsts = tuple(dict.fromkeys(tconsts))
    if not unique_tconsts:
        return

    def run() -> None:
        try:
            for tconst in unique_tconsts:
                get_title_presentation(tconst)
        finally:
            global _homepage_warmup_thread
            with _homepage_warmup_lock:
                _homepage_warmup_thread = None

    global _homepage_warmup_thread
    with _homepage_warmup_lock:
        if _homepage_warmup_thread is not None and _homepage_warmup_thread.is_alive():
            return
        _homepage_warmup_thread = threading.Thread(target=run, name="homepage-cache-warmup", daemon=True)
        _homepage_warmup_thread.start()


def _present_main_cast(main_cast: list[dict[str, object]]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for person in main_cast:
        item = dict(person)
        nconst = str(person.get("nconst") or "").strip()
        if nconst:
            presentation = get_person_presentation(nconst)
            if presentation is not None:
                item["portrait_url"] = presentation.get("portrait_url")
                item["has_portrait"] = presentation.get("has_portrait", False)
            else:
                item["portrait_url"] = None
                item["has_portrait"] = False
        else:
            item["portrait_url"] = None
            item["has_portrait"] = False
        items.append(item)
    return items


def _launch_person_portrait_warmup(main_cast: list[dict[str, object]]) -> None:
    missing_nconsts = [
        str(person.get("nconst") or "").strip()
        for person in main_cast
        if person.get("nconst") and not person.get("has_portrait")
    ]
    unique_nconsts = [nconst for nconst in dict.fromkeys(missing_nconsts) if nconst]
    if not unique_nconsts:
        return

    def run(nconst: str) -> None:
        try:
            fetch_person_portrait(nconst, fetch_reason="title_detail_main_cast_priority")
        except Exception:
            pass
        finally:
            with _person_portrait_warmup_lock:
                _person_portrait_warmup_active.discard(nconst)

    for nconst in unique_nconsts:
        with _person_portrait_warmup_lock:
            if nconst in _person_portrait_warmup_active:
                continue
            _person_portrait_warmup_active.add(nconst)
        thread = threading.Thread(
            target=run,
            args=(nconst,),
            name=f"person-portrait-warmup-{nconst}",
            daemon=True,
        )
        thread.start()


def _count_missing_portraits(main_cast: list[dict[str, object]]) -> int:
    return sum(1 for person in main_cast if not person.get("has_portrait"))


def _alias_bucket(alias: dict[str, object]) -> str | None:
    language = str(alias.get("language") or "").strip().lower()
    region = str(alias.get("region") or "").strip().upper()
    if language == "en" or region in {"US", "GB", "CA", "IE", "AU", "NZ", "IN"}:
        return "en"
    if language == "cs" or region == "CZ":
        return "cs"
    if language == "es" or region == "ES":
        return "es"
    if language == "de" or region == "DE":
        return "de"
    return None


def _present_title_aliases(presentation: dict[str, object]) -> list[dict[str, object]]:
    aliases = presentation.get("aliases") or []
    buckets: dict[str, list[dict[str, object]]] = {key: [] for key in ("en", "cs", "es", "de")}
    for alias in aliases:
        if isinstance(alias, dict):
            bucket = _alias_bucket(alias)
            if bucket is not None:
                buckets[bucket].append(alias)

    original_title = str(presentation.get("original_title") or "").strip().casefold()
    title = str(presentation.get("title") or "").strip().casefold()
    selected: list[dict[str, object]] = []
    seen_titles: set[str] = set()
    for key in ("en", "cs", "es", "de"):
        candidates = buckets[key]
        if not candidates:
            continue
        chosen = next(
            (
                alias
                for alias in candidates
                if str(alias.get("language") or "").strip().lower() == key
            ),
            candidates[0],
        )
        alias_title = str(chosen.get("title") or "").strip()
        if not alias_title:
            continue
        normalized = alias_title.casefold()
        if normalized in seen_titles or normalized in {title, original_title}:
            continue
        seen_titles.add(normalized)
        selected.append(chosen)
    return selected


def _present_episode_seasons(episodes: list[object]) -> list[int]:
    seasons: list[int] = []
    seen: set[int] = set()
    for episode in episodes:
        if not isinstance(episode, (list, tuple)) or len(episode) < 2:
            continue
        season_number = episode[1]
        if isinstance(season_number, int) and season_number not in seen:
            seen.add(season_number)
            seasons.append(season_number)
    seasons.sort()
    return seasons


def _present_title_episodes(episodes: list[object]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for episode in episodes:
        if not isinstance(episode, (list, tuple)) or len(episode) < 5:
            continue
        items.append(
            {
                "tconst": episode[0],
                "season_number": episode[1],
                "episode_number": episode[2],
                "title": episode[3],
                "year": episode[4],
                "user_rating": episode[5] if len(episode) > 5 else None,
                "watched_count": episode[6] if len(episode) > 6 else 0,
            }
        )
    return items


def _selected_panel_page(selected_list: dict[str, object] | None, limit: int, offset: int = 0) -> dict[str, object]:
    if selected_list is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset, "list": None}
    if selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "watched":
        return get_watched_page(limit=limit, offset=offset)
    if selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "recently_watched":
        return get_recently_watched_page(limit=limit, offset=offset)
    return get_user_list_items_page(str(selected_list["id"]), limit=limit, offset=offset)


def _card_action_move_targets(visible_lists: list[dict[str, object]], selected_list: dict[str, object] | None) -> list[dict[str, object]]:
    selected_id = selected_list.get("id") if selected_list else None
    return [
        item
        for item in visible_lists
        if item.get("item_type") == "list" and item["id"] != selected_id
    ]


def _redirect_back(return_to: str | None) -> RedirectResponse:
    target = return_to or "/"
    parts = urlsplit(target)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query_pairs.append(("_ts", str(time.time_ns())))
    refreshed = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment))
    response = RedirectResponse(url=refreshed, status_code=303)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def _safe_back_target(candidate: str | None) -> str | None:
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return None
    if not parsed.path.startswith("/"):
        return None
    return urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))


def _request_back_target(request: Request, return_to: str | None = None) -> str:
    explicit = _safe_back_target(return_to)
    if explicit:
        return explicit

    referer = request.headers.get("referer")
    if referer:
        parsed = urlsplit(referer)
        if parsed.path.startswith("/"):
            return urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))
    return "/"


def _detail_return_target(path: str, parent_return_to: str, *, season: int | None = None, fragment: str | None = None) -> str:
    query_pairs: list[tuple[str, str]] = [("return_to", parent_return_to)]
    if season is not None:
        query_pairs.append(("season", str(season)))
    return urlunsplit(("", "", path, urlencode(query_pairs), fragment or ""))


def _tmdb_asset_url(detail: dict[str, object] | None, asset_kind: str) -> str | None:
    tmdb = (detail or {}).get("tmdb") or {}
    assets = tmdb.get("assets") or []
    for asset in assets:
        if asset.get("asset_kind") != asset_kind:
            continue
        local_path = asset.get("local_path")
        if not local_path:
            continue
        asset_path = str(local_path)
        marker = "/data/assets/tmdb/"
        if marker in asset_path:
            relative = asset_path.split(marker, 1)[1]
            return f"/assets/tmdb/{relative}"
    return None


def _group_tmdb_providers(detail: dict[str, object] | None) -> list[dict[str, object]]:
    type_labels = {
        "flatrate": "Stream",
        "free": "Free",
        "ads": "With ads",
        "rent": "Rent",
        "buy": "Buy",
    }
    grouped: dict[str, list[str]] = {}
    for provider in (((detail or {}).get("tmdb") or {}).get("providers") or []):
        provider_type = str(provider.get("provider_type") or "").strip()
        provider_name = str(provider.get("provider_name") or "").strip()
        if not provider_type or not provider_name:
            continue
        grouped.setdefault(provider_type, [])
        if provider_name not in grouped[provider_type]:
            grouped[provider_type].append(provider_name)

    ordered_types = ["flatrate", "free", "ads", "rent", "buy"]
    return [
        {
            "type": provider_type,
            "label": type_labels.get(provider_type, provider_type.replace("_", " ").title()),
            "providers": grouped[provider_type],
        }
        for provider_type in ordered_types
        if grouped.get(provider_type)
    ]


@app.get("/", response_class=HTMLResponse)
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
    selected_list_page = _selected_panel_page(selected_list, limit=selected_list_limit)
    continue_watching = get_continue_watching_items(limit=continue_limit)
    latest_genre_scores = get_latest_genre_scores(limit=8)
    _launch_homepage_warmup([item["tconst"] for item in continue_watching] + [item["tconst"] for item in selected_list_page["items"]])
    selected_list_show_all_url = None
    if selected_list:
        if selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "watched":
            selected_list_show_all_url = "/views/watched"
        elif selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "recently_watched":
            selected_list_show_all_url = "/views/recently-watched"
        else:
            selected_list_show_all_url = f"/lists/{selected_list['id']}"
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
            "selected_list_move_targets": _card_action_move_targets(visible_lists, selected_list),
            "selected_list_actions_enabled": bool(selected_list and selected_list.get("item_type") == "list"),
            "selected_list_return_to": f"/?list_id={selected_list['id']}#lists-section" if selected_list else "/#lists-section",
            "continue_watching": continue_watching,
            "suggestion_scores": latest_genre_scores["items"] if latest_genre_scores else [],
            "suggestion_scores_generated_at": latest_genre_scores["generated_at"] if latest_genre_scores else None,
            "background": background_supervisor.homepage_snapshot(),
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/lists/{list_id}", response_class=HTMLResponse)
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

    _launch_homepage_warmup([item["tconst"] for item in list_page["items"]])
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
            "selected_list_move_targets": _card_action_move_targets(get_local_library_status()["visible_lists"], selected_list),
            "selected_list_actions_enabled": True,
            "selected_list_return_to": f"/lists/{list_id}?page={current_page}",
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/views/recently-watched", response_class=HTMLResponse)
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

    _launch_homepage_warmup([item["tconst"] for item in list_page["items"]])
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
            "selected_list_move_targets": [],
            "selected_list_actions_enabled": False,
            "selected_list_return_to": f"/views/recently-watched?page={current_page}",
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/views/watched", response_class=HTMLResponse)
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

    _launch_homepage_warmup([item["tconst"] for item in list_page["items"]])
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
            "selected_list_move_targets": [],
            "selected_list_actions_enabled": False,
            "selected_list_return_to": f"/views/watched?page={current_page}",
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/system/favorite-genres", response_class=HTMLResponse)
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

    safe_return_to = _request_back_target(request, return_to)
    response = templates.TemplateResponse(
        request,
        "favorite_genres.html",
        {
            "back_url": safe_return_to,
            "return_to": safe_return_to,
            "saved": bool(saved),
            "genre_rows": genre_rows,
            "favorite_count": sum(1 for item in genre_rows if item["priority"] is not None),
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/system/favorite-genres")
async def favorite_genres_save(request: Request):
    form = await request.form()
    return_to = _safe_back_target(str(form.get("return_to") or "")) or "/"

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


@app.get("/system/favorite-traits", response_class=HTMLResponse)
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

    safe_return_to = _request_back_target(request, return_to)
    response = templates.TemplateResponse(
        request,
        "favorite_traits.html",
        {
            "back_url": safe_return_to,
            "return_to": safe_return_to,
            "saved": bool(saved),
            "trait_rows": trait_rows,
            "favorite_count": sum(1 for item in trait_rows if item.get("trait") and item.get("preference_rank") is not None),
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/system/favorite-traits")
async def favorite_traits_save(request: Request):
    form = await request.form()
    return_to = _safe_back_target(str(form.get("return_to") or "")) or "/"

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


@app.get("/titles/{tconst}", response_class=HTMLResponse)
async def title_detail_page(request: Request, tconst: str, return_to: str | None = Query(default=None)):
    presentation = get_title_presentation(tconst)
    detail = get_content_detail(tconst)
    if presentation is None or detail is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")

    parent_return_to = _request_back_target(request, return_to)
    main_cast = _present_main_cast(presentation.get("main_cast") or [])
    _launch_person_portrait_warmup(main_cast)
    main_cast_pending_count = _count_missing_portraits(main_cast)

    response = templates.TemplateResponse(
        request,
        "title_detail.html",
        {
            "title_item": presentation,
            "title_aliases_display": _present_title_aliases(presentation),
            "title_episode_items": _present_title_episodes(presentation.get("episodes") or []),
            "title_episode_seasons": _present_episode_seasons(presentation.get("episodes") or []),
            "title_main_cast": main_cast,
            "title_main_cast_pending_count": main_cast_pending_count,
            "title_detail": detail,
            "back_url": parent_return_to,
            "title_return_to": _detail_return_target(f"/titles/{tconst}", parent_return_to),
            "poster_url": presentation.get("poster_url"),
            "backdrop_url": _tmdb_asset_url(detail, "backdrop"),
            "provider_groups": _group_tmdb_providers(detail),
            "tmdb_details": ((detail.get("tmdb") or {}).get("details") or {}),
            "library_state": presentation.get("library_state") or {},
            "content_state": detail.get("content_state") or {},
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/titles/{tconst}/main-cast", response_class=HTMLResponse)
async def title_main_cast_partial(request: Request, tconst: str):
    presentation = get_title_presentation(tconst)
    if presentation is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")

    main_cast = _present_main_cast(presentation.get("main_cast") or [])
    _launch_person_portrait_warmup(main_cast)
    response = templates.TemplateResponse(
        request,
        "_title_main_cast.html",
        {
            "title_item": presentation,
            "title_main_cast": main_cast,
            "title_main_cast_pending_count": _count_missing_portraits(main_cast),
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/people/{nconst}", response_class=HTMLResponse)
async def person_detail_page(request: Request, nconst: str, return_to: str | None = Query(default=None)):
    presentation = get_person_presentation(nconst)
    if presentation is None:
        raise HTTPException(status_code=404, detail="Osoba nebyla nalezena.")

    filmography = presentation.get("filmography") or {}
    response = templates.TemplateResponse(
        request,
        "person_detail.html",
        {
            "person_item": presentation,
            "back_url": _request_back_target(request, return_to),
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


@app.post("/ui/list-actions/delete")
async def ui_list_action_delete(
    list_id: str = Form(),
    display_tconst: str = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        delete_group_from_user_list(list_id, display_tconst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _redirect_back(return_to)


@app.post("/ui/list-actions/move")
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
    return _redirect_back(return_to)


@app.post("/ui/list-actions/copy")
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
    return _redirect_back(return_to)


@app.post("/ui/list-actions/watched")
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
    return _redirect_back(return_to)


@app.post("/ui/list-actions/rating")
async def ui_list_action_rating(
    tconst: str = Form(),
    rating: int = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        set_user_rating(tconst, rating)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _redirect_back(return_to)


@app.post("/ui/list-actions/rating/clear")
async def ui_list_action_rating_clear(
    tconst: str = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        clear_user_rating(tconst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _redirect_back(return_to)


@app.post("/ui/title-episodes/watched-through")
async def ui_title_episode_watched_through(
    episode_tconst: str = Form(),
    return_to: str | None = Form(default=None),
):
    try:
        record_watch_events_through_episode(episode_tconst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _redirect_back(return_to)


@app.post("/ui/lists/create")
async def ui_create_list(name: str = Form(), description: str | None = Form(default=None)):
    try:
        created = create_user_list(name, description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = RedirectResponse(url=f"/?list_id={created['id']}#lists-section", status_code=303)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/ui/lists/update-description")
async def ui_update_list_description(
    list_id: str = Form(),
    description: str | None = Form(default=None),
    return_to: str | None = Form(default=None),
):
    try:
        update_user_list_description(list_id, description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _redirect_back(return_to)


@app.get("/ui/cards/background-activity", response_class=HTMLResponse)
async def ui_background_activity_card(request: Request):
    return templates.TemplateResponse(
        request,
        "_background_activity_card.html",
        {
            "background": background_supervisor.homepage_snapshot(),
            "format_czech_datetime": format_czech_datetime,
        },
    )


@app.get("/api")
async def api_root():
    stats = get_catalog_stats()
    return {
        "message": "Filmy API běží",
        "database_path": DB_PATH.as_posix(),
        "assets_path": ASSETS_DIR.as_posix(),
        "catalog_titles": stats["titles"],
        "catalog_episodes": stats["episodes"],
    }


@app.get("/api/catalog/stats")
async def catalog_stats():
    return get_catalog_stats()


@app.get("/api/admin/imdb/manifest")
async def admin_imdb_manifest():
    return {"items": get_imdb_manifest()}


@app.get("/api/admin/imdb/lists/inspect")
async def admin_imdb_lists_inspect(export_dir: str = Query(default="imdb_lists")):
    try:
        return inspect_imdb_lists(export_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/admin/imdb/lists/sync")
async def admin_imdb_lists_sync(export_dir: str = Query(default="imdb_lists")):
    try:
        return sync_imdb_lists(export_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/admin/imdb/lists/status")
async def admin_imdb_lists_status():
    return get_imdb_lists_status()


@app.get("/api/admin/imdb/watchlist")
async def admin_imdb_watchlist(
    limit: int = Query(default=100, ge=1, le=1000),
    active_only: bool = Query(default=True),
):
    return {"items": get_imdb_watchlist(limit=limit, active_only=active_only), "limit": limit, "active_only": active_only}


@app.get("/api/admin/imdb/favorite-people")
async def admin_imdb_favorite_people(
    limit: int = Query(default=100, ge=1, le=1000),
    active_only: bool = Query(default=True),
):
    return {
        "items": get_imdb_favorite_people(limit=limit, active_only=active_only),
        "limit": limit,
        "active_only": active_only,
    }


@app.get("/api/admin/library/status")
async def admin_library_status():
    return get_local_library_status()


@app.get("/api/admin/background/status")
async def admin_background_status():
    return background_supervisor.status()


@app.get("/api/admin/plex/inspect")
async def admin_plex_inspect():
    return inspect_plex_source()


@app.post("/api/admin/plex/sync")
async def admin_plex_sync(
    section_limit: int | None = Query(default=None, ge=1, le=20),
    item_limit_per_section: int | None = Query(default=None, ge=1, le=10000),
):
    try:
        return sync_plex_source(section_limit=section_limit, item_limit_per_section=item_limit_per_section)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/admin/plex/status")
async def admin_plex_status():
    return get_plex_status()


@app.get("/api/catalog/search")
async def catalog_search(
    q: str | None = Query(default=None, min_length=1),
    title_type: str | None = Query(default=None, pattern="^(movie|tvMovie|tvSeries|tvMiniSeries)$"),
    limit: int = Query(default=20, ge=1, le=100),
):
    return {"items": search_catalog(query=q, title_type=title_type, limit=limit), "limit": limit}


@app.get("/api/catalog/describe")
async def catalog_describe(
    q: str = Query(min_length=1),
    title_type: str | None = Query(default=None, pattern="^(movie|tvMovie|tvSeries|tvMiniSeries)$"),
):
    item = describe_title_by_query(query=q, title_type=title_type)
    if item is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    return item


@app.get("/api/catalog/lookup")
async def catalog_lookup(
    q: str = Query(min_length=1),
    title_type: str | None = Query(default=None, pattern="^(movie|tvMovie|tvSeries|tvMiniSeries)$"),
    candidates_limit: int = Query(default=5, ge=1, le=20),
):
    item = lookup_title_by_query(query=q, title_type=title_type, candidates_limit=candidates_limit)
    if item is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    return item


@app.get("/api/catalog/lookup/text", response_class=PlainTextResponse)
async def catalog_lookup_text(
    q: str = Query(min_length=1),
    title_type: str | None = Query(default=None, pattern="^(movie|tvMovie|tvSeries|tvMiniSeries)$"),
):
    item = lookup_title_by_query(query=q, title_type=title_type, candidates_limit=1)
    if item is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    return item["selected"]["display_text"]


@app.get("/api/catalog/person/lookup")
async def catalog_person_lookup(
    q: str = Query(min_length=1),
    candidates_limit: int = Query(default=5, ge=1, le=20),
):
    item = lookup_person_by_query(query=q, candidates_limit=candidates_limit)
    if item is None:
        raise HTTPException(status_code=404, detail="Osoba nebyla nalezena.")
    return item


@app.get("/api/catalog/person/lookup/text", response_class=PlainTextResponse)
async def catalog_person_lookup_text(q: str = Query(min_length=1)):
    item = describe_person_by_query(q)
    if item is None:
        raise HTTPException(status_code=404, detail="Osoba nebyla nalezena.")
    return item["display_text"]


@app.post("/api/admin/imdb/rebuild")
async def admin_imdb_rebuild():
    return {"status": "ok", "stats": refresh_catalog()}


@app.get("/api/admin/content/{tconst}")
async def admin_content_detail(tconst: str):
    detail = get_content_detail(tconst)
    if detail is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    return detail


@app.get("/api/catalog/presentation/{tconst}")
async def catalog_presentation(tconst: str):
    item = get_title_presentation(tconst)
    if item is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    return item


@app.post("/api/admin/content/{tconst}/state")
async def admin_update_content_state(
    tconst: str,
    interest_state: str = Query(pattern="^(previewed|in_progress|watched)$"),
):
    detail = get_content_detail(tconst)
    if detail is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    return update_content_state(tconst, interest_state)


@app.post("/api/library/content/{tconst}/watchlist")
async def library_update_watchlist(tconst: str, payload: WatchlistUpdateRequest):
    try:
        return set_watchlist_state(tconst, in_watchlist=payload.in_watchlist, notes=payload.notes)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/library/content/{tconst}/rating")
async def library_set_rating(tconst: str, payload: RatingUpdateRequest):
    try:
        return set_user_rating(tconst, payload.rating)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if "nebyl nalezen" in detail else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.delete("/api/library/content/{tconst}/rating")
async def library_clear_rating(tconst: str):
    try:
        return clear_user_rating(tconst)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/library/content/{tconst}/watch")
async def library_record_watch(tconst: str, payload: WatchEventCreateRequest):
    try:
        return record_watch_event(tconst, watched_on=payload.watched_on, notes=payload.notes)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/admin/tmdb/sync/{tconst}")
async def admin_tmdb_sync(tconst: str, locale: str = Query(default="en-US")):
    try:
        return sync_title_from_imdb(tconst, locale=locale)
    except TmdbConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except TmdbApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/admin/tmdb/assets/fetch/{tconst}")
async def admin_tmdb_fetch_assets(
    tconst: str,
    fetch_reason: str = Query(pattern="^(previewed|in_progress|watched)$"),
):
    try:
        return fetch_assets_for_title(tconst, fetch_reason=fetch_reason)
    except TmdbConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except TmdbApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/admin/tmdb/library/targets")
async def admin_tmdb_library_targets(limit: int | None = Query(default=None, ge=1, le=5000)):
    return {"items": get_enrichment_targets(limit=limit), "limit": limit}


@app.post("/api/admin/tmdb/library/enrich")
async def admin_tmdb_library_enrich(limit: int | None = Query(default=None, ge=1, le=5000)):
    try:
        return enrich_library_from_tmdb(limit=limit)
    except TmdbConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except TmdbApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/admin/import/netflix/preview")
async def admin_import_netflix_preview(
    file: UploadFile = File(...),
    max_rows: int | None = Query(default=None, ge=1, le=10000),
):
    content = await file.read()
    return create_import_preview("netflix", file.filename or "netflix.csv", content, max_rows=max_rows)


@app.post("/api/admin/import/trakt/preview")
async def admin_import_trakt_preview(
    file: UploadFile = File(...),
    max_rows: int | None = Query(default=None, ge=1, le=10000),
):
    content = await file.read()
    return create_import_preview("trakt", file.filename or "trakt.csv", content, max_rows=max_rows)


@app.get("/api/admin/import/{batch_id}")
async def admin_import_batch(batch_id: str):
    batch = get_import_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch nebyl nalezen.")
    return batch


@app.post("/api/admin/import/commit/{batch_id}")
async def admin_import_commit(batch_id: str):
    try:
        return commit_import_batch(batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/admin/import/trakt-export/inspect")
async def admin_trakt_export_inspect(export_dir: str = Query(default="trakt-export")):
    try:
        return inspect_trakt_export(export_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/admin/import/trakt-export/sync")
async def admin_trakt_export_sync(export_dir: str = Query(default="trakt-export")):
    try:
        return sync_trakt_export(export_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/admin/trakt/syncs")
async def admin_trakt_sync_runs(limit: int = Query(default=20, ge=1, le=100)):
    return {"items": get_trakt_sync_runs(limit=limit), "limit": limit}


@app.get("/api/admin/trakt/status")
async def admin_trakt_status():
    return get_trakt_status()


@app.get("/api/admin/trakt/syncs/{sync_run_id}")
async def admin_trakt_sync_run(sync_run_id: str):
    item = get_trakt_sync_run(sync_run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Trakt sync nebyl nalezen.")
    return item


@app.get("/api/admin/trakt/changes")
async def admin_trakt_changes(
    sync_run_id: str | None = Query(default=None),
    previous_sync_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    return get_trakt_sync_changes(sync_run_id=sync_run_id, previous_sync_id=previous_sync_id, limit=limit)


@app.get("/api/admin/history")
async def admin_watch_history(
    limit: int = Query(default=100, ge=1, le=1000),
    source: str | None = Query(default=None),
):
    return {"items": get_watch_history(limit=limit, source=source), "limit": limit, "source": source}


@app.get("/api/admin/trakt/ratings")
async def admin_trakt_ratings(
    limit: int = Query(default=100, ge=1, le=1000),
    active_only: bool = Query(default=True),
):
    return {"items": get_trakt_ratings(limit=limit, active_only=active_only), "limit": limit, "active_only": active_only}


@app.get("/api/admin/trakt/lists")
async def admin_trakt_lists(
    include_items: bool = Query(default=False),
    active_only: bool = Query(default=True),
):
    return get_trakt_list_overview(include_items=include_items, active_only=active_only)


@app.get("/api/admin/trakt/collection")
async def admin_trakt_collection(
    limit: int = Query(default=100, ge=1, le=1000),
    active_only: bool = Query(default=True),
):
    return {"items": get_trakt_collection(limit=limit, active_only=active_only), "limit": limit, "active_only": active_only}
