"""HTML routy pro system, importy a background stav."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import HTMLResponse, RedirectResponse

from filmy.app_shared import (
    background_supervisor,
    build_breadcrumb_context,
    format_czech_datetime,
    safe_back_target,
    templates,
)
from filmy.db import (
    compute_and_record_genre_scores,
    delete_ai_recommendation_file,
    get_catalog_genres,
    get_favorite_genres,
    get_favorite_traits,
    get_latest_genre_scores,
    import_ai_recommendations_file,
    list_ai_recommendation_files,
    replace_favorite_genres,
    replace_favorite_traits,
)
from filmy.imdb_refresh import get_imdb_refresh_snapshot, start_imdb_refresh_job

from .web_shared import DEFAULT_FAVORITE_TRAITS, PREFERENCE_PRIORITY_MAX, PREFERENCE_PRIORITY_MIN

router = APIRouter()


def _no_store_redirect(url: str) -> RedirectResponse:
    """Vrat redirect vhodny pro administracni/system workflow bez cache."""
    response = RedirectResponse(url=url, status_code=303)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/system/favorite-genres", response_class=HTMLResponse)
async def favorite_genres_page(request: Request, return_to: str | None = Query(default=None), saved: int = Query(default=0)):
    """Vykresli formular oblibenych zanru."""
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

    breadcrumb_context = build_breadcrumb_context(request, "Favorite Genres", return_to=return_to, default_trail=[{"url": "/", "label": "Home"}])
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
    return response


@router.post("/system/favorite-genres")
async def favorite_genres_save(request: Request):
    """Uloz oblibene zanry z formulare."""
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
        if not (PREFERENCE_PRIORITY_MIN <= priority <= PREFERENCE_PRIORITY_MAX):
            raise HTTPException(
                status_code=400,
                detail=f"Priorita pro zanr '{item['genre']}' musi byt v rozsahu {PREFERENCE_PRIORITY_MIN}-{PREFERENCE_PRIORITY_MAX}.",
            )
        favorites.append({"genre": item["genre"], "preference_rank": priority, "weight": 1.0})

    favorites.sort(key=lambda item: (int(item["preference_rank"]), str(item["genre"]).lower()))
    replace_favorite_genres(favorites, source_origin="local_app", source_ref="system.favorite_genres", archive_missing=True)
    return _no_store_redirect(f"/system/favorite-genres?{urlencode({'return_to': return_to, 'saved': 1})}")


@router.get("/system/imdb-refresh", response_class=HTMLResponse)
async def imdb_refresh_page(request: Request, return_to: str | None = Query(default=None), started: int = Query(default=0)):
    """Vykresli stav IMDb refresh jobu."""
    breadcrumb_context = build_breadcrumb_context(request, "IMDb Refresh", return_to=return_to, default_trail=[{"url": "/", "label": "Home"}])
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
    """Spust IMDb refresh a vrat se na stavovou stranku."""
    form = await request.form()
    return_to = safe_back_target(str(form.get("return_to") or "")) or "/system/imdb-refresh"
    start_imdb_refresh_job()
    return _no_store_redirect(f"/system/imdb-refresh?{urlencode({'return_to': return_to, 'started': 1})}")


@router.get("/system/suggestion-scoring", response_class=HTMLResponse)
async def suggestion_scoring_page(
    request: Request,
    return_to: str | None = Query(default=None),
    recomputed: int = Query(default=0),
    error: str | None = Query(default=None),
):
    """Vykresli stranku se scoring snapshotem a manualnim recompute."""
    breadcrumb_context = build_breadcrumb_context(request, "Suggestion Scoring", return_to=return_to, default_trail=[{"url": "/", "label": "Home"}])
    latest_scores = get_latest_genre_scores(limit=8)
    response = templates.TemplateResponse(
        request,
        "suggestion_scoring.html",
        {
            **breadcrumb_context,
            "return_to": breadcrumb_context["page_return_to"],
            "recomputed": bool(recomputed),
            "error_message": str(error or "").strip() or None,
            "latest_scores": latest_scores,
            "favorite_genres_count": len(get_favorite_genres(active_only=True)),
            "favorite_traits_count": len(get_favorite_traits(active_only=True)),
            "format_czech_datetime": format_czech_datetime,
        },
    )
    return response


@router.post("/system/suggestion-scoring/recompute")
async def suggestion_scoring_recompute(request: Request):
    """Rucne prepocitej suggestion scoring."""
    form = await request.form()
    return_to = safe_back_target(str(form.get("return_to") or "")) or "/system/suggestion-scoring"
    try:
        compute_and_record_genre_scores(score_scope="default", source_origin="local_app", source_ref="system.suggestion_scoring")
    except ValueError as exc:
        return _no_store_redirect(f"/system/suggestion-scoring?{urlencode({'return_to': return_to, 'error': str(exc)})}")
    return _no_store_redirect(f"/system/suggestion-scoring?{urlencode({'return_to': return_to, 'recomputed': 1})}")


@router.get("/system/import-ai-suggestions", response_class=HTMLResponse)
async def import_ai_suggestions_page(
    request: Request,
    return_to: str | None = Query(default=None),
    imported: int = Query(default=0),
    already_imported: int = Query(default=0),
    source_filename: str | None = Query(default=None),
    recommendations: int = Query(default=0),
    resolved: int = Query(default=0),
    unresolved: int = Query(default=0),
    list_inserted: int = Query(default=0),
    list_updated: int = Query(default=0),
    deleted: int = Query(default=0),
    deleted_filename: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    """Vykresli importni stranku pro AI suggestion JSON soubory."""
    from filmy.routers import web as compat

    breadcrumb_context = build_breadcrumb_context(request, "Import AI suggestions", return_to=return_to, default_trail=[{"url": "/", "label": "Home"}])
    response = templates.TemplateResponse(
        request,
        "import_ai_suggestions.html",
        {
            **breadcrumb_context,
            "return_to": breadcrumb_context["page_return_to"],
            "files": compat.list_ai_recommendation_files(),
            "imported": bool(imported),
            "already_imported": bool(already_imported),
            "source_filename": str(source_filename or "").strip() or None,
            "recommendations": recommendations,
            "resolved": resolved,
            "unresolved": unresolved,
            "list_inserted": list_inserted,
            "list_updated": list_updated,
            "deleted": bool(deleted),
            "deleted_filename": str(deleted_filename or "").strip() or None,
            "error_message": str(error or "").strip() or None,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.post("/system/import-ai-suggestions/delete")
async def import_ai_suggestions_delete(request: Request):
    """Smaz jeden AI suggestion JSON soubor z importniho adresare."""
    from filmy.routers import web as compat

    form = await request.form()
    return_to = safe_back_target(str(form.get("return_to") or "")) or "/system/import-ai-suggestions"
    filename = str(form.get("filename") or "").strip()
    try:
        result = compat.delete_ai_recommendation_file(filename)
    except (OSError, ValueError) as exc:
        return _no_store_redirect(
            f"/system/import-ai-suggestions?{urlencode({'return_to': return_to, 'source_filename': filename, 'error': str(exc)})}"
        )
    return _no_store_redirect(
        f"/system/import-ai-suggestions?{urlencode({'return_to': return_to, 'deleted': 1, 'deleted_filename': result['filename']})}"
    )


@router.post("/system/import-ai-suggestions")
async def import_ai_suggestions_run(request: Request):
    """Importuj jeden validni AI suggestion JSON soubor."""
    from filmy.routers import web as compat

    form = await request.form()
    return_to = safe_back_target(str(form.get("return_to") or "")) or "/system/import-ai-suggestions"
    filename = str(form.get("filename") or "").strip()
    available_files = {item["filename"]: item for item in compat.list_ai_recommendation_files() if not item.get("error")}
    selected = available_files.get(filename)
    if selected is None:
        return _no_store_redirect(
            f"/system/import-ai-suggestions?{urlencode({'return_to': return_to, 'error': 'Soubor nebyl nalezen nebo neni validni.'})}"
        )

    try:
        result = compat.import_ai_recommendations_file(str(selected["path"]))
    except (OSError, ValueError) as exc:
        return _no_store_redirect(
            f"/system/import-ai-suggestions?{urlencode({'return_to': return_to, 'source_filename': filename, 'error': str(exc)})}"
        )

    query = {
        "return_to": return_to,
        "source_filename": result.get("source_filename") or filename,
        "imported": 0 if result.get("already_imported") else 1,
        "already_imported": 1 if result.get("already_imported") else 0,
        "recommendations": result.get("recommendations") or 0,
        "resolved": result.get("resolved") or 0,
        "unresolved": result.get("unresolved") or 0,
        "list_inserted": result.get("list_inserted") or 0,
        "list_updated": result.get("list_updated") or 0,
    }
    return _no_store_redirect(f"/system/import-ai-suggestions?{urlencode(query)}")


@router.get("/system/favorite-traits", response_class=HTMLResponse)
async def favorite_traits_page(request: Request, return_to: str | None = Query(default=None), saved: int = Query(default=0)):
    """Vykresli formular oblibenych traits."""
    stored_traits = get_favorite_traits(active_only=False)
    stored_lookup = {str(item["trait"]).strip().lower(): item for item in stored_traits if item.get("trait")}

    trait_rows: list[dict[str, object]] = []
    for trait in DEFAULT_FAVORITE_TRAITS:
        existing = stored_lookup.pop(trait.lower(), None)
        trait_rows.append({"trait": trait, "weight": 1.0, "preference_rank": existing.get("preference_rank") if existing else None, "is_active": True})

    extra_rows = sorted(
        stored_lookup.values(),
        key=lambda item: (
            item["preference_rank"] is None,
            item["preference_rank"] if item["preference_rank"] is not None else 10_000,
            str(item["trait"]).lower(),
        ),
    )
    trait_rows.extend(extra_rows)
    for _ in range(6):
        trait_rows.append({"trait": "", "weight": 1.0, "preference_rank": None, "is_active": True})

    breadcrumb_context = build_breadcrumb_context(request, "Favorite Traits", return_to=return_to, default_trail=[{"url": "/", "label": "Home"}])
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


@router.post("/system/favorite-traits")
async def favorite_traits_save(request: Request):
    """Uloz oblibene traits z formulare s validaci priorit."""
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
        priority: int | None = None
        if raw_priority:
            try:
                priority = int(raw_priority)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Radek {index}: priorita musi byt cele cislo.") from exc
            if not (PREFERENCE_PRIORITY_MIN <= priority <= PREFERENCE_PRIORITY_MAX):
                raise HTTPException(
                    status_code=400,
                    detail=f"Radek {index}: priorita musi byt v rozsahu {PREFERENCE_PRIORITY_MIN}-{PREFERENCE_PRIORITY_MAX}.",
                )
        traits.append({"trait": raw_trait, "preference_rank": priority, "weight": 1.0})

    deduped_by_trait: dict[str, dict[str, object]] = {}
    for item in sorted(
        traits,
        key=lambda item: (
            item["preference_rank"] is None,
            int(item["preference_rank"]) if item["preference_rank"] is not None else 10_000,
            str(item["trait"]).lower(),
        ),
    ):
        deduped_by_trait[str(item["trait"])] = item

    replace_favorite_traits(list(deduped_by_trait.values()), source_origin="local_app", source_ref="system.favorite_traits", archive_missing=True)
    return _no_store_redirect(f"/system/favorite-traits?{urlencode({'return_to': return_to, 'saved': 1})}")


@router.get("/system/background-jobs", response_class=HTMLResponse)
async def background_jobs_page(request: Request, return_to: str | None = Query(default=None)):
    """Vykresli stav background jobu."""
    breadcrumb_context = build_breadcrumb_context(request, "Background Jobs", return_to=return_to, default_trail=[{"url": "/", "label": "Home"}])
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
