from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from starlette.responses import PlainTextResponse

from filmy.app_shared import (
    RatingUpdateRequest,
    WatchEventCreateRequest,
    WatchlistUpdateRequest,
    background_supervisor,
    signal_metadata_pipeline,
)
from filmy.db import (
    ASSETS_DIR,
    DB_PATH,
    clear_user_rating,
    commit_import_batch,
    create_import_preview,
    describe_person_by_query,
    describe_title_by_query,
    get_catalog_stats,
    get_content_detail,
    get_imdb_favorite_people,
    get_imdb_lists_status,
    get_imdb_manifest,
    get_imdb_watchlist,
    get_import_batch,
    get_local_library_status,
    get_plex_status,
    get_title_presentation,
    get_watch_history,
    inspect_imdb_lists,
    inspect_plex_source,
    lookup_person_by_query,
    lookup_title_by_query,
    record_watch_event,
    refresh_catalog,
    search_catalog,
    set_user_rating,
    set_watchlist_state,
    sync_imdb_lists,
    sync_plex_source,
    update_content_state,
)
from filmy.integrations.tmdb import (
    TmdbApiError,
    TmdbConfigError,
    enrich_library_from_tmdb,
    fetch_assets_for_title,
    get_enrichment_targets,
    sync_title_from_imdb,
)
from filmy.scripts.materialize_title_details import materialize_title_detail_cache

router = APIRouter()


@router.get("/api")
async def api_root():
    stats = get_catalog_stats()
    return {
        "message": "Filmy API běží",
        "database_path": DB_PATH.as_posix(),
        "assets_path": ASSETS_DIR.as_posix(),
        "catalog_titles": stats["titles"],
        "catalog_episodes": stats["episodes"],
    }


@router.get("/api/catalog/stats")
async def catalog_stats():
    return get_catalog_stats()


@router.get("/api/admin/imdb/manifest")
async def admin_imdb_manifest():
    return {"items": get_imdb_manifest()}


