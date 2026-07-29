"""TMDB HTTP klient a souvisejici integracni utility."""

from __future__ import annotations

"""TMDB integrace pro enrichment titulu, osob a lokalnich assetu.

Modul drzi jeden sdileny servisni objekt, protoze TMDB vrstva potrebuje
koordinovat rate-limit, konfiguracni cache a vice navazujicich kroku nad
sitovymi pozadavky i lokalnimi soubory. Verejne funkce zustavaji zachovane
jako tenke wrappery kvuli kompatibilite se zbytkem projektu.
"""

import hashlib
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from filmy.db import (
    ASSETS_DIR,
    get_content_detail,
    get_tmdb_asset_summary,
    get_tmdb_detail_locales,
    get_tmdb_enrichment_targets,
    get_tmdb_mapping,
    record_tmdb_asset,
    store_tmdb_payloads,
    upsert_tmdb_mapping,
)
from filmy.paths import ENV_PATH, PEOPLE_ASSETS_DIR

load_dotenv(ENV_PATH)

TMDB_API_BASE = "https://api.themoviedb.org/3"
PRIMARY_LOCALE = "en-US"
FALLBACK_LOCALE = "cs-CZ"
TMDB_SAFE_RATE_LIMIT_PER_SECOND = 35
TMDB_MAX_RETRIES = 5


class TmdbConfigError(RuntimeError):
    """TMDB integrace nema dostupnou potrebnou konfiguraci."""


class TmdbApiError(RuntimeError):
    """Obecna chyba pri komunikaci s TMDB nebo pri zpracovani odpovedi."""


class TmdbNotFoundError(TmdbApiError):
    """TMDB pro dany identifikator nenaslo odpovidajici zaznam."""


