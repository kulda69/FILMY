"""TMDB mapovani, payloady, assety a target-selection helpery."""

from __future__ import annotations

"""TMDB mapping, asset, and enrichment-target operations extracted from `filmy.db`."""

import importlib
import json
import uuid
from dataclasses import dataclass, field
from math import sqrt
from pathlib import Path
from typing import Any, Sequence

from filmy.config import get_ui_config
from filmy.runtime_postgres import (
    fetch_active_user_list_items,
    fetch_all_watch_events,
    fetch_catalog_brief_rows,
    fetch_episode_series_map,
    fetch_latest_tmdb_assets_for_title,
    fetch_positive_person_affinities,
    fetch_relevant_people_candidate_rows,
    fetch_tmdb_completion_flags,
    fetch_tmdb_mapping_record,
    fetch_tmdb_payload_snapshot,
    fetch_user_lists,
    insert_tmdb_asset_record,
    list_in_progress_content_states,
    store_tmdb_payload_bundle,
    upsert_tmdb_mapping_record,
)


def _db():
    """Vrat facade modul `filmy.db` kvuli kompatibilnim helperum a cache API."""
    return importlib.import_module("filmy.db")


@dataclass
class TmdbTargetItem:
    """Jedna TMDB target/cache kandidatni polozka v internim zpracovani."""

    tconst: str
    priority: int
    reasons: list[str] = field(default_factory=list)
    title_type: str | None = None
    primary_title: str | None = None
    start_year: int | None = None

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "TmdbTargetItem":
        """Preved slovnikovy target payload na typovany TMDB target."""
        start_year = item.get("start_year")
        return cls(
            tconst=str(item["tconst"]),
            priority=int(item.get("priority") or 0),
            reasons=list(item.get("reasons") or []),
            title_type=item.get("title_type"),
            primary_title=item.get("primary_title"),
            start_year=int(start_year) if start_year is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Preved TMDB target zpet na bezny slovnik."""
        return {
            "tconst": self.tconst,
            "title_type": self.title_type,
            "primary_title": self.primary_title,
            "start_year": self.start_year,
            "priority": self.priority,
            "reasons": list(self.reasons),
        }

    def merge_from(self, other: "TmdbTargetItem") -> None:
        """Sluc dalsi target se stejnym `tconst` a ponech lepsi prioritu i duvody."""
        self.priority = min(self.priority, other.priority)
        self.reasons = sorted(set(self.reasons).union(other.reasons))
        if self.start_year is None and other.start_year is not None:
            self.start_year = other.start_year
        if not self.primary_title and other.primary_title:
            self.primary_title = other.primary_title
        if not self.title_type and other.title_type:
            self.title_type = other.title_type

    def sort_key(self) -> tuple[int, int, int, str]:
        """Vrat stabilni tridici klic pro finalni poradi TMDB targetu."""
        return (
            self.priority,
            1 if self.start_year is None else 0,
            -(self.start_year if self.start_year is not None else 0),
            str(self.primary_title or ""),
        )


@dataclass
class TitleDetailCacheTarget:
    """Interni reprezentace kandidata pro materializaci title detail cache."""

    tconst: str
    title_type: str | None
    primary_title: str | None
    start_year: int | None
    priority: int
    reasons: list[str] = field(default_factory=list)
    cache_status: str = "missing"
    cache_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Preved title detail cache target na bezny slovnik."""
        return {
            "tconst": self.tconst,
            "title_type": self.title_type,
            "primary_title": self.primary_title,
            "start_year": self.start_year,
            "priority": self.priority,
            "reasons": list(self.reasons),
            "cache_status": self.cache_status,
            "cache_path": self.cache_path,
        }


@dataclass
class PersonDetailCacheTarget:
    """Interni reprezentace kandidata pro materializaci person detail cache."""

    nconst: str
    name: str
    primary_name: str
    credit_count: int
    affinity_rating: int
    cache_status: str
    cache_path: str

    def to_dict(self) -> dict[str, Any]:
        """Preved person detail cache target na bezny slovnik."""
        return {
            "nconst": self.nconst,
            "name": self.name,
            "primary_name": self.primary_name,
            "credit_count": self.credit_count,
            "affinity_rating": self.affinity_rating,
            "cache_status": self.cache_status,
            "cache_path": self.cache_path,
        }


def upsert_tmdb_mapping(
    tconst: str,
    tmdb_media_type: str,
    tmdb_id: int,
    matched_by: str,
    sync_status: str,
    last_error: str | None = None,
) -> None:
    """Upsertni TMDB mapovani titulu a invaliduj presentation cache."""
    db = _db()
    upsert_tmdb_mapping_record(
        tconst=tconst,
        tmdb_media_type=tmdb_media_type,
        tmdb_id=tmdb_id,
        matched_by=matched_by,
        sync_status=sync_status,
        matched_at=db._now_iso(),
        last_error=last_error,
    )
    db.invalidate_title_presentation_cache(tconst)


def store_tmdb_payloads(
    tconst: str,
    locale: str,
    detail_payload: dict[str, Any],
    provider_payload: dict[str, Any] | None,
) -> None:
    """Uloz detail a provider payloady TMDB do PostgreSQL snapshot vrstvy."""
    db = _db()
    poster_path = detail_payload.get("poster_path")
    backdrop_path = detail_payload.get("backdrop_path")
    display_title = detail_payload.get("title") or detail_payload.get("name")
    release_date = detail_payload.get("release_date") or detail_payload.get("first_air_date")
    genres_json = json.dumps(detail_payload.get("genres", []), ensure_ascii=False)
    raw_json = json.dumps(detail_payload, ensure_ascii=False)
    providers = provider_payload.get("results", {}).get("CZ", {}) if provider_payload else {}
    synced_at = db._now_iso()
    db.store_tmdb_payload_bundle(
        tconst=tconst,
        locale=locale,
        display_title=display_title,
        original_title=detail_payload.get("original_title") or detail_payload.get("original_name"),
        overview=detail_payload.get("overview"),
        poster_path=poster_path,
        backdrop_path=backdrop_path,
        release_date=release_date,
        genres_json=genres_json,
        raw_json=raw_json,
        synced_at=synced_at,
        providers=[
            {
                "provider_type": provider_type,
                "provider_id": provider.get("provider_id"),
                "provider_name": provider.get("provider_name"),
                "logo_path": provider.get("logo_path"),
                "display_priority": provider.get("display_priority"),
            }
            for provider_type in ("flatrate", "rent", "buy", "ads")
            for provider in providers.get(provider_type, [])
        ],
    )
    db.invalidate_title_presentation_cache(tconst)


def get_tmdb_mapping(tconst: str) -> dict[str, Any] | None:
    """Vrat ulozene TMDB mapovani pro jeden titul."""
    return _db().fetch_tmdb_mapping_record(tconst)


def record_tmdb_asset(
    tconst: str,
    asset_kind: str,
    relative_path: str,
    local_path: str,
    fetch_reason: str,
    status: str,
    sha256: str | None,
) -> dict[str, Any]:
    """Zapis metadata o stazenem TMDB assetu a vrat ulozeny zaznam."""
    db = _db()
    asset_id = str(uuid.uuid4())
    fetched_at = db._now_iso()
    insert_tmdb_asset_record(
        asset_id=asset_id,
        tconst=tconst,
        asset_kind=asset_kind,
        relative_path=relative_path,
        local_path=local_path,
        fetch_reason=fetch_reason,
        status=status,
        sha256=sha256,
        fetched_at=fetched_at,
    )
    db.invalidate_title_presentation_cache(tconst)
    return {
        "id": asset_id,
        "tconst": tconst,
        "asset_kind": asset_kind,
        "relative_path": relative_path,
        "local_path": local_path,
        "fetch_reason": fetch_reason,
        "status": status,
        "sha256": sha256,
        "fetched_at": fetched_at,
    }


def get_latest_tmdb_assets(tconst: str) -> list[dict[str, Any]]:
    """Vrat posledni asset zaznamy pro jeden titul."""
    return fetch_latest_tmdb_assets_for_title(tconst)


def get_tmdb_detail_locales(tconst: str) -> list[str]:
    """Vrat seznam lokalizaci, pro ktere uz ma titul ulozeny detail payload."""
    primary_locale, fallback_locale = get_ui_config().tmdb_locale_order
    snapshot = fetch_tmdb_payload_snapshot(
        tconst,
        primary_locale=primary_locale,
        fallback_locale=fallback_locale,
    )
    return [] if snapshot is None else list(snapshot["detail_locales"])


def get_tmdb_asset_summary(tconst: str) -> dict[str, dict[str, Any]]:
    """Sloz shrnuti posledniho stavu assetu po druzich pro jeden titul."""
    latest_by_kind: dict[str, dict[str, Any]] = {}
    for asset in get_latest_tmdb_assets(tconst):
        asset_kind = asset["asset_kind"]
        if asset_kind in latest_by_kind:
            continue
        resolved_local_path = _resolve_tmdb_asset_local_path(asset)
        latest_by_kind[asset_kind] = {
            "status": asset.get("status"),
            "local_path": resolved_local_path,
            "exists": bool(resolved_local_path and Path(resolved_local_path).exists()),
            "fetched_at": asset.get("fetched_at"),
        }
    return latest_by_kind


def get_latest_poster_records(tconsts: list[str]) -> dict[str, dict[str, Any]]:
    """Vrat pro vice titulu posledni uspesne poster asset zaznamy."""
    clean_tconsts = [str(tconst).strip() for tconst in tconsts if str(tconst).strip()]
    if not clean_tconsts:
        return {}
    records: dict[str, dict[str, Any]] = {}
    for tconst in clean_tconsts:
        for asset in get_latest_tmdb_assets(tconst):
            if str(asset.get("asset_kind")) != "poster":
                continue
            if str(asset.get("status")) != "fetched":
                continue
            records[tconst] = {
                "poster_relative_path": asset.get("relative_path"),
                "poster_local_path": asset.get("local_path"),
            }
            break
    return records


def get_tmdb_enrichment_targets(
    limit: int | None = None,
    include_complete: bool = True,
    priority_tconsts: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Vrat tituly, ktere maji vstoupit do TMDB enrichment fronty."""
    db = _db()
    items = db._get_runtime_postgres_candidate_items(None)
    if priority_tconsts:
        priority_items = db._get_priority_tmdb_target_items(None, priority_tconsts)
        existing = {item["tconst"] for item in items}
        items = [item for item in priority_items if item["tconst"] not in existing] + items
    primary_locale, fallback_locale = get_ui_config().tmdb_locale_order
    flags_by_tconst = db.fetch_tmdb_completion_flags(
        [str(item["tconst"]) for item in items],
        primary_locale=primary_locale,
        fallback_locale=fallback_locale,
    )
    filtered_items: list[dict[str, Any]] = []
    for item in items:
        tconst = str(item["tconst"])
        flags = flags_by_tconst.get(tconst)
        if flags is not None and str(flags.get("sync_status") or "") == "not_found":
            continue
        is_complete = _tmdb_flags_indicate_complete(
            flags,
            primary_locale=primary_locale,
            fallback_locale=fallback_locale,
        )
        if include_complete or not is_complete:
            filtered_items.append(item)
    if limit is not None:
        filtered_items = filtered_items[:limit]
    return filtered_items


def get_tmdb_target_counts() -> tuple[int, int]:
    """Vrat pocet vsech TMDB targetu a pocet jiz kompletnich."""
    total = len(get_tmdb_enrichment_targets(include_complete=True))
    remaining = len(get_tmdb_enrichment_targets(include_complete=False))
    return (total, total - remaining)


def _tmdb_status_is_complete(tconst: str) -> bool:
    """Over starym detail pohledem, zda ma titul kompletni TMDB materializaci."""
    mapping = get_tmdb_mapping(tconst)
    if mapping is None:
        return False
    locales = set(get_tmdb_detail_locales(tconst))
    detail = ((_db().get_content_detail(tconst) or {}).get("tmdb") or {}).get("details") or {}
    assets = get_tmdb_asset_summary(tconst)
    has_locales = "en-US" in locales and "cs-CZ" in locales
    poster_ok = not detail.get("poster_path") or bool((assets.get("poster") or {}).get("exists"))
    backdrop_ok = not detail.get("backdrop_path") or bool((assets.get("backdrop") or {}).get("exists"))
    return has_locales and poster_ok and backdrop_ok


def _tmdb_flags_indicate_complete(
    flags: dict[str, Any] | None, *, primary_locale: str, fallback_locale: str
) -> bool:
    """Vyhodnot z completion flags, zda je titul pro dane locale poradi kompletni."""
    if flags is None:
        return False
    has_locales = bool(flags.get("has_primary")) and bool(flags.get("has_fallback"))
    poster_ok = not flags.get("poster_path") or bool(flags.get("has_poster"))
    backdrop_ok = not flags.get("backdrop_path") or bool(flags.get("has_backdrop"))
    return has_locales and poster_ok and backdrop_ok


def _get_tmdb_postgres_runtime_items(conn, *, include_complete: bool) -> list[dict[str, Any]]:
    """Sloz kandidaty pro TMDB runtime pres PG pomocnou SQL projekci."""
    runtime_items = _get_runtime_postgres_candidate_items(conn)
    if not runtime_items:
        return []
    candidate_rows: list[tuple[str, str, int]] = []
    for item in runtime_items:
        for reason in item.get("reasons") or []:
            candidate_rows.append((str(item["tconst"]), str(reason), int(item["priority"])))
    input_sql = " UNION ALL ".join(("SELECT ? AS target_tconst, ? AS reason, ? AS priority" for _ in candidate_rows))
    sql = f"""
        WITH pg_candidates AS (
            {input_sql}
        ),
        candidates AS (
            SELECT
                c.target_tconst,
                c.reason,
                c.priority
            FROM pg_candidates AS c
        ),
        ranked AS (
            SELECT
                target_tconst,
                MIN(priority) AS priority,
                string_agg(DISTINCT reason, ', ' ORDER BY reason) AS reasons
            FROM candidates
            WHERE target_tconst IS NOT NULL
            GROUP BY 1
        ),
        detail_flags AS (
            SELECT
                tconst,
                MAX(CASE WHEN locale = 'en-US' THEN 1 ELSE 0 END) AS has_en,
                MAX(CASE WHEN locale = 'cs-CZ' THEN 1 ELSE 0 END) AS has_cs,
                MAX(CASE WHEN locale = 'en-US' THEN poster_path WHEN locale = 'cs-CZ' THEN poster_path ELSE NULL END) AS poster_path,
                MAX(CASE WHEN locale = 'en-US' THEN backdrop_path WHEN locale = 'cs-CZ' THEN backdrop_path ELSE NULL END) AS backdrop_path
            FROM app.tmdb_title_details
            GROUP BY 1
        ),
        asset_flags AS (
            SELECT
                tconst,
                MAX(CASE WHEN asset_kind = 'poster' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_poster,
                MAX(CASE WHEN asset_kind = 'backdrop' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_backdrop
            FROM app.tmdb_assets
            GROUP BY 1
        )
        SELECT
            r.target_tconst,
            t.title_type,
            t.primary_title,
            t.start_year,
            r.priority,
            r.reasons
        FROM ranked AS r
        JOIN app.catalog_titles AS t ON t.tconst = r.target_tconst
        LEFT JOIN detail_flags AS d ON d.tconst = r.target_tconst
        LEFT JOIN asset_flags AS a ON a.tconst = r.target_tconst
        LEFT JOIN app.tmdb_title_map AS m ON m.tconst = r.target_tconst
        WHERE (
            ? = TRUE
            OR NOT (
                COALESCE(d.has_en, 0) = 1
                AND COALESCE(d.has_cs, 0) = 1
                AND (COALESCE(d.poster_path, '') = '' OR COALESCE(a.has_poster, 0) = 1)
                AND (COALESCE(d.backdrop_path, '') = '' OR COALESCE(a.has_backdrop, 0) = 1)
            )
        )
          AND COALESCE(m.sync_status, '') <> 'not_found'
        ORDER BY r.priority, t.start_year DESC NULLS LAST, t.primary_title
    """
    params: list[Any] = []
    for row in candidate_rows:
        params.extend(row)
    params.append(include_complete)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "tconst": row[0],
            "title_type": row[1],
            "primary_title": row[2],
            "start_year": row[3],
            "priority": row[4],
            "reasons": row[5].split(", ") if row[5] else [],
        }
        for row in rows
    ]


def _get_priority_tmdb_target_items(conn, priority_tconsts: Sequence[str]) -> list[dict[str, Any]]:
    """Vrat explicitne uprednostnene targety, typicky z aktivniho hledani."""
    priority_set = [str(tconst).strip() for tconst in priority_tconsts if str(tconst).strip()]
    if not priority_set:
        return []
    rows = _db().fetch_catalog_brief_rows(priority_set)
    return [
        TmdbTargetItem(
            tconst=str(row[0]),
            title_type=row[1],
            primary_title=row[2],
            start_year=row[3],
            priority=0,
            reasons=["search_target"],
        ).to_dict()
        for row in rows
    ]


def _merge_tmdb_target_items(primary: Sequence[dict[str, Any]], secondary: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sluc dva seznamy TMDB targetu a zachovej nejlepsi prioritu i duvody."""
    merged: dict[str, TmdbTargetItem] = {}
    for source in (primary, secondary):
        for item in source:
            candidate = TmdbTargetItem.from_dict(item)
            existing = merged.get(candidate.tconst)
            if existing is None:
                merged[candidate.tconst] = candidate
                continue
            existing.merge_from(candidate)
    return [item.to_dict() for item in sorted(merged.values(), key=lambda item: item.sort_key())]


def _merge_runtime_candidate_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Zredukuj duplikovane runtime kandidatni radky podle `tconst`."""
    merged: dict[str, TmdbTargetItem] = {}
    for item in rows:
        candidate = TmdbTargetItem.from_dict(item)
        existing = merged.get(candidate.tconst)
        if existing is None:
            merged[candidate.tconst] = candidate
            continue
        existing.merge_from(candidate)
    return [item.to_dict() for item in merged.values()]


def _catalog_title_rows_by_tconsts(conn, tconsts: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Vrat kratka katalogova metadata pro zadane tituly."""
    if not tconsts:
        return {}
    rows = _db().fetch_catalog_brief_rows([str(tconst) for tconst in tconsts])
    return {
        str(row[0]): {
            "tconst": str(row[0]),
            "title_type": row[1],
            "primary_title": row[2],
            "start_year": row[3],
        }
        for row in rows
    }


def _episode_series_map(conn, tconsts: Sequence[str]) -> dict[str, str]:
    """Vrat mapovani epizod na jejich serialove `tconst`."""
    if not tconsts:
        return {}
    return _db().fetch_episode_series_map([str(tconst) for tconst in tconsts])


def _compute_actor_affinity_scores(conn, tconsts: Sequence[str]) -> dict[str, float]:
    """Spocti lehky affinity signal titulu z pozitivne hodnocenych hercu."""
    if not tconsts:
        return {}
    affinities = _db().fetch_positive_person_affinities()
    if not affinities:
        return {}
    title_placeholders = ", ".join(("?" for _ in tconsts))
    person_ids = sorted(affinities)
    person_placeholders = ", ".join(("?" for _ in person_ids))
    rows = conn.execute(
        f"""
        SELECT tconst, nconst, ordering
        FROM app.title_credits
        WHERE credit_group = 'cast'
          AND tconst IN ({title_placeholders})
          AND nconst IN ({person_placeholders})
        """,
        [*tconsts, *person_ids],
    ).fetchall()
    totals: dict[str, tuple[float, float]] = {}
    for tconst, nconst, ordering in rows:
        weight = 1.0 if ordering is None or ordering <= 0 else 1.0 / sqrt(float(ordering))
        current_sum, current_weight = totals.get(str(tconst), (0.0, 0.0))
        totals[str(tconst)] = (current_sum + float(affinities[str(nconst)]) * weight, current_weight + weight)
    return {
        tconst: score_sum / weight_sum
        for tconst, (score_sum, weight_sum) in totals.items()
        if weight_sum > 0
    }


def _get_runtime_postgres_candidate_items(conn) -> list[dict[str, Any]]:
    """Sloz hlavni runtime kandidaty z watched, in-progress a user list vstupu."""
    db = _db()
    candidate_rows: list[dict[str, Any]] = []
    event_tconsts = sorted({str(event["tconst"]) for event in db.fetch_all_watch_events() if event.get("tconst")})
    title_rows = _catalog_title_rows_by_tconsts(conn, event_tconsts)
    for tconst in title_rows:
        candidate_rows.append({"tconst": tconst, "priority": 1, "reasons": ["watched_title"]})
    series_map = _episode_series_map(conn, event_tconsts)
    for series_tconst in sorted(set(series_map.values())):
        candidate_rows.append({"tconst": series_tconst, "priority": 1, "reasons": ["watched_series"]})
    state_tconsts = sorted({str(state["tconst"]) for state in db.list_in_progress_content_states(limit=None) if state.get("tconst")})
    title_rows = _catalog_title_rows_by_tconsts(conn, state_tconsts)
    for tconst in title_rows:
        candidate_rows.append({"tconst": tconst, "priority": 2, "reasons": ["in_progress_title"]})
    series_map = _episode_series_map(conn, state_tconsts)
    for series_tconst in sorted(set(series_map.values())):
        candidate_rows.append({"tconst": series_tconst, "priority": 2, "reasons": ["in_progress_series"]})
    list_kind_by_id = {str(item["id"]): str(item["list_kind"]) for item in db.fetch_user_lists()}
    active_items = db.fetch_active_user_list_items()
    item_tconsts = sorted({str(item["tconst"]) for item in active_items if item.get("tconst")})
    title_rows = _catalog_title_rows_by_tconsts(conn, item_tconsts)
    series_map = _episode_series_map(conn, item_tconsts)
    for item in active_items:
        tconst = item.get("tconst")
        if not tconst:
            continue
        tconst = str(tconst)
        list_kind = list_kind_by_id.get(str(item["list_id"]))
        source_origin = str(item.get("source_origin") or "")
        if list_kind == "watchlist":
            priority = 3
            direct_reason = "watchlist"
            series_reason = "watchlist_series_from_episode"
        elif source_origin == "seed_plex_library":
            priority = 3
            direct_reason = "plex_library"
            series_reason = "plex_library_series_from_episode"
        elif list_kind == "custom":
            priority = 4
            direct_reason = "custom_list"
            series_reason = "custom_list_series_from_episode"
        else:
            continue
        if tconst in title_rows:
            candidate_rows.append({"tconst": tconst, "priority": priority, "reasons": [direct_reason]})
        series_tconst = series_map.get(tconst)
        if series_tconst:
            candidate_rows.append({"tconst": series_tconst, "priority": priority, "reasons": [series_reason]})
    merged = _merge_runtime_candidate_rows(candidate_rows)
    title_meta = _catalog_title_rows_by_tconsts(conn, [item["tconst"] for item in merged])
    return [{**item, **title_meta[item["tconst"]]} for item in merged if item["tconst"] in title_meta]


def _tmdb_target_sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
    """Kompatibilni tridici klic pro slovnikovy target payload."""
    return TmdbTargetItem.from_dict(item).sort_key()


def get_title_detail_cache_targets(limit: int | None = None, include_ready: bool = False) -> list[dict[str, Any]]:
    """Vrat tituly vhodne pro materializaci title detail cache."""
    candidate_items = get_tmdb_enrichment_targets(include_complete=True)
    primary_locale, fallback_locale = get_ui_config().tmdb_locale_order
    flags_by_tconst = _db().fetch_tmdb_completion_flags(
        [str(item["tconst"]) for item in candidate_items],
        primary_locale=primary_locale,
        fallback_locale=fallback_locale,
    )
    items: list[dict[str, Any]] = []
    for item in candidate_items:
        tconst = str(item["tconst"])
        if not _tmdb_flags_indicate_complete(flags_by_tconst.get(tconst), primary_locale=primary_locale, fallback_locale=fallback_locale):
            continue
        cache_path = _db()._title_detail_cache_path(tconst)
        if not cache_path.exists():
            cache_status = "missing"
        else:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                cache_status = "ready" if isinstance(cached, dict) and cached.get("tconst") == tconst else "invalid"
            except (OSError, json.JSONDecodeError):
                cache_status = "invalid"
        if cache_status == "ready" and (not include_ready):
            continue
        items.append(
            TitleDetailCacheTarget(
                tconst=str(item["tconst"]),
                title_type=item.get("title_type"),
                primary_title=item.get("primary_title"),
                start_year=item.get("start_year"),
                priority=int(item.get("priority") or 0),
                reasons=list(item.get("reasons") or []),
                cache_status=cache_status,
                cache_path=cache_path.as_posix(),
            ).to_dict()
        )
        if limit is not None and len(items) >= limit:
            break
    return items


def _get_relevant_people_candidates(limit: int | None = None) -> list[dict[str, Any]]:
    """Vrat osoby, ktere jsou pro projekt aktualne nejrelevantnejsi."""
    return _db().fetch_relevant_people_candidate_rows(main_cast_limit=8, limit=limit)


def get_person_detail_cache_targets(limit: int | None = None, include_ready: bool = False) -> list[dict[str, Any]]:
    """Vrat osoby vhodne pro materializaci person detail cache."""
    candidates = _get_relevant_people_candidates(limit=limit)
    if not candidates:
        return []
    items: list[dict[str, Any]] = []
    for row in candidates:
        nconst = str(row["nconst"])
        fingerprint = _db()._person_cache_source_fingerprint(None, nconst)
        if fingerprint is None:
            cache_status = "missing"
        else:
            cache_status = _db()._person_detail_cache_status(nconst, fingerprint)
        if cache_status == "ready" and (not include_ready):
            continue
        items.append(
            PersonDetailCacheTarget(
                nconst=nconst,
                name=str(row["primary_name"]),
                primary_name=str(row["primary_name"]),
                credit_count=int(row.get("credit_count") or 0),
                affinity_rating=int(row.get("affinity_rating") or 0),
                cache_status=cache_status,
                cache_path=_db()._person_detail_cache_path(nconst).as_posix(),
            ).to_dict()
        )
    return items


def _latest_tmdb_asset_by_kind(assets: list[dict[str, Any]], asset_kind: str) -> dict[str, Any] | None:
    """Vrat prvni asset daneho druhu z jiz serazeneho asset seznamu."""
    for asset in assets:
        if str(asset.get("asset_kind")) == asset_kind:
            return asset
    return None


def _resolve_tmdb_asset_local_path(asset: dict[str, Any] | None) -> str | None:
    """Rozhodni lokalni cestu k assetu z absolutni nebo relativni podoby."""
    if not asset:
        return None
    db = _db()
    local_path_value = str(asset.get("local_path") or "").strip()
    relative_path_value = str(asset.get("relative_path") or "").strip()
    if local_path_value:
        local_path = Path(local_path_value)
        if local_path.exists():
            return local_path.as_posix()
    if relative_path_value:
        relative_path = Path(relative_path_value)
        candidate = db.ASSETS_DIR / relative_path
        if candidate.exists():
            return candidate.as_posix()
    return local_path_value or ((db.ASSETS_DIR / Path(relative_path_value)).as_posix() if relative_path_value else None)


def _poster_url_from_local_path(local_path_value: str | None) -> str | None:
    """Preved lokalni asset cestu na obslouzitelnou URL pod `/assets/tmdb`."""
    db = _db()
    return db._asset_url_from_local_path(local_path_value, assets_root=db.ASSETS_DIR, mount_path="/assets/tmdb")


def _poster_url_from_detail(detail: dict[str, Any] | None) -> str | None:
    """Vytahni poster URL z detail struktury titulu."""
    tmdb = (detail or {}).get("tmdb") or {}
    poster_asset = _latest_tmdb_asset_by_kind(tmdb.get("assets") or [], "poster")
    local_path = _resolve_tmdb_asset_local_path(poster_asset)
    if not local_path:
        return None
    return _poster_url_from_local_path(local_path)


def _backdrop_url_from_detail(detail: dict[str, Any] | None) -> str | None:
    """Vytahni backdrop URL z detail struktury titulu."""
    tmdb = (detail or {}).get("tmdb") or {}
    backdrop_asset = _latest_tmdb_asset_by_kind(tmdb.get("assets") or [], "backdrop")
    local_path = _resolve_tmdb_asset_local_path(backdrop_asset)
    if not local_path:
        return None
    return _poster_url_from_local_path(local_path)
