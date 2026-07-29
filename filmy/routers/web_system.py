"""HTML routy pro system, importy a background stav."""

from __future__ import annotations

from urllib.parse import urlencode
import uuid

from fastapi import APIRouter, Form, HTTPException, Query, Request
import psycopg
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
from filmy.runtime_postgres import (
    delete_list_action_rule,
    fetch_list_action_rule,
    fetch_list_action_rules,
    fetch_user_list,
    fetch_user_lists,
    upsert_list_action_rule,
)

from .web_shared import DEFAULT_FAVORITE_TRAITS, PREFERENCE_PRIORITY_MAX, PREFERENCE_PRIORITY_MIN

router = APIRouter()

LIST_RULE_TRIGGER_ORDER: tuple[str, ...] = (
    "set_rating",
    "mark_watched",
    "copy_to_list",
    "move_to_list",
)

LIST_RULE_TRIGGER_LABELS = {
    "set_rating": "Set Rating",
    "mark_watched": "Mark Watched",
    "copy_to_list": "Copy To List",
    "move_to_list": "Move To List",
}

LIST_RULE_EFFECT_LABELS = {
    "write_rating": "Write rating",
    "derive_watched": "Derive watched",
    "write_watched": "Write watched",
    "add_target_membership": "Add target membership",
    "deactivate_source_membership": "Deactivate source membership",
    "preserve_source_membership": "Preserve source membership",
    "preserve_target_membership": "Preserve target membership",
    "remove_source_membership": "Remove source membership",
    "remove_target_membership": "Remove target membership",
    "noop": "No-op",
}

LIST_RULE_PHASE_LABELS = {
    "immediate": "Immediate",
    "finalize_only": "Finalize only",
}

LIST_RULE_TRIGGER_HELP = {
    "set_rating": "Akce bez cíle. Typicky zapisuje rating a odvozené watched efekty.",
    "mark_watched": "Akce bez cíle. Typicky zapisuje watched a source cleanup.",
    "copy_to_list": "Akce s konkrétním cílovým listem. Zdroj typicky zůstává zachovaný.",
    "move_to_list": "Akce s konkrétním cílovým listem. Zdroj typicky končí deaktivací.",
}

LIST_RULE_TRIGGER_EFFECT_OPTIONS = {
    "set_rating": (
        "write_rating",
        "derive_watched",
        "write_watched",
        "deactivate_source_membership",
        "preserve_source_membership",
        "noop",
    ),
    "mark_watched": (
        "write_watched",
        "deactivate_source_membership",
        "preserve_source_membership",
        "noop",
    ),
    "copy_to_list": (
        "add_target_membership",
        "preserve_source_membership",
        "preserve_target_membership",
        "remove_target_membership",
        "noop",
    ),
    "move_to_list": (
        "add_target_membership",
        "deactivate_source_membership",
        "remove_source_membership",
        "preserve_target_membership",
        "remove_target_membership",
        "noop",
    ),
}

LIST_KIND_LABELS = {
    "watchlist": "System",
    "custom": "Custom",
    "view": "View",
}


def _ai_role_label(value: str | None) -> str:
    """Preved AI input roli na lidsky citelny kratky label."""

    labels = {
        "strong_positive": "Silně se mi líbí",
        "interested_owned": "Mám",
        "interested_planned": "Chci vidět",
        "in_progress": "Rozkoukáno",
        "negative": "Nechci",
        "external_suggestion": "Návrh od AI",
        "ignore": "Nepoužívat",
    }
    return labels.get(str(value or "").strip(), "—")