class TmdbClient:
    """Servisni objekt pro TMDB enrichment a assety.

    Trida drzi sdileny stav, ktery by byl v ciste funkcionalnim modulu rozlezly
    do vice globalnich promennych. Typicky jde o:

    - rate-limit frontu mezi pozadavky
    - cache konfigurace obrazkovych velikosti
    - jednotne chovani pro retry, status update a lokalni metadata soubory
    """

    def __init__(self) -> None:
        """Inicializuj prazdny runtime stav klienta."""

        self._request_timestamps: deque[float] = deque()
        self._configuration_cache: dict[str, Any] | None = None

    def sync_title_from_imdb(self, tconst: str, locale: str = PRIMARY_LOCALE) -> dict[str, Any]:
        """Najdi titul pres IMDb ID a uloz detail i providery do lokalni DB."""

        self._require_token()
        match = self._api_get(f"/find/{tconst}", {"external_source": "imdb_id", "language": locale})
        movie_results = match.get("movie_results") or []
        tv_results = match.get("tv_results") or []
        if movie_results:
            first = movie_results[0]
            media_type = "movie"
        elif tv_results:
            first = tv_results[0]
            media_type = "tv"
        else:
            raise TmdbNotFoundError(f"TMDB nenaslo titul pro IMDb ID {tconst}.")
        tmdb_id = int(first["id"])
        detail = self._api_get(f"/{media_type}/{tmdb_id}", {"language": locale})
        providers = self._api_get(f"/{media_type}/{tmdb_id}/watch/providers")
        store_tmdb_payloads(tconst, locale, detail, providers)
        upsert_tmdb_mapping(tconst, media_type, tmdb_id, "imdb_id", "synced")
        return {
            "tconst": tconst,
            "locale": locale,
            "tmdb_media_type": media_type,
            "tmdb_id": tmdb_id,
            "detail_title": detail.get("title") or detail.get("name"),
            "poster_path": detail.get("poster_path"),
            "backdrop_path": detail.get("backdrop_path"),
        }

    def fetch_assets_for_title(self, tconst: str, fetch_reason: str) -> dict[str, Any]:
        """Stahni chybejici poster a backdrop pro titul s existujicim mapovanim."""

        mapping = get_tmdb_mapping(tconst)
        if mapping is None:
            raise TmdbApiError("Titul jeste nema TMDB mapovani. Nejprve spust sync.")
        if mapping.get("sync_status") == "not_found" or int(mapping.get("tmdb_id") or 0) <= 0:
            raise TmdbApiError("Titul je v TMDB oznaceny jako nenalezeny, assety nelze stahnout.")
        detail = get_content_detail(tconst)
        if detail is None:
            raise TmdbApiError("Titul v katalogu neexistuje.")
        details = ((detail.get("tmdb") or {}).get("details") or {})
        configuration = self._get_tmdb_configuration()
        base_url = configuration["images"]["secure_base_url"]
        poster_size = _preferred_size(configuration["images"]["poster_sizes"], "w500")
        backdrop_size = _preferred_size(configuration["images"]["backdrop_sizes"], "w780")
        asset_summary = get_tmdb_asset_summary(tconst)
        fetched: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for asset_kind, path_value, size in (
            ("poster", details.get("poster_path"), poster_size),
            ("backdrop", details.get("backdrop_path"), backdrop_size),
        ):
            if not path_value:
                continue
            existing_asset = asset_summary.get(asset_kind)
            if existing_asset and existing_asset.get("exists"):
                continue
            relative_path = _relative_asset_path(tconst, asset_kind, Path(path_value).name)
            local_path = ASSETS_DIR / relative_path
            local_path.parent.mkdir(parents=True, exist_ok=True)
            asset_url = f"{base_url}{size}{path_value}"
            try:
                content = self._binary_get(asset_url)
                local_path.write_bytes(content)
                fetched.append(
                    record_tmdb_asset(
                        tconst=tconst,
                        asset_kind=asset_kind,
                        relative_path=relative_path.as_posix(),
                        local_path=local_path.as_posix(),
                        fetch_reason=fetch_reason,
                        status="fetched",
                        sha256=hashlib.sha256(content).hexdigest(),
                    )
                )
            except TmdbApiError as exc:
                if local_path.exists():
                    local_path.unlink()
                errors.append(
                    {
                        "asset_kind": asset_kind,
                        "error": str(exc),
                        "asset": record_tmdb_asset(
                            tconst=tconst,
                            asset_kind=asset_kind,
                            relative_path=relative_path.as_posix(),
                            local_path=local_path.as_posix(),
                            fetch_reason=fetch_reason,
                            status="error",
                            sha256=None,
                        ),
                    }
                )
        return {"tconst": tconst, "assets": fetched, "errors": errors}

    def fetch_person_portrait(self, nconst: str, fetch_reason: str) -> dict[str, Any]:
        """Stahni portrat osoby podle IMDb `nconst` a uloz metadata vedle assetu."""

        self._require_token()
        meta_path = _person_portrait_meta_path(nconst)
        portrait_path = _person_portrait_file_path(nconst)
        existing_meta = _load_json_file(meta_path)
        if existing_meta:
            status = str(existing_meta.get("status") or "")
            if status == "fetched" and portrait_path and portrait_path.exists():
                return existing_meta
            if status in {"no_profile", "not_found"}:
                return existing_meta
        match = self._api_get(f"/find/{nconst}", {"external_source": "imdb_id"})
        person_results = match.get("person_results") or []
        if not person_results:
            payload = {
                "nconst": nconst,
                "status": "not_found",
                "fetch_reason": fetch_reason,
                "updated_at": time.time(),
            }
            _write_json_file(meta_path, payload)
            return payload
        first = person_results[0]
        tmdb_person_id = int(first["id"])
        detail = self._api_get(f"/person/{tmdb_person_id}")
        profile_path = detail.get("profile_path")
        if not profile_path:
            payload = {
                "nconst": nconst,
                "status": "no_profile",
                "tmdb_person_id": tmdb_person_id,
                "name": detail.get("name") or first.get("name"),
                "fetch_reason": fetch_reason,
                "updated_at": time.time(),
            }
            _write_json_file(meta_path, payload)
            return payload
        configuration = self._get_tmdb_configuration()
        base_url = configuration["images"]["secure_base_url"]
        profile_size = _preferred_size(configuration["images"]["profile_sizes"], "w276")
        filename = Path(str(profile_path)).name
        local_path = PEOPLE_ASSETS_DIR / nconst / f"portrait{Path(filename).suffix.lower()}"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        _remove_other_portrait_files(nconst, keep=local_path)
        content = self._binary_get(f"{base_url}{profile_size}{profile_path}")
        local_path.write_bytes(content)
        payload = {
            "nconst": nconst,
            "status": "fetched",
            "tmdb_person_id": tmdb_person_id,
            "name": detail.get("name") or first.get("name"),
            "profile_path": profile_path,
            "relative_path": local_path.relative_to(PEOPLE_ASSETS_DIR).as_posix(),
            "local_path": local_path.as_posix(),
            "fetch_reason": fetch_reason,
            "sha256": hashlib.sha256(content).hexdigest(),
            "updated_at": time.time(),
        }
        _write_json_file(meta_path, payload)
        return payload

    def fetch_person_biography(self, nconst: str, fetch_reason: str) -> dict[str, Any]:
        """Stahni biografii osoby s fallbackem mezi primarnim a ceskym locale."""

        self._require_token()
        meta_path = _person_biography_meta_path(nconst)
        existing_meta = _load_json_file(meta_path)
        if existing_meta:
            status = str(existing_meta.get("status") or "")
            if status in {"fetched", "no_biography", "not_found"}:
                return existing_meta
        match = self._api_get(f"/find/{nconst}", {"external_source": "imdb_id"})
        person_results = match.get("person_results") or []
        if not person_results:
            payload = {
                "nconst": nconst,
                "status": "not_found",
                "fetch_reason": fetch_reason,
                "updated_at": time.time(),
            }
            _write_json_file(meta_path, payload)
            return payload
        first = person_results[0]
        tmdb_person_id = int(first["id"])
        selected_payload: dict[str, Any] | None = None
        for locale in (PRIMARY_LOCALE, FALLBACK_LOCALE, None):
            query = {"language": locale} if locale else None
            detail = self._api_get(f"/person/{tmdb_person_id}", query)
            biography = str(detail.get("biography") or "").strip()
            if not biography:
                continue
            selected_payload = {
                "nconst": nconst,
                "status": "fetched",
                "tmdb_person_id": tmdb_person_id,
                "name": detail.get("name") or first.get("name"),
                "locale": locale or "default",
                "biography": biography,
                "fetch_reason": fetch_reason,
                "updated_at": time.time(),
            }
            break
        if selected_payload is None:
            selected_payload = {
                "nconst": nconst,
                "status": "no_biography",
                "tmdb_person_id": tmdb_person_id,
                "name": first.get("name"),
                "fetch_reason": fetch_reason,
                "updated_at": time.time(),
            }
        _write_json_file(meta_path, selected_payload)
        return selected_payload

    def get_person_portrait_status(self, nconst: str) -> dict[str, Any]:
        """Vrat lokalni stav portratu bez dalsiho sitoveho volani."""

        meta_path = _person_portrait_meta_path(nconst)
        portrait_path = _person_portrait_file_path(nconst)
        meta = _load_json_file(meta_path) or {}
        status = str(meta.get("status") or "missing")
        if status == "fetched" and portrait_path and portrait_path.exists():
            return {
                "nconst": nconst,
                "status": "fetched",
                "has_portrait": True,
                "portrait_path": portrait_path.as_posix(),
                "meta": meta,
            }
        if status in {"no_profile", "not_found"}:
            return {
                "nconst": nconst,
                "status": status,
                "has_portrait": False,
                "portrait_path": None,
                "meta": meta,
            }
        return {
            "nconst": nconst,
            "status": "missing",
            "has_portrait": bool(portrait_path and portrait_path.exists()),
            "portrait_path": portrait_path.as_posix() if portrait_path and portrait_path.exists() else None,
            "meta": meta,
        }

    def get_person_biography_status(self, nconst: str) -> dict[str, Any]:
        """Vrat lokalni stav biografie bez dalsiho sitoveho volani."""

        meta_path = _person_biography_meta_path(nconst)
        meta = _load_json_file(meta_path) or {}
        status = str(meta.get("status") or "missing")
        if status == "fetched" and str(meta.get("biography") or "").strip():
            return {
                "nconst": nconst,
                "status": "fetched",
                "has_biography": True,
                "meta": meta,
            }
        if status in {"no_biography", "not_found"}:
            return {
                "nconst": nconst,
                "status": status,
                "has_biography": False,
                "meta": meta,
            }
        return {
            "nconst": nconst,
            "status": "missing",
            "has_biography": False,
            "meta": meta,
        }

    def get_enrichment_targets(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Rozsir kandidaty z DB o odvozeny stav syncu a assetu."""

        targets = get_tmdb_enrichment_targets(limit=limit)
        items: list[dict[str, Any]] = []
        for target in targets:
            tconst = target["tconst"]
            items.append({**target, "needs_sync": _needs_sync(tconst), "tmdb_status": self.get_tmdb_status(tconst)})
        return items

    def get_tmdb_status(self, tconst: str) -> dict[str, Any]:
        """Sloz souhrn mapovani, locale a assetu pro jeden titul."""

        mapping = get_tmdb_mapping(tconst)
        locales = get_tmdb_detail_locales(tconst)
        asset_summary = get_tmdb_asset_summary(tconst)
        content_detail = get_content_detail(tconst)
        tmdb_detail = ((content_detail or {}).get("tmdb") or {}).get("details") or {}
        return {
            "has_mapping": mapping is not None,
            "mapping": mapping,
            "locales": locales,
            "assets": asset_summary,
            "expected_assets": {
                "poster": bool(tmdb_detail.get("poster_path")),
                "backdrop": bool(tmdb_detail.get("backdrop_path")),
            },
            "is_complete": not _missing_locales(locales) and not _missing_asset_kinds(tmdb_detail, asset_summary),
        }

    def enrich_library_from_tmdb(
        self,
        limit: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        priority_tconsts: list[str] | None = None,
    ) -> dict[str, Any]:
        """Projdi kandidaty a dopln lokalni TMDB data i assety po davkach."""

        self._require_token()
        targets = get_tmdb_enrichment_targets(limit=limit, include_complete=False, priority_tconsts=priority_tconsts)
        summary: dict[str, Any] = {
            "requested_limit": limit,
            "candidate_count": len(targets),
            "processed": 0,
            "skipped": 0,
            "not_found": 0,
            "synced": 0,
            "partials": 0,
            "asset_fetches": 0,
            "errors": 0,
            "items": [],
        }

        def emit(event: dict[str, Any]) -> None:
            """Predej progress event volajicimu, pokud callback existuje."""

            if progress_callback is not None:
                progress_callback(event)

        for target in targets:
            tconst = target["tconst"]
            started_at = time.monotonic()
            target_result: dict[str, Any] = {
                "tconst": tconst,
                "primary_title": target["primary_title"],
                "title_type": target["title_type"],
                "priority": target["priority"],
                "reasons": target["reasons"],
            }
            summary["processed"] += 1
            emit({"phase": "item", "event": "start", "tconst": tconst, "primary_title": target["primary_title"], "priority": target["priority"]})
            sync_results: list[dict[str, Any]] = []
            assets_result: dict[str, Any] | None = None
            current_stage = "status_check"
            try:
                status_before = self.get_tmdb_status(tconst)
                if status_before["is_complete"]:
                    summary["skipped"] += 1
                    target_result["status"] = "skipped"
                    target_result["tmdb_status"] = status_before
                    target_result["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
                    emit({"phase": "item", "event": "done", "tconst": tconst, "status": "skipped", "elapsed_seconds": target_result["elapsed_seconds"]})
                    summary["items"].append(target_result)
                    continue
                for locale in _missing_locales(status_before["locales"]):
                    current_stage = f"sync:{locale}"
                    sync_results.append(self.sync_title_from_imdb(tconst, locale=locale))
                current_stage = "asset_check"
                status_after_sync = self.get_tmdb_status(tconst)
                tmdb_detail_after_sync = ((get_content_detail(tconst) or {}).get("tmdb") or {}).get("details") or {}
                if _missing_asset_kinds(tmdb_detail_after_sync, status_after_sync["assets"]):
                    current_stage = "assets_fetch"
                    assets_result = self.fetch_assets_for_title(tconst, fetch_reason=_fetch_reason_for_priority(target["priority"]))
                    summary["asset_fetches"] += len(assets_result["assets"])
                item_status = "partial" if assets_result and assets_result["errors"] else "synced"
                if item_status == "partial":
                    summary["partials"] += 1
                else:
                    summary["synced"] += 1
                target_result["status"] = item_status
                target_result["sync_results"] = sync_results
                target_result["assets_result"] = assets_result
                target_result["tmdb_status"] = self.get_tmdb_status(tconst)
                target_result["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
                emit(
                    {
                        "phase": "item",
                        "event": "done",
                        "tconst": tconst,
                        "status": item_status,
                        "elapsed_seconds": target_result["elapsed_seconds"],
                        "synced_locales": [item["locale"] for item in sync_results],
                        "fetched_assets": len((assets_result or {}).get("assets", [])),
                        "asset_errors": len((assets_result or {}).get("errors", [])),
                    }
                )
            except TmdbNotFoundError as exc:
                summary["not_found"] += 1
                self._mark_tmdb_not_found(tconst, str(exc))
                target_result["status"] = "not_found"
                target_result["error"] = str(exc)
                target_result["error_stage"] = current_stage
                target_result["sync_results"] = sync_results
                target_result["assets_result"] = assets_result
                target_result["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
                emit({"phase": "item", "event": "not_found", "tconst": tconst, "error_stage": current_stage, "error": str(exc), "elapsed_seconds": target_result["elapsed_seconds"]})
            except TmdbApiError as exc:
                summary["errors"] += 1
                self._try_mark_existing_mapping_error(tconst, str(exc))
                target_result["status"] = "error"
                target_result["error"] = str(exc)
                target_result["error_stage"] = current_stage
                target_result["sync_results"] = sync_results
                target_result["assets_result"] = assets_result
                target_result["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
                emit({"phase": "item", "event": "error", "tconst": tconst, "error_stage": current_stage, "error": str(exc), "elapsed_seconds": target_result["elapsed_seconds"]})
            except Exception as exc:
                summary["errors"] += 1
                self._try_mark_existing_mapping_error(tconst, f"{type(exc).__name__}: {exc}")
                target_result["status"] = "error"
                target_result["error"] = f"{type(exc).__name__}: {exc}"
                target_result["error_stage"] = current_stage
                target_result["sync_results"] = sync_results
                target_result["assets_result"] = assets_result
                target_result["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
                emit({"phase": "item", "event": "error", "tconst": tconst, "error_stage": current_stage, "error": target_result["error"], "elapsed_seconds": target_result["elapsed_seconds"]})
            summary["items"].append(target_result)
        return summary

    def _api_get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        """Proved JSON GET nad TMDB API s retry a autorizaci."""

        token = self._require_token()
        query_string = f"?{urlencode(query)}" if query else ""
        request = Request(
            f"{TMDB_API_BASE}{path}{query_string}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        body = self._request_with_retry(request, timeout=30, error_prefix="TMDB request")
        return json.loads(body.decode("utf-8"))

    def _binary_get(self, url: str) -> bytes:
        """Stahni binarni obsah assetu s retry politikou shodnou s API dotazy."""

        request = Request(url, headers={"Accept": "*/*"})
        return self._request_with_retry(request, timeout=60, error_prefix="Stazeni assetu")

    def _get_tmdb_configuration(self) -> dict[str, Any]:
        """Vrat a pripadne nacacheuj globalni konfiguraci obrazkovych velikosti."""

        if self._configuration_cache is None:
            self._configuration_cache = self._api_get("/configuration")
        return self._configuration_cache

    def _try_mark_existing_mapping_error(self, tconst: str, message: str) -> None:
        """Pokud mapovani existuje, prepis jeho status na chybu bez shozeni batch runu."""

        try:
            existing_mapping = get_tmdb_mapping(tconst)
            if existing_mapping is None:
                return
            upsert_tmdb_mapping(
                tconst,
                existing_mapping["tmdb_media_type"],
                existing_mapping["tmdb_id"],
                existing_mapping["matched_by"],
                "error",
                last_error=message,
            )
        except Exception:
            return

    def _mark_tmdb_not_found(self, tconst: str, message: str) -> None:
        """Zaznamenej stav nenalezeno i kdyz titul dosud nema plne mapovani."""

        try:
            existing_mapping = get_tmdb_mapping(tconst)
            if existing_mapping is not None:
                upsert_tmdb_mapping(
                    tconst,
                    existing_mapping["tmdb_media_type"],
                    existing_mapping["tmdb_id"],
                    existing_mapping["matched_by"],
                    "not_found",
                    last_error=message,
                )
                return
            upsert_tmdb_mapping(tconst, "unknown", 0, "imdb_id", "not_found", last_error=message)
        except Exception:
            return

    def _require_token(self) -> str:
        """Nacti bearer token z prostredi nebo vyhod domyslenou konfiguracni chybu."""

        token = os.getenv("TMDB_API_READ_ACCESS_TOKEN")
        if not token:
            raise TmdbConfigError("Chybi TMDB_API_READ_ACCESS_TOKEN v prostredi.")
        return token

    def _request_with_retry(self, request: Request, *, timeout: int, error_prefix: str) -> bytes:
        """Proved HTTP pozadavek s retry politikou a lokalnim rate-limitem."""

        last_error: Exception | None = None
        for attempt in range(1, TMDB_MAX_RETRIES + 1):
            self._wait_for_rate_limit_slot()
            try:
                with urlopen(request, timeout=timeout) as response:
                    return response.read()
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < TMDB_MAX_RETRIES:
                    time.sleep(_retry_delay_seconds(exc, attempt))
                    last_error = exc
                    continue
                raise TmdbApiError(f"{error_prefix} selhal HTTP {exc.code}: {body}") from exc
            except URLError as exc:
                if attempt < TMDB_MAX_RETRIES:
                    time.sleep(min(2**attempt, 10))
                    last_error = exc
                    continue
                raise TmdbApiError(f"{error_prefix} selhal: {exc}") from exc
            except (TimeoutError, OSError) as exc:
                if attempt < TMDB_MAX_RETRIES:
                    time.sleep(min(2**attempt, 10))
                    last_error = exc
                    continue
                raise TmdbApiError(f"{error_prefix} selhal: {exc}") from exc
        raise TmdbApiError(f"{error_prefix} selhal po {TMDB_MAX_RETRIES} pokusech: {last_error}")

    def _wait_for_rate_limit_slot(self) -> None:
        """Zajisti, ze klient neprekroci konzervativni pocet TMDB requestu za sekundu."""

        now = time.monotonic()
        while self._request_timestamps and now - self._request_timestamps[0] >= 1.0:
            self._request_timestamps.popleft()
        if len(self._request_timestamps) >= TMDB_SAFE_RATE_LIMIT_PER_SECOND:
            sleep_for = max(0.0, 1.0 - (now - self._request_timestamps[0]))
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            while self._request_timestamps and now - self._request_timestamps[0] >= 1.0:
                self._request_timestamps.popleft()
        self._request_timestamps.append(time.monotonic())


_TMDB_CLIENT = TmdbClient()


def _client() -> TmdbClient:
    """Vrat sdilenou instanci TMDB klienta pro cely proces."""

    return _TMDB_CLIENT


def sync_title_from_imdb(tconst: str, locale: str = PRIMARY_LOCALE) -> dict[str, Any]:
    """Kompatibilni wrapper nad sdilenym TMDB klientem pro sync jednoho titulu."""

    return _client().sync_title_from_imdb(tconst, locale=locale)


def fetch_assets_for_title(tconst: str, fetch_reason: str) -> dict[str, Any]:
    """Kompatibilni wrapper pro stahovani chybejicich assetu titulu."""

    return _client().fetch_assets_for_title(tconst, fetch_reason)


def fetch_person_portrait(nconst: str, fetch_reason: str) -> dict[str, Any]:
    """Kompatibilni wrapper pro materializaci portretu osoby."""

    return _client().fetch_person_portrait(nconst, fetch_reason)


def fetch_person_biography(nconst: str, fetch_reason: str) -> dict[str, Any]:
    """Kompatibilni wrapper pro materializaci biografie osoby."""

    return _client().fetch_person_biography(nconst, fetch_reason)


def get_person_portrait_status(nconst: str) -> dict[str, Any]:
    """Kompatibilni wrapper pro cteni lokalniho stavu portratu osoby."""

    return _client().get_person_portrait_status(nconst)


def get_person_biography_status(nconst: str) -> dict[str, Any]:
    """Kompatibilni wrapper pro cteni lokalniho stavu biografie osoby."""

    return _client().get_person_biography_status(nconst)


def get_enrichment_targets(limit: int | None = None) -> list[dict[str, Any]]:
    """Kompatibilni wrapper pro cteni kandidatu na TMDB enrichment."""

    return _client().get_enrichment_targets(limit=limit)


def get_tmdb_status(tconst: str) -> dict[str, Any]:
    """Kompatibilni wrapper pro souhrnny stav TMDB dat jednoho titulu."""

    return _client().get_tmdb_status(tconst)


def enrich_library_from_tmdb(
    limit: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    priority_tconsts: list[str] | None = None,
) -> dict[str, Any]:
    """Kompatibilni wrapper pro davkovy TMDB enrichment lokalni knihovny."""

    return _client().enrich_library_from_tmdb(
        limit=limit,
        progress_callback=progress_callback,
        priority_tconsts=priority_tconsts,
    )


def _api_get(path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    """Kompatibilni wrapper pro JSON GET pres sdileny TMDB klient."""

    return _client()._api_get(path, query)


def _binary_get(url: str) -> bytes:
    """Kompatibilni wrapper pro stazeni binarniho assetu."""

    return _client()._binary_get(url)


def _get_tmdb_configuration() -> dict[str, Any]:
    """Kompatibilni wrapper pro konfiguracni cache TMDB klienta."""

    return _client()._get_tmdb_configuration()


def _try_mark_existing_mapping_error(tconst: str, message: str) -> None:
    """Kompatibilni wrapper pro zapsani chyboveho stavu existujiciho mapovani."""

    _client()._try_mark_existing_mapping_error(tconst, message)


def _mark_tmdb_not_found(tconst: str, message: str) -> None:
    """Kompatibilni wrapper pro zaznam stavu nenalezeno v TMDB mapovani."""

    _client()._mark_tmdb_not_found(tconst, message)


def _preferred_size(available_sizes: list[str], preferred: str) -> str:
    """Vyber preferovanou velikost, nebo nejvetsi dostupny fallback."""

    if preferred in available_sizes:
        return preferred
    return available_sizes[-1]


def _relative_asset_path(tconst: str, asset_kind: str, filename: str) -> Path:
    """Sestav relativni cestu assetu uvnitr lokalniho TMDB uloziste."""

    return Path(tconst) / asset_kind / filename


def _person_portrait_meta_path(nconst: str) -> Path:
    """Vrat cestu k JSON metadatum lokalniho portratu osoby."""

    return PEOPLE_ASSETS_DIR / nconst / "portrait.json"


def _person_biography_meta_path(nconst: str) -> Path:
    """Vrat cestu k JSON metadatum lokalni biografie osoby."""

    return PEOPLE_ASSETS_DIR / nconst / "biography.json"


def _person_portrait_file_path(nconst: str) -> Path | None:
    """Najdi existujici lokalni soubor portretu osoby podle podporovanych suffixu."""

    person_dir = PEOPLE_ASSETS_DIR / nconst
    for suffix in ("jpg", "jpeg", "webp", "png"):
        candidate = person_dir / f"portrait.{suffix}"
        if candidate.exists():
            return candidate
    return None


def _remove_other_portrait_files(nconst: str, keep: Path) -> None:
    """Smaz stare varianty portretu, aby v adresari zustal jen jeden aktivni soubor."""

    person_dir = PEOPLE_ASSETS_DIR / nconst
    for suffix in ("jpg", "jpeg", "webp", "png"):
        candidate = person_dir / f"portrait.{suffix}"
        if candidate == keep:
            continue
        if candidate.exists():
            candidate.unlink()


def _load_json_file(path: Path) -> dict[str, Any] | None:
    """Bezpecne nacti JSON objekt z disku a pri chybe vrat `None`."""

    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """Zapis JSON metadata sousedici s lokalnim TMDB assetem."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _needs_sync(tconst: str) -> bool:
    """Rozhodni, zda je lokalni TMDB stav titulu stale nekompletni."""

    status = get_tmdb_status(tconst)
    return not status["is_complete"]


def _missing_locales(locales: list[str]) -> list[str]:
    """Vrat seznam pozadovanych locale, ktera pro titul jeste chybi."""

    missing: list[str] = []
    for locale in (PRIMARY_LOCALE, FALLBACK_LOCALE):
        if locale not in locales:
            missing.append(locale)
    return missing


def _missing_asset_kinds(
    tmdb_detail: dict[str, Any],
    asset_summary: dict[str, dict[str, Any]],
) -> list[str]:
    """Vrat typy assetu, ktere detail ocekava, ale lokalne jeste neexistuji."""

    missing: list[str] = []
    expected_paths = {
        "poster": tmdb_detail.get("poster_path"),
        "backdrop": tmdb_detail.get("backdrop_path"),
    }
    for asset_kind, path_value in expected_paths.items():
        if not path_value:
            continue
        asset = asset_summary.get(asset_kind)
        if not asset or not asset.get("exists"):
            missing.append(asset_kind)
    return missing


def _fetch_reason_for_priority(priority: int) -> str:
    """Preved internni prioritu cile na srozumitelny duvod fetch akce."""

    if priority == 1:
        return "watched"
    if priority == 2:
        return "in_progress"
    return "previewed"


def _require_token() -> str:
    """Kompatibilni wrapper pro nacteni TMDB bearer tokenu."""

    return _client()._require_token()


def _request_with_retry(request: Request, *, timeout: int, error_prefix: str) -> bytes:
    """Kompatibilni wrapper pro HTTP retry politiku TMDB klienta."""

    return _client()._request_with_retry(request, timeout=timeout, error_prefix=error_prefix)


def _wait_for_rate_limit_slot() -> None:
    """Kompatibilni wrapper pro lokalni TMDB rate-limit frontu."""

    _client()._wait_for_rate_limit_slot()


def _retry_delay_seconds(exc: HTTPError, attempt: int) -> float:
    """Vypocitej cekani po HTTP 429 s podporou hlavicky `Retry-After`."""

    retry_after = exc.headers.get("Retry-After")
    if retry_after:
        try:
            return max(1.0, float(retry_after))
        except ValueError:
            pass
    return min(float(2**attempt), 30.0)