@router.get("/api/admin/imdb/lists/inspect")
async def admin_imdb_lists_inspect(export_dir: str = Query(default="imdb_lists")):
    try:
        return inspect_imdb_lists(export_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/admin/imdb/lists/sync")
async def admin_imdb_lists_sync(export_dir: str = Query(default="imdb_lists")):
    try:
        result = sync_imdb_lists(export_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    signal_metadata_pipeline("admin_imdb_lists_sync")
    return result


@router.get("/api/admin/imdb/lists/status")
async def admin_imdb_lists_status():
    return get_imdb_lists_status()


@router.get("/api/admin/imdb/watchlist")
async def admin_imdb_watchlist(
    limit: int = Query(default=100, ge=1, le=1000),
    active_only: bool = Query(default=True),
):
    return {"items": get_imdb_watchlist(limit=limit, active_only=active_only), "limit": limit, "active_only": active_only}


@router.get("/api/admin/imdb/favorite-people")
async def admin_imdb_favorite_people(
    limit: int = Query(default=100, ge=1, le=1000),
    active_only: bool = Query(default=True),
):
    return {
        "items": get_imdb_favorite_people(limit=limit, active_only=active_only),
        "limit": limit,
        "active_only": active_only,
    }


@router.get("/api/admin/library/status")
async def admin_library_status():
    return get_local_library_status()


@router.get("/api/admin/background/status")
async def admin_background_status():
    return background_supervisor.status()


@router.get("/api/admin/plex/inspect")
async def admin_plex_inspect():
    return inspect_plex_source()


@router.post("/api/admin/plex/sync")
async def admin_plex_sync(
    section_limit: int | None = Query(default=None, ge=1, le=20),
    item_limit_per_section: int | None = Query(default=None, ge=1, le=10000),
):
    try:
        result = sync_plex_source(section_limit=section_limit, item_limit_per_section=item_limit_per_section)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    signal_metadata_pipeline("admin_plex_sync")
    return result


@router.get("/api/admin/plex/status")
async def admin_plex_status():
    return get_plex_status()


@router.get("/api/catalog/search")
async def catalog_search(
    q: str | None = Query(default=None, min_length=1),
    title_type: str | None = Query(default=None, pattern="^(movie|tvMovie|tvSeries|tvMiniSeries)$"),
    limit: int = Query(default=20, ge=1, le=100),
):
    return {"items": search_catalog(query=q, title_type=title_type, limit=limit), "limit": limit}


@router.get("/api/catalog/describe")
async def catalog_describe(
    q: str = Query(min_length=1),
    title_type: str | None = Query(default=None, pattern="^(movie|tvMovie|tvSeries|tvMiniSeries)$"),
):
    item = describe_title_by_query(query=q, title_type=title_type)
    if item is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    return item


@router.get("/api/catalog/lookup")
async def catalog_lookup(
    q: str = Query(min_length=1),
    title_type: str | None = Query(default=None, pattern="^(movie|tvMovie|tvSeries|tvMiniSeries)$"),
    candidates_limit: int = Query(default=5, ge=1, le=20),
):
    item = lookup_title_by_query(query=q, title_type=title_type, candidates_limit=candidates_limit)
    if item is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    return item


@router.get("/api/catalog/lookup/text", response_class=PlainTextResponse)
async def catalog_lookup_text(
    q: str = Query(min_length=1),
    title_type: str | None = Query(default=None, pattern="^(movie|tvMovie|tvSeries|tvMiniSeries)$"),
):
    item = lookup_title_by_query(query=q, title_type=title_type, candidates_limit=1)
    if item is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    return item["selected"]["display_text"]


@router.get("/api/catalog/person/lookup")
async def catalog_person_lookup(
    q: str = Query(min_length=1),
    candidates_limit: int = Query(default=5, ge=1, le=20),
):
    item = lookup_person_by_query(query=q, candidates_limit=candidates_limit)
    if item is None:
        raise HTTPException(status_code=404, detail="Osoba nebyla nalezena.")
    return item


@router.get("/api/catalog/person/lookup/text", response_class=PlainTextResponse)
async def catalog_person_lookup_text(q: str = Query(min_length=1)):
    item = describe_person_by_query(q)
    if item is None:
        raise HTTPException(status_code=404, detail="Osoba nebyla nalezena.")
    return item["display_text"]


@router.post("/api/admin/imdb/rebuild")
async def admin_imdb_rebuild():
    result = {"status": "ok", "stats": refresh_catalog()}
    signal_metadata_pipeline("admin_imdb_rebuild")
    return result


@router.get("/api/admin/content/{tconst}")
async def admin_content_detail(tconst: str):
    detail = get_content_detail(tconst)
    if detail is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    return detail


@router.get("/api/catalog/presentation/{tconst}")
async def catalog_presentation(tconst: str):
    item = get_title_presentation(tconst)
    if item is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    return item


@router.post("/api/admin/content/{tconst}/state")
async def admin_update_content_state(
    tconst: str,
    interest_state: str = Query(pattern="^(previewed|in_progress|watched)$"),
):
    detail = get_content_detail(tconst)
    if detail is None:
        raise HTTPException(status_code=404, detail="Titul nebyl nalezen.")
    result = update_content_state(tconst, interest_state)
    signal_metadata_pipeline("admin_content_state")
    return result


@router.post("/api/library/content/{tconst}/watchlist")
async def library_update_watchlist(tconst: str, payload: WatchlistUpdateRequest):
    try:
        result = set_watchlist_state(tconst, in_watchlist=payload.in_watchlist, notes=payload.notes)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    signal_metadata_pipeline("api_watchlist_update")
    return result


@router.post("/api/library/content/{tconst}/rating")
async def library_set_rating(tconst: str, payload: RatingUpdateRequest):
    try:
        result = set_user_rating(tconst, payload.rating)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if "nebyl nalezen" in detail else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    signal_metadata_pipeline("api_rating_set")
    return result


@router.delete("/api/library/content/{tconst}/rating")
async def library_clear_rating(tconst: str):
    try:
        result = clear_user_rating(tconst)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    signal_metadata_pipeline("api_rating_clear")
    return result


@router.post("/api/library/content/{tconst}/watch")
async def library_record_watch(tconst: str, payload: WatchEventCreateRequest):
    try:
        result = record_watch_event(tconst, watched_on=payload.watched_on, notes=payload.notes)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    signal_metadata_pipeline("api_watch_record")
    return result


@router.post("/api/admin/tmdb/sync/{tconst}")
async def admin_tmdb_sync(tconst: str, locale: str = Query(default="en-US")):
    try:
        return sync_title_from_imdb(tconst, locale=locale)
    except TmdbConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except TmdbApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/admin/tmdb/assets/fetch/{tconst}")
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


@router.get("/api/admin/tmdb/library/targets")
async def admin_tmdb_library_targets(limit: int | None = Query(default=None, ge=1, le=5000)):
    return {"items": get_enrichment_targets(limit=limit), "limit": limit}


@router.post("/api/admin/tmdb/library/enrich")
async def admin_tmdb_library_enrich(limit: int | None = Query(default=None, ge=1, le=5000)):
    try:
        return enrich_library_from_tmdb(limit=limit)
    except TmdbConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except TmdbApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/admin/cache/title-details/materialize")
async def admin_materialize_title_details(
    limit: int | None = Query(default=None, ge=1, le=50000),
    rewrite: bool = Query(default=False),
):
    """Archival one-shot repair for older title detail JSON cache files."""

    return materialize_title_detail_cache(limit=limit, rewrite=rewrite)


@router.post("/api/admin/import/netflix/preview")
async def admin_import_netflix_preview(
    file: UploadFile = File(...),
    max_rows: int | None = Query(default=None, ge=1, le=10000),
):
    content = await file.read()
    return create_import_preview("netflix", file.filename or "netflix.csv", content, max_rows=max_rows)


@router.get("/api/admin/import/{batch_id}")
async def admin_import_batch(batch_id: str):
    batch = get_import_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch nebyl nalezen.")
    return batch


@router.post("/api/admin/import/commit/{batch_id}")
async def admin_import_commit(batch_id: str):
    try:
        result = commit_import_batch(batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    signal_metadata_pipeline("admin_import_commit")
    return result


@router.get("/api/admin/history")
async def admin_watch_history(
    limit: int = Query(default=100, ge=1, le=1000),
    source: str | None = Query(default=None),
):
    return {"items": get_watch_history(limit=limit, source=source), "limit": limit, "source": source}
