"""Title/person presentation buildry a cache helpery."""

from __future__ import annotations

"""Title/person presentation and cache helpers extracted from `filmy.db`."""

import hashlib
import importlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from filmy.runtime_postgres import (
    fetch_catalog_episode_row,
    fetch_catalog_primary_title,
    fetch_catalog_refresh_fingerprint,
    fetch_catalog_title_row,
    fetch_known_for_title_rows,
    fetch_person_catalog_row,
    fetch_person_credit_rows,
    fetch_person_episode_series_credit_rows,
)


def _db():
    """Vrat facade modul `filmy.db` kvuli kompatibilnim helperum a konstantam."""
    return importlib.import_module("filmy.db")


class TitlePresentationBuilder:
    """Sklada title presentation pro jeden titul.

    Trida drzi kontext jednoho `tconst` a oddeluje kroky potrebne pro:
    - nacteni detailu,
    - dohledani souvisejicich lidi,
    - dopocteni UI poli,
    - a ulozeni presentation cache.

    Cilem neni vytvaret novou abstrakcni vrstvu nad celym modulem, ale
    zprehlednit konkretni vicekrokovy build procesu, ktery uz prestaval byt
    citelny jako jedna dlouha funkce.
    """

    def __init__(self, tconst: str) -> None:
        """Inicializuje builder pro konkretni titul.

        Parametry:
        - tconst: IMDb identita titulu nebo epizody, pro kterou se ma slozit
          finalni presentation payload.
        """

        self.db = _db()
        self.tconst = tconst

    def build(self) -> dict[str, Any] | None:
        """Vrati title presentation, pripadne `None`, kdyz titul neexistuje.

        Metoda nejdriv zkusí disk cache. Pokud cache neni pouzitelna, slozi
        presentation z aktualniho detailu, title people bloku a TMDB payloadu
        a nasledne ji ulozi zpet do cache.
        """

        if not self.tconst:
            return None
        cached = _load_cached_title_presentation(None, self.tconst)
        if cached is not None:
            return cached
        detail = self.db.get_content_detail(self.tconst)
        if detail is None:
            return None
        people = self.db._fetch_title_people(None, self.tconst)
        cache_fingerprint = _title_cache_source_fingerprint(None, self.tconst)
        presentation = self._build_from_detail(detail, people)
        presentation["display_text"] = render_title_presentation(presentation)
        _store_cached_title_presentation(self.tconst, presentation, cache_fingerprint)
        return presentation

    def build_people_panel(self) -> dict[str, Any] | None:
        """Vrati zjednoduseny credits payload pro partial `main-cast`.

        Tato varianta umi overit existenci titulu a vratit jen cast/director/
        writer/create blok bez celeho skladani detailove presentation.
        """

        exists = fetch_catalog_title_row(self.tconst) is not None or fetch_catalog_episode_row(self.tconst) is not None
        if not exists:
            return None
        people = self.db._fetch_title_people(None, self.tconst)
        return {
            "tconst": self.tconst,
            "directed_by": people["directors"],
            "written_by": people["writers"],
            "created_by": people["creators"],
            "main_cast": people["cast"],
        }

    def _build_from_detail(self, detail: dict[str, Any], people: dict[str, Any]) -> dict[str, Any]:
        """Prevede title detail a kredity na finalni presentation slovnik."""

        series_title = detail.get("series_title")
        if detail.get("kind") == "episode" and detail.get("series_tconst") and series_title is None:
            series_title = fetch_catalog_primary_title(str(detail["series_tconst"]))
        tmdb_payload = detail.get("tmdb") or {}
        tmdb_details = tmdb_payload.get("details") or {}
        overview = tmdb_details.get("overview")
        providers = [
            provider["provider_name"]
            for provider in tmdb_payload.get("providers") or []
            if provider.get("provider_name")
        ]
        unique_providers = list(dict.fromkeys(providers))
        return {
            "tconst": detail["tconst"],
            "title": detail.get("primary_title"),
            "original_title": detail.get("original_title"),
            "title_type": detail.get("title_type"),
            "kind": detail.get("kind"),
            "kind_label": self.db._title_type_label(detail.get("title_type")),
            "year": detail.get("start_year"),
            "end_year": detail.get("end_year"),
            "runtime_minutes": detail.get("runtime_minutes"),
            "genres": detail.get("genres") or [],
            "imdb_rating": detail.get("average_rating"),
            "imdb_votes": detail.get("num_votes"),
            "overview": overview,
            "tmdb_details": tmdb_details,
            "tmdb_providers": tmdb_payload.get("providers") or [],
            "directed_by": people["directors"],
            "written_by": people["writers"],
            "created_by": people["creators"],
            "main_cast": people["cast"],
            "available_in_czechia": unique_providers,
            "library_state": detail.get("library") or {},
            "content_state": detail.get("content_state") or {},
            "episodes": detail.get("episodes") or [],
            "aliases": detail.get("aliases") or [],
            "tmdb_locales": (detail.get("tmdb") or {}).get("detail_locales") or [],
            "poster_url": self.db._poster_url_from_detail(detail),
            "backdrop_url": self.db._backdrop_url_from_detail(detail),
            "series_tconst": detail.get("series_tconst"),
            "series_title": series_title,
            "season_number": detail.get("season_number"),
            "episode_number": detail.get("episode_number"),
            "has_poster": any((asset.get("asset_kind") == "poster" for asset in tmdb_payload.get("assets") or [])),
            "has_backdrop": any((asset.get("asset_kind") == "backdrop" for asset in tmdb_payload.get("assets") or [])),
        }