def _build_rule_overview_rows(filter_kind: str) -> list[dict[str, object]]:
    """Sloz overview radky listu vcetne rule souhrnu pro system editor."""

    rows: list[dict[str, object]] = []
    for list_row in fetch_user_lists():
        list_kind = str(list_row.get("list_kind") or "")
        if filter_kind == "custom" and list_kind != "custom":
            continue
        if filter_kind == "system" and list_kind == "custom":
            continue
        rules = fetch_list_action_rules(source_list_id=str(list_row["id"]), enabled_only=False)
        locked_count = sum(
            1
            for rule in rules
            if (not bool(rule.get("enabled", True))) or rule.get("lock_reason_key") or rule.get("lock_reason_text")
        )
        updated_candidates = [rule.get("updated_at") for rule in rules if rule.get("updated_at") is not None]
        rows.append(
            {
                "id": list_row["id"],
                "slug": list_row.get("slug"),
                "name": list_row["name"],
                "description": list_row.get("description"),
                "list_kind": list_kind,
                "list_kind_label": LIST_KIND_LABELS.get(list_kind, list_kind or "—"),
                "ai_input_role": list_row.get("ai_input_role"),
                "ai_input_role_label": _ai_role_label(list_row.get("ai_input_role")),
                "rule_count": len(rules),
                "locked_count": locked_count,
                "updated_at": max(updated_candidates) if updated_candidates else None,
            }
        )
    rows.sort(key=lambda item: (str(item["list_kind"]) == "custom", str(item["name"]).casefold()))
    return rows


def _group_rules_for_list(list_id: str) -> list[dict[str, object]]:
    """Sloz read-only skupiny pravidel pro detail jednoho seznamu."""

    target_lists_by_id = {row["id"]: row for row in fetch_user_lists()}
    groups: list[dict[str, object]] = []
    for trigger_action in LIST_RULE_TRIGGER_ORDER:
        rules = fetch_list_action_rules(
            source_list_id=list_id,
            trigger_action=trigger_action,
            enabled_only=False,
        )
        prepared_rules: list[dict[str, object]] = []
        for rule in rules:
            target_list = target_lists_by_id.get(rule.get("target_list_id") or "")
            lock_reason = str(rule.get("lock_reason_text") or "").strip() or None
            if lock_reason is None and rule.get("lock_reason_key"):
                lock_reason = str(rule["lock_reason_key"])
            prepared_rules.append(
                {
                    **rule,
                    "target_list_name": target_list["name"] if target_list else "—",
                    "effect_label": LIST_RULE_EFFECT_LABELS.get(str(rule.get("effect_type") or ""), str(rule.get("effect_type") or "—")),
                    "phase_label": LIST_RULE_PHASE_LABELS.get(str(rule.get("phase") or ""), str(rule.get("phase") or "—")),
                    "is_locked": bool(lock_reason),
                    "is_enabled": bool(rule.get("enabled", True)),
                    "lock_reason": lock_reason,
                }
            )
        groups.append(
            {
                "trigger_action": trigger_action,
                "trigger_label": LIST_RULE_TRIGGER_LABELS[trigger_action],
                "trigger_help": LIST_RULE_TRIGGER_HELP.get(trigger_action, ""),
                "target_required": trigger_action in {"copy_to_list", "move_to_list"},
                "effect_options": [
                    {
                        "value": effect_type,
                        "label": LIST_RULE_EFFECT_LABELS.get(effect_type, effect_type),
                    }
                    for effect_type in LIST_RULE_TRIGGER_EFFECT_OPTIONS.get(trigger_action, ())
                ],
                "rules": prepared_rules,
                "rule_count": len(prepared_rules),
            }
        )
    return groups


def _list_action_rules_setup_message() -> str:
    """Vrat lidsky citelnou hlasku pro chybejici DB schema list-action rules."""

    return (
        "Tahle databáze ještě nemá tabulku app.list_action_rules. "
        "Nejdřív spusť databázový upgrade přes `filmy-upgrade-database` "
        "nebo `python -m filmy.scripts.upgrade_database`."
    )


def _no_store_redirect(url: str) -> RedirectResponse:
    """Vrat redirect vhodny pro administracni/system workflow bez cache."""
    response = RedirectResponse(url=url, status_code=303)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def _build_list_action_rule_detail_url(
    list_id: str,
    *,
    return_to: str | None,
    saved: str | None = None,
    error: str | None = None,
) -> str:
    """Sestav detail URL editoru pravidel vcetne navratoveho stavu."""

    params: dict[str, str] = {}
    if return_to:
        params["return_to"] = return_to
    if saved:
        params["saved"] = saved
    if error:
        params["error"] = error
    if not params:
        return f"/system/list-action-rules/{list_id}"
    return f"/system/list-action-rules/{list_id}?{urlencode(params)}"


