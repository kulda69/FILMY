from __future__ import annotations

import json
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PLEX_PREFS_PATH = Path.home() / "Library/Application Support/Plex/Plex Media Server/Preferences.xml"


class PlexConfigError(RuntimeError):
    """Raised when local Plex configuration is missing or incomplete."""


class PlexApiError(RuntimeError):
    """Raised when a Plex HTTP request fails."""


@dataclass(frozen=True)
class PlexResource:
    name: str
    product: str
    provides: str
    client_identifier: str
    access_token: str
    owned: bool
    presence: bool
    connections: tuple[dict[str, Any], ...]


def get_plex_online_token() -> str:
    """Load the Plex account token from local Plex Media Server preferences."""
    if not PLEX_PREFS_PATH.exists():
        raise PlexConfigError(f"Chybí Plex Preferences.xml: {PLEX_PREFS_PATH}")

    root = ET.parse(PLEX_PREFS_PATH).getroot()
    token = root.attrib.get("PlexOnlineToken")
    if not token:
        raise PlexConfigError("V Plex Preferences.xml chybí PlexOnlineToken.")
    return token


def get_resources() -> list[PlexResource]:
    """Return Plex devices/resources visible to the current Plex account."""
    root = _xml_get(f"https://plex.tv/api/resources?includeHttps=1&X-Plex-Token={get_plex_online_token()}")
    resources: list[PlexResource] = []
    for device in root.findall("Device"):
        connections = tuple(connection.attrib for connection in device.findall("Connection"))
        resources.append(
            PlexResource(
                name=device.attrib.get("name", ""),
                product=device.attrib.get("product", ""),
                provides=device.attrib.get("provides", ""),
                client_identifier=device.attrib.get("clientIdentifier", ""),
                access_token=device.attrib.get("accessToken", ""),
                owned=device.attrib.get("owned") == "1",
                presence=device.attrib.get("presence") == "1",
                connections=connections,
            )
        )
    return resources


def get_primary_server() -> PlexResource | None:
    """Pick the first owned, online Plex Media Server resource."""
    for resource in get_resources():
        if resource.product == "Plex Media Server" and resource.owned and resource.presence:
            return resource
    return None


def get_library_sections(resource: PlexResource | None = None) -> list[dict[str, Any]]:
    """Return library sections from the selected Plex Media Server."""
    server = resource or get_primary_server()
    if server is None:
        return []

    root = _server_xml_get(server, "/library/sections")
    return [section.attrib for section in root.findall("Directory")]


def get_section_items(
    section_key: str,
    *,
    resource: PlexResource | None = None,
    start: int = 0,
    size: int = 3,
) -> list[dict[str, Any]]:
    """Return a small sample of items from one Plex library section."""
    server = resource or get_primary_server()
    if server is None:
        return []

    path = f"/library/sections/{section_key}/all?X-Plex-Container-Start={start}&X-Plex-Container-Size={size}"
    root = _server_xml_get(server, path)
    items: list[dict[str, Any]] = []
    for child in root:
        if child.tag not in {"Video", "Directory"}:
            continue
        items.append(
            {
                "type": child.attrib.get("type"),
                "title": child.attrib.get("title"),
                "rating_key": child.attrib.get("ratingKey"),
                "guid": child.attrib.get("guid"),
                "year": child.attrib.get("year"),
                "view_count": child.attrib.get("viewCount"),
                "viewed_leaf_count": child.attrib.get("viewedLeafCount"),
                "leaf_count": child.attrib.get("leafCount"),
            }
        )
    return items