class PersonPresentationBuilder:
    """Sklada detailovou presentation a cache source detail pro jednu osobu.

    Trida soustreduje vice navazujicich kroku:
    - nacteni katalogove identity osoby,
    - rozdeleni filmografie do sekci,
    - doplneni affinity, portraitu a biografie,
    - a pripravu payloadu vhodneho pro cache fingerprint i presentation text.
    """

    def __init__(self, nconst: str, conn: Any = None) -> None:
        """Inicializuje builder pro konkretni osobu.

        Parametry:
        - nconst: IMDb identita osoby.
        - conn: volitelny DB kontext, ktery zustava kompatibilni s historickou
          signaturou helperu v tomto modulu.
        """

        self.db = _db()
        self.nconst = nconst
        self.conn = conn

    def build_cache_source_detail(self) -> dict[str, Any] | None:
        """Slozi plny person payload pouzivany pro cache a render.

        Vysledkem je stejny slovnik jako drive vracel helper
        `_fetch_person_cache_source_detail()`, jen je skladani presunute do
        tridy se sdilenym kontextem.
        """

        person = fetch_person_catalog_row(self.nconst)
        if person is None:
            return None
        credits = fetch_person_credit_rows(self.nconst, limit=500)
        affinity_rating = self.db._get_person_affinity_rating(self.conn, person[0])
        filmography, credit_count, seen_titles = self._build_filmography(credits)
        if affinity_rating > 0:
            for episode_series_entry in _fetch_person_episode_series_credits(self.conn, self.nconst, existing_tconsts=seen_titles):
                filmography["acted"].append(episode_series_entry)
                seen_titles.add(str(episode_series_entry["tconst"]))
            filmography["acted"].sort(
                key=lambda item: (item.get("start_year") or 0, item.get("title") or ""),
                reverse=True,
            )
        presentation = {
            "nconst": person[0],
            "name": person[1],
            "birth_year": person[2],
            "death_year": person[3],
            "primary_profession": person[4],
            "known_for_titles": person[5],
            "known_for_items": _fetch_known_for_items(self.conn, person[5]),
            "filmography": filmography,
            "credit_count": credit_count,
            "portrait_url": _person_portrait_url(person[0]),
            "has_portrait": _person_portrait_path(person[0]) is not None,
            "affinity_rating": affinity_rating,
            "biography": _person_biography_payload(person[0]),
        }
        presentation["display_text"] = render_person_presentation(presentation)
        return presentation

    def _build_filmography(
        self,
        credits: Sequence[tuple[Any, ...]],
    ) -> tuple[dict[str, list[dict[str, Any]]], int, set[str]]:
        """Roztridi syrove kredity do presentation sekci filmografie."""

        filmography: dict[str, list[dict[str, Any]]] = {
            "directed": [],
            "written": [],
            "created": [],
            "acted": [],
            "other": [],
        }
        credit_count = 0
        seen_titles: set[str] = set()
        for row in credits:
            credit_count += 1
            tconst = row[5]
            if tconst in seen_titles:
                continue
            seen_titles.add(tconst)
            entry = {
                "tconst": tconst,
                "title": row[6],
                "original_title": row[7],
                "start_year": row[8],
                "title_type": row[9],
                "credit_group": row[0],
                "category": row[1],
                "job": row[2],
                "character": self.db._principal_character(row[3]),
            }
            if row[0] == "director":
                filmography["directed"].append(entry)
            elif row[0] == "creator":
                filmography["created"].append(entry)
            elif row[0] == "writer":
                filmography["written"].append(entry)
            elif row[0] == "cast":
                filmography["acted"].append(entry)
            else:
                filmography["other"].append(entry)
        return filmography, credit_count, seen_titles