def _build_rule_editor_target_options(source_list_id: str) -> list[dict[str, str]]:
    """Vrat povolene cilove seznamy pro editor target akci."""

    blocked_slugs = {"watchlist", "ai-navrhy"}
    blocked_ids = {"watchlist", "ai-suggestions"}
    rows: list[dict[str, str]] = []
    for list_row in fetch_user_lists():
        list_id = str(list_row.get("id") or "")
        slug = str(list_row.get("slug") or "")
        if not list_id or list_id == source_list_id:
            continue
        if list_id in blocked_ids or slug in blocked_slugs:
            continue
        rows.append(
            {
                "id": list_id,
                "name": str(list_row.get("name") or list_id),
                "slug": slug,
            }
        )
    rows.sort(key=lambda item: item["name"].casefold())
    return rows


def _parse_rule_enabled(value: str | None) -> bool:
    """Preved formularovy stav enabled na boolean."""

    return str(value or "true").strip().lower() not in {"0", "false", "off", "disabled"}


def _validate_list_action_rule_payload(
    *,
    source_list: dict[str, object],
    trigger_action: str,
    target_list_id: str | None,
    effect_type: str,
    phase: str,
    order_index: int,
) -> dict[str, object]:
    """Zvaliduj editor payload a vrat normalizovana data pro ulozeni."""

    normalized_trigger = str(trigger_action or "").strip()
    if normalized_trigger not in LIST_RULE_TRIGGER_ORDER:
        raise ValueError("Neznámá trigger akce.")

    normalized_effect = str(effect_type or "").strip()
    allowed_effects = LIST_RULE_TRIGGER_EFFECT_OPTIONS.get(normalized_trigger, ())
    if normalized_effect not in allowed_effects:
        raise ValueError("Vybraný effect pro tuhle trigger akci nedává smysl.")

    normalized_phase = str(phase or "").strip()
    if normalized_phase not in LIST_RULE_PHASE_LABELS:
        raise ValueError("Neznámá phase pravidla.")

    if order_index <= 0:
        raise ValueError("Pořadí pravidla musí být kladné číslo.")

    target_required = normalized_trigger in {"copy_to_list", "move_to_list"}
    normalized_target = str(target_list_id or "").strip() or None
    if target_required and normalized_target is None:
        raise ValueError("Tahle trigger akce vyžaduje konkrétní cílový list.")
    if (not target_required) and normalized_target is not None:
        raise ValueError("Tahle trigger akce nesmí mít cílový list.")

    if normalized_target is not None:
        if normalized_target == str(source_list["id"]):
            raise ValueError("Cílový list nesmí být stejný jako zdrojový.")
        target_list = fetch_user_list(normalized_target)
        if target_list is None:
            raise ValueError("Vybraný cílový list neexistuje.")
        target_slug = str(target_list.get("slug") or "")
        if normalized_target in {"watchlist", "ai-suggestions"} or target_slug in {"watchlist", "ai-navrhy"}:
            raise ValueError("Do Watchlistu ani do AI návrhů se přes editor pravidel zapisovat nesmí.")

    if normalized_effect == "write_rating" and normalized_phase != "immediate":
        raise ValueError("Write rating dává smysl jen jako immediate effect.")
    if normalized_effect == "derive_watched" and normalized_phase != "immediate":
        raise ValueError("Derive watched dává smysl jen jako immediate effect.")
    if normalized_trigger == "move_to_list" and normalized_effect == "preserve_source_membership":
        raise ValueError("Move to list nesmí zachovat source membership.")

    return {
        "trigger_action": normalized_trigger,
        "target_list_id": normalized_target,
        "effect_type": normalized_effect,
        "phase": normalized_phase,
        "order_index": order_index,
    }