def iter_section_items(
    section_key: str,
    *,
    resource: PlexResource | None = None,
    page_size: int = 100,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return paginated items from one Plex library section."""
    server = resource or get_primary_server()
    if server is None:
        return []

    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        root = _server_xml_get(
            server,
            f"/library/sections/{section_key}/all?X-Plex-Container-Start={offset}&X-Plex-Container-Size={page_size}",
        )
        batch: list[dict[str, Any]] = []
        for child in root:
            if child.tag not in {"Video", "Directory"}:
                continue
            batch.append(
                {
                    "type": child.attrib.get("type"),
                    "title": child.attrib.get("title"),
                    "rating_key": child.attrib.get("ratingKey"),
                    "guid": child.attrib.get("guid"),
                    "year": child.attrib.get("year"),
                    "view_count": child.attrib.get("viewCount"),
                    "viewed_leaf_count": child.attrib.get("viewedLeafCount"),
                    "leaf_count": child.attrib.get("leafCount"),
                }
            )
        if not batch:
            break
        items.extend(batch)
        if limit is not None and len(items) >= limit:
            return items[:limit]
        offset += len(batch)
    return items


def get_metadata_snapshot(rating_key: str, *, resource: PlexResource | None = None) -> dict[str, Any] | None:
    """Return a compact metadata snapshot for one Plex item."""
    server = resource or get_primary_server()
    if server is None:
        return None

    root = _server_xml_get(server, f"/library/metadata/{rating_key}?includeGuids=1")
    item = next(iter(root), None)
    if item is None:
        return None

    guids = [guid.attrib.get("id", "") for guid in item.findall("Guid")]
    people_tag = "Director" if item.tag == "Video" else "Role"
    return {
        "rating_key": item.attrib.get("ratingKey"),
        "type": item.attrib.get("type"),
        "title": item.attrib.get("title"),
        "year": item.attrib.get("year"),
        "guid": item.attrib.get("guid"),
        "ids": _extract_external_ids(guids),
        "library_section_id": item.attrib.get("librarySectionID"),
        "library_section_title": item.attrib.get("librarySectionTitle"),
        "view_count": item.attrib.get("viewCount"),
        "viewed_leaf_count": item.attrib.get("viewedLeafCount"),
        "leaf_count": item.attrib.get("leafCount"),
        "last_viewed_at": item.attrib.get("lastViewedAt"),
        "added_at": item.attrib.get("addedAt"),
        "updated_at": item.attrib.get("updatedAt"),
        "originally_available_at": item.attrib.get("originallyAvailableAt"),
        "directors": [person.attrib.get("tag") for person in item.findall("Director")[:5]],
        "roles": [person.attrib.get("tag") for person in item.findall("Role")[:10]],
        "genres": [genre.attrib.get("tag") for genre in item.findall("Genre")[:10]],
        "countries": [country.attrib.get("tag") for country in item.findall("Country")[:5]],
        "people_source_tag": people_tag,
        "guids": guids,
    }


def get_metadata_guids(rating_key: str, *, resource: PlexResource | None = None) -> list[str]:
    """Return GUID identifiers attached to one Plex metadata item."""
    server = resource or get_primary_server()
    if server is None:
        return []

    root = _server_xml_get(server, f"/library/metadata/{rating_key}?includeGuids=1")
    item = next(iter(root), None)
    if item is None:
        return []
    return [guid.attrib.get("id", "") for guid in item.findall("Guid")]


def inspect_plex_state() -> dict[str, Any]:
    """Build a concise read-only snapshot of the current Plex setup."""
    resources = get_resources()
    primary = get_primary_server()
    return {
        "resources": [
            {
                "name": resource.name,
                "product": resource.product,
                "provides": resource.provides,
                "owned": resource.owned,
                "presence": resource.presence,
                "connections": list(resource.connections),
            }
            for resource in resources
        ],
        "primary_server": {
            "name": primary.name,
            "client_identifier": primary.client_identifier,
            "connections": list(primary.connections),
            "sections": get_library_sections(primary),
        }
        if primary
        else None,
    }


def build_import_probe(
    *,
    section_limit: int = 2,
    item_limit: int = 5,
    resource: PlexResource | None = None,
) -> dict[str, Any]:
    """Build a read-only import probe over the main Plex movie/show sections."""
    server = resource or get_primary_server()
    if server is None:
        return {"primary_server": None, "sections": []}

    candidate_sections = [
        section
        for section in get_library_sections(server)
        if section.get("type") in {"movie", "show"} and section.get("hidden") != "1"
    ][:section_limit]

    sections: list[dict[str, Any]] = []
    for section in candidate_sections:
        items = get_section_items(section["key"], resource=server, size=item_limit)
        detailed_items = [
            get_metadata_snapshot(item["rating_key"], resource=server)
            for item in items
            if item.get("rating_key")
        ]
        sections.append(
            {
                "key": section.get("key"),
                "title": section.get("title"),
                "type": section.get("type"),
                "agent": section.get("agent"),
                "scanner": section.get("scanner"),
                "sample_items": [item for item in detailed_items if item is not None],
            }
        )

    return {
        "primary_server": {
            "name": server.name,
            "client_identifier": server.client_identifier,
        },
        "sections": sections,
    }


def _server_xml_get(resource: PlexResource, path: str) -> ET.Element:
    local_connection = next((connection for connection in resource.connections if connection.get("local") == "1"), None)
    connection = local_connection or (resource.connections[0] if resource.connections else None)
    if connection is None:
        raise PlexApiError(f"Server {resource.name} nemá žádné connection URI.")

    separator = "&" if "?" in path else "?"
    url = f"{connection['uri']}{path}{separator}X-Plex-Token={resource.access_token}"
    return _xml_get(url)


def _xml_get(url: str) -> ET.Element:
    request = Request(url, headers={"Accept": "application/xml"})
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    try:
        with urlopen(request, timeout=30, context=ssl_context) as response:
            return ET.fromstring(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise PlexApiError(f"Plex HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise PlexApiError(f"Plex request selhal: {exc}") from exc


def _debug_dump_json(data: dict[str, Any]) -> str:
    """Return human-readable JSON for ad hoc diagnostics."""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _extract_external_ids(guids: list[str]) -> dict[str, str | None]:
    ids: dict[str, str | None] = {"imdb": None, "tmdb": None, "tvdb": None}
    for guid in guids:
        if guid.startswith("imdb://"):
            ids["imdb"] = guid.removeprefix("imdb://")
        elif guid.startswith("tmdb://"):
            ids["tmdb"] = guid.removeprefix("tmdb://")
        elif guid.startswith("tvdb://"):
            ids["tvdb"] = guid.removeprefix("tvdb://")
    return ids