def get_person_portrait_summary(nconst: str) -> dict[str, Any]:
    """Return only portrait-related person data without building full person presentation."""
    return {
        "portrait_url": _person_portrait_url(nconst),
        "has_portrait": _person_portrait_path(nconst) is not None,
    }


def _fetch_known_for_items(conn, known_for_titles: str | None) -> list[dict[str, Any]]:
    """Preved CSV seznam `known_for_titles` na kratke title payloady v puvodnim poradi."""
    if not known_for_titles:
        return []
    ordered_tconsts = [item.strip() for item in str(known_for_titles).split(",") if item.strip()]
    if not ordered_tconsts:
        return []
    rows = fetch_known_for_title_rows(ordered_tconsts)
    items_by_tconst = {row[0]: {"tconst": row[0], "title": row[1], "start_year": row[2]} for row in rows}
    return [items_by_tconst[tconst] for tconst in ordered_tconsts if tconst in items_by_tconst]


def get_title_presentation(tconst: str) -> dict[str, Any] | None:
    """Vrati presentation payload titulu vcetne cache vrstvy.

    Parametry:
    - tconst: IMDb identita titulu nebo epizody.

    Navrat:
    - Slovnik s presentation poli pro UI a textovy render.
    - `None`, pokud titul v lokalnich datech neexistuje.
    """

    return _get_title_presentation_cached(tconst)


def get_title_people_panel(tconst: str) -> dict[str, Any] | None:
    """Vrati jen credits blok potrebny pro partial `main-cast`.

    Parametry:
    - tconst: IMDb identita titulu nebo epizody.

    Navrat:
    - Zjednoduseny payload s reziji, scenarem, created-by a hlavnim obsazenim.
    - `None`, pokud titul neexistuje.
    """

    return TitlePresentationBuilder(tconst).build_people_panel()


