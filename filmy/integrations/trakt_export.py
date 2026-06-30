from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from filmy.paths import DATA_DIR, ENV_PATH

EXPORTS_DIR = DATA_DIR / "trakt_exports"
TRAKT_API_BASE = "https://api.trakt.tv"

load_dotenv(ENV_PATH)


class TraktConfigError(RuntimeError):
    pass


class TraktApiError(RuntimeError):
    pass


def export_personal_lists() -> dict[str, Any]:
    client_id, client_secret = _require_credentials()
    token_payload = _authorize_device_flow(client_id, client_secret)
    access_token = token_payload["access_token"]

    exported_at = _now_utc()
    export_dir = EXPORTS_DIR / exported_at.strftime("%Y%m%d-%H%M%S")
    export_dir.mkdir(parents=True, exist_ok=True)

    lists = _api_get(
        "/users/me/lists",
        access_token=access_token,
        query={"limit": "100", "page": "1"},
    )

    list_summaries: list[dict[str, Any]] = []
    for item in lists:
        list_id = str(item["ids"]["trakt"])
        slug = item["ids"].get("slug") or f"list-{list_id}"
        safe_slug = _safe_slug(slug)
        list_items = _api_get(
            f"/users/me/lists/{list_id}/items/movie,show,season,episode",
            access_token=access_token,
            query={"limit": "all", "extended": "full"},
        )

        payload = {
            "exported_at": exported_at.isoformat(),
            "list": item,
            "items": list_items,
        }
        output_path = export_dir / f"{safe_slug}.json"
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        list_summaries.append(
            {
                "name": item.get("name"),
                "trakt_id": list_id,
                "slug": slug,
                "privacy": item.get("privacy"),
                "item_count": len(list_items),
                "path": output_path.as_posix(),
            }
        )

    manifest = {
        "exported_at": exported_at.isoformat(),
        "export_dir": export_dir.as_posix(),
        "list_count": len(list_summaries),
        "lists": list_summaries,
    }
    manifest_path = export_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _authorize_device_flow(client_id: str, client_secret: str) -> dict[str, Any]:
    device = _api_post("/oauth/device/code", {"client_id": client_id})
    verification_url = device["verification_url"]
    user_code = device["user_code"]
    device_code = device["device_code"]
    interval = int(device["interval"])
    expires_in = int(device["expires_in"])

    expires_at = time.monotonic() + expires_in
    print("Trakt autorizace:")
    print(f"1. Otevři: {verification_url}")
    print(f"2. Zadej kód: {user_code}")
    activate_url = _build_activate_url(verification_url, user_code)
    if activate_url is not None:
        print(f"Rychlý odkaz: {activate_url}")
    print("Čekám na potvrzení v Traktu...")

    while time.monotonic() < expires_at:
        time.sleep(interval)
        try:
            return _api_post(
                "/oauth/device/token",
                {
                    "code": device_code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        except TraktApiError as exc:
            message = str(exc)
            if "HTTP 400" in message:
                continue
            if "HTTP 429" in message:
                interval += 1
                continue
            if "HTTP 418" in message:
                raise TraktApiError("Autorizace byla v Traktu zamítnuta.") from exc
            if "HTTP 410" in message:
                raise TraktApiError("Autorizační kód expiroval. Spusť export znovu.") from exc
            raise

    raise TraktApiError("Vypršel čas pro device-code autorizaci. Spusť export znovu.")


def _api_get(path: str, access_token: str | None = None, query: dict[str, str] | None = None) -> Any:
    query_string = f"?{urlencode(query)}" if query else ""
    request = Request(
        f"{TRAKT_API_BASE}{path}{query_string}",
        headers=_headers(access_token),
    )
    return _read_json(request)


def _api_post(path: str, payload: dict[str, Any], access_token: str | None = None) -> Any:
    request = Request(
        f"{TRAKT_API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(access_token),
        method="POST",
    )
    return _read_json(request)


def _headers(access_token: str | None = None) -> dict[str, str]:
    client_id = os.getenv("TRAKT_CLIENT_ID")
    if not client_id:
        raise TraktConfigError("Chybí TRAKT_CLIENT_ID v .env.")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "filmy/0.1.0",
        "trakt-api-key": client_id,
        "trakt-api-version": "2",
        "Accept": "application/json",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _read_json(request: Request) -> Any:
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise TraktApiError(f"Trakt HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise TraktApiError(f"Trakt request selhal: {exc}") from exc


def _require_credentials() -> tuple[str, str]:
    client_id = os.getenv("TRAKT_CLIENT_ID")
    client_secret = os.getenv("TRAKT_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise TraktConfigError(
            "Chybí TRAKT_CLIENT_ID nebo TRAKT_CLIENT_SECRET v .env. "
            "Vytvoř aplikaci na https://trakt.tv/oauth/applications."
        )
    if "@" in client_id or client_id.endswith(".com"):
        raise TraktConfigError(
            "TRAKT_CLIENT_ID vypadá jako email nebo login, ne jako API client_id. "
            "Použij hodnoty z https://trakt.tv/oauth/applications."
        )
    return client_id, client_secret


def _build_activate_url(verification_url: str, user_code: str) -> str | None:
    if verification_url.rstrip("/") == "https://trakt.tv/activate":
        return f"{verification_url.rstrip('/')}/{user_code}"
    return None


def _safe_slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.lower())
    cleaned = cleaned.strip("-")
    return cleaned or "list"


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def main() -> int:
    try:
        manifest = export_personal_lists()
    except (TraktApiError, TraktConfigError) as exc:
        print(f"Chyba: {exc}", file=sys.stderr)
        return 1

    print(f"Export hotov: {manifest['list_count']} seznamů")
    print(f"Výstup: {manifest['export_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