@router.get("/system/list-action-rules", response_class=HTMLResponse)
async def list_action_rules_overview_page(
    request: Request,
    return_to: str | None = Query(default=None),
    filter_kind: str = Query(default="all"),
):
    """Vykresli overview seznam listu pro read-only spravu list action rules."""

    normalized_filter = str(filter_kind or "all").strip().lower()
    if normalized_filter not in {"all", "system", "custom"}:
        normalized_filter = "all"
    breadcrumb_context = build_breadcrumb_context(request, "List Action Rules", return_to=return_to, default_trail=[{"url": "/", "label": "Home"}])
    setup_message: str | None = None
    try:
        rows = _build_rule_overview_rows(normalized_filter)
    except psycopg.errors.UndefinedTable:
        rows = []
        setup_message = _list_action_rules_setup_message()
    response = templates.TemplateResponse(
        request,
        "system_list_action_rules_overview.html",
        {
            **breadcrumb_context,
            "return_to": breadcrumb_context["page_return_to"],
            "filter_kind": normalized_filter,
            "overview_rows": rows,
            "list_count": len(rows),
            "configured_rule_count": sum(int(item["rule_count"]) for item in rows),
            "locked_rule_count": sum(int(item["locked_count"]) for item in rows),
            "setup_message": setup_message,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    return response


@router.get("/system/list-action-rules/{list_id}", response_class=HTMLResponse)
async def list_action_rules_detail_page(
    request: Request,
    list_id: str,
    return_to: str | None = Query(default=None),
    saved: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    """Vykresli detail a editor pravidel jednoho konkretniho seznamu."""

    list_row = fetch_user_list(list_id)
    if list_row is None:
        raise HTTPException(status_code=404, detail="Seznam nebyl nalezen.")
    breadcrumb_context = build_breadcrumb_context(
        request,
        "List Action Rules",
        return_to=return_to or "/system/list-action-rules",
        default_trail=[{"url": "/", "label": "Home"}, {"url": "/system/list-action-rules", "label": "List Action Rules"}],
    )
    setup_message: str | None = None
    try:
        rule_groups = _group_rules_for_list(list_id)
    except psycopg.errors.UndefinedTable:
        rule_groups = [
            {
                "trigger_action": trigger_action,
                "trigger_label": LIST_RULE_TRIGGER_LABELS[trigger_action],
                "rules": [],
                "rule_count": 0,
            }
            for trigger_action in LIST_RULE_TRIGGER_ORDER
        ]
        setup_message = _list_action_rules_setup_message()
    all_rules = [rule for group in rule_groups for rule in group["rules"]]
    response = templates.TemplateResponse(
        request,
        "system_list_action_rules_detail.html",
        {
            **breadcrumb_context,
            "return_to": breadcrumb_context["page_return_to"],
            "list_row": {
                **list_row,
                "list_kind_label": LIST_KIND_LABELS.get(str(list_row.get("list_kind") or ""), str(list_row.get("list_kind") or "—")),
                "ai_input_role_label": _ai_role_label(list_row.get("ai_input_role")),
            },
            "rule_groups": rule_groups,
            "rule_count": len(all_rules),
            "locked_rule_count": sum(1 for rule in all_rules if rule["is_locked"] or (not rule["is_enabled"])),
            "target_list_options": _build_rule_editor_target_options(list_id),
            "setup_message": setup_message,
            "saved_message": {
                "created": "Pravidlo bylo přidané.",
                "updated": "Pravidlo bylo uložené.",
                "deleted": "Pravidlo bylo smazané.",
            }.get(str(saved or "").strip()),
            "error_message": str(error or "").strip() or None,
            "format_czech_datetime": format_czech_datetime,
        },
    )
    return response


@router.post("/system/list-action-rules/{list_id}/rules/create")
async def list_action_rules_create(
    list_id: str,
    trigger_action: str = Form(),
    target_list_id: str | None = Form(default=None),
    effect_type: str = Form(),
    phase: str = Form(),
    order_index: int | None = Form(default=None),
    enabled: str | None = Form(default="true"),
    return_to: str | None = Form(default=None),
):
    """Pridej nove pravidlo do editoru konkretniho seznamu."""

    list_row = fetch_user_list(list_id)
    if list_row is None:
        raise HTTPException(status_code=404, detail="Seznam nebyl nalezen.")
    try:
        normalized = _validate_list_action_rule_payload(
            source_list=list_row,
            trigger_action=trigger_action,
            target_list_id=target_list_id,
            effect_type=effect_type,
            phase=phase,
            order_index=int(order_index or 0),
        )
        upsert_list_action_rule(
            rule_id=f"rule:custom:{uuid.uuid4()}",
            source_list_id=list_id,
            trigger_action=str(normalized["trigger_action"]),
            target_list_id=normalized["target_list_id"],
            effect_type=str(normalized["effect_type"]),
            phase=str(normalized["phase"]),
            order_index=int(normalized["order_index"]),
            enabled=_parse_rule_enabled(enabled),
            effect_params={},
        )
    except (ValueError, psycopg.Error) as exc:
        return _no_store_redirect(_build_list_action_rule_detail_url(list_id, return_to=return_to, error=str(exc)))
    return _no_store_redirect(_build_list_action_rule_detail_url(list_id, return_to=return_to, saved="created"))


@router.post("/system/list-action-rules/{list_id}/rules/{rule_id}/update")
async def list_action_rules_update(
    list_id: str,
    rule_id: str,
    trigger_action: str = Form(),
    target_list_id: str | None = Form(default=None),
    effect_type: str = Form(),
    phase: str = Form(),
    order_index: int | None = Form(default=None),
    enabled: str | None = Form(default="true"),
    return_to: str | None = Form(default=None),
):
    """Uprav existujici pravidlo v editoru konkretniho seznamu."""

    list_row = fetch_user_list(list_id)
    if list_row is None:
        raise HTTPException(status_code=404, detail="Seznam nebyl nalezen.")
    existing_rule = fetch_list_action_rule(rule_id)
    if existing_rule is None or str(existing_rule.get("source_list_id") or "") != list_id:
        raise HTTPException(status_code=404, detail="Pravidlo nebylo nalezeno.")
    if existing_rule.get("lock_reason_key") or existing_rule.get("lock_reason_text"):
        return _no_store_redirect(
            _build_list_action_rule_detail_url(
                list_id,
                return_to=return_to,
                error=str(existing_rule.get("lock_reason_text") or "Tahle kombinace je zamčená a nejde upravit."),
            )
        )
    try:
        normalized = _validate_list_action_rule_payload(
            source_list=list_row,
            trigger_action=trigger_action,
            target_list_id=target_list_id,
            effect_type=effect_type,
            phase=phase,
            order_index=int(order_index or 0),
        )
        upsert_list_action_rule(
            rule_id=rule_id,
            source_list_id=list_id,
            trigger_action=str(normalized["trigger_action"]),
            target_list_id=normalized["target_list_id"],
            effect_type=str(normalized["effect_type"]),
            phase=str(normalized["phase"]),
            order_index=int(normalized["order_index"]),
            enabled=_parse_rule_enabled(enabled),
            lock_reason_key=str(existing_rule.get("lock_reason_key") or "") or None,
            lock_reason_text=str(existing_rule.get("lock_reason_text") or "") or None,
            effect_params=dict(existing_rule.get("effect_params") or {}),
        )
    except (ValueError, psycopg.Error) as exc:
        return _no_store_redirect(_build_list_action_rule_detail_url(list_id, return_to=return_to, error=str(exc)))
    return _no_store_redirect(_build_list_action_rule_detail_url(list_id, return_to=return_to, saved="updated"))


@router.post("/system/list-action-rules/{list_id}/rules/{rule_id}/delete")
async def list_action_rules_delete(
    list_id: str,
    rule_id: str,
    return_to: str | None = Form(default=None),
):
    """Smaz existujici pravidlo z editoru konkretniho seznamu."""

    existing_rule = fetch_list_action_rule(rule_id)
    if existing_rule is None or str(existing_rule.get("source_list_id") or "") != list_id:
        raise HTTPException(status_code=404, detail="Pravidlo nebylo nalezeno.")
    if existing_rule.get("lock_reason_key") or existing_rule.get("lock_reason_text"):
        return _no_store_redirect(
            _build_list_action_rule_detail_url(
                list_id,
                return_to=return_to,
                error=str(existing_rule.get("lock_reason_text") or "Tahle kombinace je zamčená a nejde smazat."),
            )
        )
    try:
        delete_list_action_rule(rule_id)
    except psycopg.Error as exc:
        return _no_store_redirect(_build_list_action_rule_detail_url(list_id, return_to=return_to, error=str(exc)))
    return _no_store_redirect(_build_list_action_rule_detail_url(list_id, return_to=return_to, saved="deleted"))


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