def render_title_presentation(presentation: dict[str, Any]) -> str:
    """Preved title presentation slovnik na lidsky citelny textovy souhrn."""
    lines: list[str] = []
    lines.append(str(presentation["title"]))
    meta_bits = [presentation["kind_label"]]
    if presentation.get("year") is not None:
        meta_bits.append(str(presentation["year"]))
    lines.append(", ".join(meta_bits))
    genres = presentation.get("genres") or []
    if genres:
        lines.append(" / ".join(genres))
    rating = presentation.get("imdb_rating")
    if rating is not None:
        votes = presentation.get("imdb_votes")
        vote_suffix = f" ({votes} votes)" if votes is not None else ""
        lines.append(f"IMDb: {rating}/10{vote_suffix}")
    overview = presentation.get("overview")
    if overview:
        lines.append("")
        lines.append("What it's about")
        lines.append(str(overview))
    if presentation.get("created_by"):
        lines.append("")
        lines.append("Created by")
        lines.append(", ".join((person["name"] for person in presentation["created_by"])))
    if presentation.get("directed_by"):
        lines.append("")
        lines.append("Directed by")
        lines.append(", ".join((person["name"] for person in presentation["directed_by"])))
    if presentation.get("written_by"):
        lines.append("")
        lines.append("Written by")
        lines.append(", ".join((person["name"] for person in presentation["written_by"])))
    if presentation.get("main_cast"):
        lines.append("")
        lines.append("Main cast")
        for person in presentation["main_cast"]:
            role = f" as {person['character']}" if person.get("character") else ""
            lines.append(f"{person['name']}{role}")
    if presentation.get("available_in_czechia"):
        lines.append("")
        lines.append("Available in Czechia")
        for provider in presentation["available_in_czechia"]:
            lines.append(provider)
    library = presentation.get("library_state") or {}
    if library:
        lines.append("")
        lines.append("Your local library state")
        if library.get("watched_count") is not None:
            times = "time" if library["watched_count"] == 1 else "times"
            lines.append(f"Watched {library['watched_count']} {times}")
        if library.get("last_watched_at") is not None:
            lines.append(f"Last watched: {library['last_watched_at']}")
        if library.get("in_watchlist"):
            lines.append("In watchlist")
        if library.get("rating") is not None:
            lines.append(f"Your rating: {library['rating']}/10")
    if presentation.get("episodes"):
        lines.append("")
        lines.append("Episodes")
        lines.append(f"{len(presentation['episodes'])} episodes loaded")
    local_bits: list[str] = []
    if presentation.get("tmdb_locales"):
        local_bits.append("TMDB detail: " + ", ".join(presentation["tmdb_locales"]))
    asset_bits: list[str] = []
    if presentation.get("has_poster"):
        asset_bits.append("poster")
    if presentation.get("has_backdrop"):
        asset_bits.append("backdrop")
    if asset_bits:
        local_bits.append("assets: " + ", ".join(asset_bits))
    if local_bits:
        lines.append("")
        lines.append("Available locally")
        lines.extend(local_bits)
    return "\n".join(lines)


def render_person_presentation(presentation: dict[str, Any]) -> str:
    """Preved person presentation slovnik na lidsky citelny textovy souhrn."""
    lines: list[str] = []
    lines.append(str(presentation["name"]))
    meta_bits: list[str] = []
    if presentation.get("birth_year") is not None:
        meta_bits.append(str(presentation["birth_year"]))
    if presentation.get("death_year") is not None:
        meta_bits.append(str(presentation["death_year"]))
    if presentation.get("primary_profession"):
        meta_bits.append(str(presentation["primary_profession"]))
    if meta_bits:
        lines.append(", ".join(meta_bits))
    known_for_items = presentation.get("known_for_items") or []
    known_for = presentation.get("known_for_titles") or ""
    if known_for_items:
        lines.append("")
        lines.append("Known for")
        lines.append(", ".join((item["title"] for item in known_for_items)))
    elif known_for:
        lines.append("")
        lines.append("Known for")
        lines.append(known_for)
    biography = (presentation.get("biography") or {}).get("text")
    if biography:
        lines.append("")
        lines.append("Biography")
        lines.append(str(biography))
    filmography = presentation.get("filmography") or {}
    sections = [
        ("Directed", filmography.get("directed") or []),
        ("Created by", filmography.get("created") or []),
        ("Written", filmography.get("written") or []),
        ("Acted in", filmography.get("acted") or []),
    ]
    for section_title, items in sections:
        if not items:
            continue
        lines.append("")
        lines.append(section_title)
        for item in items[:20]:
            year = f" ({item['start_year']})" if item.get("start_year") is not None else ""
            role = f" as {item['character']}" if item.get("character") else ""
            lines.append(f"{item['title']}{year}{role}")
    other_items = filmography.get("other") or []
    if other_items:
        lines.append("")
        lines.append("Other credits")
        for item in other_items[:10]:
            year = f" ({item['start_year']})" if item.get("start_year") is not None else ""
            lines.append(f"{item['title']}{year}")
    lines.append("")
    lines.append(f"Total credits: {presentation.get('credit_count') or 0}")
    return "\n".join(lines)


def _title_detail_cache_path(tconst: str) -> Path:
    """Vrat cestu k lokalnimu `detail.json` cache souboru titulu."""
    if not tconst:
        raise ValueError("tconst is required")
    return _db().ASSETS_DIR / tconst / "detail.json"


def _person_detail_cache_path(nconst: str) -> Path:
    """Vrat cestu k lokalnimu `detail.json` cache souboru osoby."""
    if not nconst:
        raise ValueError("nconst is required")
    return _db().PEOPLE_ASSETS_DIR / nconst / "detail.json"


def _person_portrait_path(nconst: str) -> Path | None:
    """Najdi existujici lokalni soubor s portretem osoby."""
    if not nconst:
        return None
    person_dir = _db().PEOPLE_ASSETS_DIR / nconst
    for suffix in ("jpg", "jpeg", "webp", "png"):
        candidate = person_dir / f"portrait.{suffix}"
        if candidate.exists():
            return candidate
    return None


def _person_portrait_url(nconst: str) -> str | None:
    """Preved lokalni portrait asset osoby na obslouzitelnou URL."""
    portrait_path = _person_portrait_path(nconst)
    if portrait_path is None:
        return None
    return _db()._asset_url_from_local_path(
        portrait_path.as_posix(),
        assets_root=_db().PEOPLE_ASSETS_DIR,
        mount_path="/assets/people",
    )


def _person_biography_path(nconst: str) -> Path:
    """Vrat cestu k lokalnimu JSON souboru s biografii osoby."""
    return _db().PEOPLE_ASSETS_DIR / nconst / "biography.json"


def _person_biography_meta(nconst: str) -> dict[str, Any] | None:
    """Nacti surovy biography metadata payload z lokalniho souboru."""
    path = _person_biography_path(nconst)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _person_biography_payload(nconst: str) -> dict[str, Any] | None:
    """Vrat jen pouzitelnou cast biografie osoby pro presentation vrstvu."""
    meta = _person_biography_meta(nconst)
    if not meta or str(meta.get("status") or "") != "fetched":
        return None
    biography = str(meta.get("biography") or "").strip()
    if not biography:
        return None
    return {
        "text": biography,
        "locale": meta.get("locale"),
        "tmdb_person_id": meta.get("tmdb_person_id"),
        "updated_at": meta.get("updated_at"),
    }


def _title_detail_cache_status(tconst: str, source_fingerprint: str) -> str:
    """Vyhodnot stav title detail cache proti aktualnimu source fingerprintu."""
    cache_path = _title_detail_cache_path(tconst)
    if not cache_path.exists():
        return "missing"
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(cached, dict):
        return "invalid"
    if cached.get("tconst") != tconst:
        return "stale"
    if cached.get("cache_version") != _db().TITLE_PRESENTATION_CACHE_VERSION:
        return "stale"
    if cached.get("source_fingerprint") != source_fingerprint:
        return "stale"
    return "ready"


def _person_detail_cache_status(nconst: str, source_fingerprint: str) -> str:
    """Vyhodnot stav person detail cache proti aktualnimu source fingerprintu."""
    cache_path = _person_detail_cache_path(nconst)
    if not cache_path.exists():
        return "missing"
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(cached, dict):
        return "invalid"
    if cached.get("nconst") != nconst:
        return "stale"
    if cached.get("source_fingerprint") != source_fingerprint:
        return "stale"
    return "ready"


def _load_cached_title_presentation(conn, tconst: str) -> dict[str, Any] | None:
    """Nacti cached title presentation jen kdyz je stale validni a kompletni."""
    cache_path = _title_detail_cache_path(tconst)
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get("tconst") != tconst:
        return None
    expected_fingerprint = _title_cache_source_fingerprint(conn, tconst)
    if expected_fingerprint is None:
        return None
    if _title_detail_cache_status(tconst, expected_fingerprint) != "ready":
        return None
    if cached.get("kind") not in {"title", "episode"}:
        return None
    if cached.get("cache_version") != _db().TITLE_PRESENTATION_CACHE_VERSION:
        return None
    if "has_poster" not in cached or "has_backdrop" not in cached:
        return None
    if cached.get("has_poster") and (not cached.get("poster_url")):
        return None
    return cached


def _load_cached_person_presentation(conn, nconst: str) -> dict[str, Any] | None:
    """Nacti cached person presentation jen kdyz je stale validni."""
    cache_path = _person_detail_cache_path(nconst)
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get("nconst") != nconst:
        return None
    expected_fingerprint = _person_cache_source_fingerprint(conn, nconst)
    if expected_fingerprint is None:
        return None
    if _person_detail_cache_status(nconst, expected_fingerprint) != "ready":
        return None
    return cached


def _store_cached_title_presentation(tconst: str, presentation: dict[str, Any], source_fingerprint: str | None) -> None:
    """Uloz title presentation do disk cache s fingerprintem a cache verzi."""
    if not source_fingerprint:
        return
    cache_path = _title_detail_cache_path(tconst)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _jsonify_for_cache(
        {
            **presentation,
            "cache_version": _db().TITLE_PRESENTATION_CACHE_VERSION,
            "source_fingerprint": source_fingerprint,
            "cached_at": _db()._now_iso(),
        }
    )
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _store_cached_person_presentation(nconst: str, presentation: dict[str, Any], source_fingerprint: str | None) -> None:
    """Uloz person presentation do disk cache s fingerprintem zdroje."""
    if not source_fingerprint:
        return
    cache_path = _person_detail_cache_path(nconst)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _jsonify_for_cache(
        {**presentation, "source_fingerprint": source_fingerprint, "cached_at": _db()._now_iso()}
    )
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _title_cache_source_fingerprint(conn, tconst: str, detail: dict[str, Any] | None = None) -> str | None:
    """Spocti fingerprint vseho, co muze ovlivnit title presentation cache."""
    refresh_fingerprint = fetch_catalog_refresh_fingerprint()
    if refresh_fingerprint is None:
        return None
    if detail is None:
        detail = _fetch_title_cache_source_detail(conn, tconst)
    if detail is None:
        return None
    payload = {
        "refresh": refresh_fingerprint,
        "detail": {
            "tconst": detail.get("tconst"),
            "title_type": detail.get("title_type") or detail.get("kind"),
            "title": detail.get("primary_title"),
            "original_title": detail.get("original_title"),
            "start_year": detail.get("start_year"),
            "end_year": detail.get("end_year"),
            "runtime_minutes": detail.get("runtime_minutes"),
            "genres": detail.get("genres") or [],
            "tmdb_locales": (detail.get("tmdb") or {}).get("detail_locales") or [],
            "tmdb_assets": _tmdb_asset_summary_signature(detail.get("tmdb") or {}),
            "library": detail.get("library") or {},
            "content_state": detail.get("content_state") or {},
            "aliases": detail.get("aliases") or [],
        },
    }
    digest = hashlib.sha256(
        json.dumps(_jsonify_for_cache(payload), sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    return digest.hexdigest()


def _person_cache_source_fingerprint(conn, nconst: str, presentation: dict[str, Any] | None = None) -> str | None:
    """Spocti fingerprint vseho, co muze ovlivnit person presentation cache."""
    refresh_fingerprint = fetch_catalog_refresh_fingerprint()
    if refresh_fingerprint is None:
        return None
    if presentation is None:
        presentation = _fetch_person_cache_source_detail(conn, nconst)
        if presentation is None:
            return None
    payload = {
        "refresh": refresh_fingerprint,
        "person": {
            "nconst": presentation.get("nconst"),
            "name": presentation.get("name"),
            "birth_year": presentation.get("birth_year"),
            "death_year": presentation.get("death_year"),
            "primary_profession": presentation.get("primary_profession"),
            "known_for_titles": presentation.get("known_for_titles"),
            "filmography": presentation.get("filmography") or {},
            "credit_count": presentation.get("credit_count"),
            "portrait_url": presentation.get("portrait_url"),
            "has_portrait": presentation.get("has_portrait"),
            "affinity_rating": presentation.get("affinity_rating") or 0,
            "biography": presentation.get("biography") or None,
        },
    }
    digest = hashlib.sha256(
        json.dumps(_jsonify_for_cache(payload), sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    return digest.hexdigest()


def _fetch_person_cache_source_detail(conn, nconst: str) -> dict[str, Any] | None:
    """Sloz plny person payload pouzivany jako zdroj pro cache fingerprint i render."""
    return PersonPresentationBuilder(nconst, conn=conn).build_cache_source_detail()


def _fetch_person_episode_series_credits(
    conn, nconst: str, *, existing_tconsts: set[str] | None = None
) -> list[dict[str, Any]]:
    """Aggregate episode-only acting credits to their parent series."""
    try:
        rows = fetch_person_episode_series_credit_rows(nconst, limit=200)
    except Exception:
        return []
    blocked = existing_tconsts or set()
    items: list[dict[str, Any]] = []
    for row in rows:
        series_tconst = str(row[0])
        if series_tconst in blocked:
            continue
        episode_count = int(row[5] or 0)
        if episode_count <= 0:
            continue
        items.append(
            {
                "tconst": series_tconst,
                "title": row[1],
                "original_title": row[2],
                "start_year": row[3],
                "title_type": row[4],
                "credit_group": "cast",
                "category": "actor",
                "job": f"{episode_count} episodes",
                "character": None,
            }
        )
    return items


def _fetch_title_cache_source_detail(conn, tconst: str) -> dict[str, Any] | None:
    """Nacti title detail payload pouzivany pro cache fingerprint a presentation build."""
    title = fetch_catalog_title_row(tconst)
    if title is not None:
        title_detail: dict[str, Any] = {
            "tconst": title[0],
            "title_type": title[1],
            "primary_title": title[2],
            "original_title": title[3],
            "start_year": title[4],
            "end_year": title[5],
            "runtime_minutes": title[6],
            "genres": title[7] or [],
        }
    else:
        episode = fetch_catalog_episode_row(tconst)
        if episode is None:
            return None
        title_detail = {
            "tconst": episode[0],
            "kind": "episode",
            "primary_title": episode[4],
            "original_title": episode[5],
            "start_year": episode[6],
            "runtime_minutes": episode[7],
        }
    title_detail["aliases"] = _db()._fetch_aliases(conn, tconst)
    title_detail["content_state"] = _db()._fetch_content_state(conn, tconst)
    title_detail["library"] = _db()._fetch_library_summary(conn, tconst, title[1] if title is not None else "tvEpisode")
    title_detail["tmdb"] = _db()._fetch_tmdb(conn, tconst)
    return title_detail


def _tmdb_asset_summary_signature(tmdb: dict[str, Any]) -> list[dict[str, Any]]:
    """Vytahni z TMDB payloadu jen asset cast podstatnou pro cache fingerprint."""
    assets = tmdb.get("assets") or []
    return [
        {
            "kind": asset.get("asset_kind"),
            "path": asset.get("local_path") or asset.get("relative_path"),
            "status": asset.get("status"),
            "sha256": asset.get("sha256"),
        }
        for asset in assets
    ]


def _tmdb_detail_is_cache_ready(tmdb: dict[str, Any] | None) -> bool:
    """Over, ze TMDB detail uz ma obe locale a vsechny potrebne assety."""
    if not tmdb:
        return False
    locales = set(tmdb.get("detail_locales") or [])
    if "en-US" not in locales or "cs-CZ" not in locales:
        return False
    details = tmdb.get("details") or {}
    fetched_assets = {asset.get("asset_kind") for asset in tmdb.get("assets") or [] if asset.get("status") == "fetched"}
    if details.get("poster_path") and "poster" not in fetched_assets:
        return False
    if details.get("backdrop_path") and "backdrop" not in fetched_assets:
        return False
    return True


def _jsonify_for_cache(value: Any) -> Any:
    """Preved Python hodnoty na JSON-safe tvar pro zapis disk cache."""
    if isinstance(value, dict):
        return {str(key): _jsonify_for_cache(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonify_for_cache(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonify_for_cache(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, set):
        return sorted((_jsonify_for_cache(item) for item in value))
    return value


def _get_title_presentation_cached(tconst: str) -> dict[str, Any] | None:
    """Vrati title presentation s vyuzitim disk cache a builder tridy.

    Parametry:
    - tconst: IMDb identita titulu nebo epizody.

    Navrat:
    - Zcacheovana nebo cerstve slozena presentation.
    - `None`, pokud titul neexistuje.
    """

    return TitlePresentationBuilder(tconst).build()
