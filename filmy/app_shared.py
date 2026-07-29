"""Sdilene FastAPI/Jinja helpery, response modely a UI utility."""

from __future__ import annotations

import json
import threading
import time
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from fastapi import Request
from pydantic import BaseModel, Field
from starlette.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from filmy.background_jobs import BackgroundJobSupervisor, signal_background_activity
from filmy.db import (
    format_czech_datetime,
    get_hot_watchlist_page,
    get_person_presentation,
    get_person_portrait_summary,
    get_recently_watched_page,
    get_title_presentation,
    get_user_list_items_page,
    get_watched_page,
)
from filmy.integrations.tmdb import fetch_person_portrait
from filmy.markdown import render_user_markdown
from filmy.paths import PROJECT_ROOT

background_supervisor = BackgroundJobSupervisor()
templates = Jinja2Templates(directory=(PROJECT_ROOT / "templates").as_posix())
templates.env.filters["markdown"] = render_user_markdown
_homepage_warmup_lock = threading.Lock()
_homepage_warmup_thread: threading.Thread | None = None
_person_portrait_warmup_lock = threading.Lock()
_person_portrait_warmup_active: set[str] = set()
_person_presentation_warmup_lock = threading.Lock()
_person_presentation_warmup_active: set[str] = set()
_BREADCRUMB_TRAIL_PARAM = "_trail"
_BREADCRUMB_LABEL_PARAM = "_label"
_BREADCRUMB_TS_PARAM = "_ts"


class WatchlistUpdateRequest(BaseModel):
    """Local watchlist toggle for one title or episode."""

    in_watchlist: bool
    notes: str | None = None


class RatingUpdateRequest(BaseModel):
    """Local user rating on the 1-10 IMDb-like scale."""

    rating: int = Field(ge=1, le=10)
    liked_notes: str | None = None
    disliked_notes: str | None = None


class WatchEventCreateRequest(BaseModel):
    """Append a local watch event."""

    watched_on: str | None = Field(default=None, description="ISO date YYYY-MM-DD.")
    notes: str | None = None


class BreadcrumbNavigation:
    """Spravce breadcrumb a return-to navigace pro cele HTML UI.

    Tato trida drzi vsechna pravidla kolem bezpecnych internich URL,
    serializace breadcrumb trailu do query parametru a skladani
    navratovych targetu po formularovych akcich. Smyslem je mit
    jednu logickou vrstvu pro navigaci misto roztristenych helperu,
    ktere by se daly snadno rozjet do vice nekompatibilnich variant.
    """

    trail_param = _BREADCRUMB_TRAIL_PARAM
    label_param = _BREADCRUMB_LABEL_PARAM
    ts_param = _BREADCRUMB_TS_PARAM

    @classmethod
    def safe_back_target(cls, candidate: str | None) -> str | None:
        """Vrat jen bezpecny interni relativni target, jinak `None`.

        UI nikdy nesmi slepe nasledovat externi URL z formulare nebo
        refereru. Povoluji se jen lokalni cesty zacinajici `/`, bez
        schematu a bez hosta.
        """
        if not candidate:
            return None
        parsed = urlsplit(candidate)
        if parsed.scheme or parsed.netloc:
            return None
        if not parsed.path.startswith("/"):
            return None
        return urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))

    @classmethod
    def request_back_target(cls, request: Request, return_to: str | None = None) -> str:
        """Najdi nejlepsi rodicovsky target z explicitniho `return_to` nebo refereru."""
        explicit = cls.safe_back_target(return_to)
        if explicit:
            return explicit

        referer = request.headers.get("referer")
        if referer:
            parsed = urlsplit(referer)
            if parsed.path.startswith("/"):
                return urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))
        return "/"

    @classmethod
    def sanitize_label(cls, label: str | None) -> str | None:
        """Zkrat a ocisti zobrazovany breadcrumb label na bezpecny text."""
        text = str(label or "").strip()
        if not text:
            return None
        return text[:80]

    @classmethod
    def normalize_item(cls, url: str | None, label: str | None) -> dict[str, str] | None:
        """Preved kandidat na validni breadcrumb polozku nebo `None`."""
        safe_url = cls.safe_back_target(url)
        safe_label = cls.sanitize_label(label)
        if not safe_url or not safe_label:
            return None
        return {"url": safe_url, "label": safe_label}

    @classmethod
    def decode_trail(cls, value: str | None) -> list[dict[str, str]]:
        """Dekoduj serialized breadcrumb trail z query parametru.

        Vstup muze byt chybejici, rozbity nebo umyslne zkonstruovany
        mimo appku. Proto se trail po JSON decode znovu sanitizuje,
        deduplikuje a zkracuje na rozumnou delku.
        """
        if not value:
            return []
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []

        trail: list[dict[str, str]] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            item = cls.normalize_item(entry.get("url"), entry.get("label"))
            if item is None:
                continue
            if trail and trail[-1] == item:
                continue
            trail.append(item)
        return trail[:8]

    @classmethod
    def encode_trail(cls, trail: list[dict[str, str]]) -> str:
        """Zakoduj breadcrumb trail do kompaktniho ASCII JSON tvaru."""
        return json.dumps(trail, separators=(",", ":"), ensure_ascii=True)

    @classmethod
    def split_navigation_query(cls, target: str) -> tuple[list[dict[str, str]], str | None, list[tuple[str, str]]]:
        """Rozdel query na breadcrumb cast a funkcni parametry stranky."""
        parts = urlsplit(target)
        functional_pairs: list[tuple[str, str]] = []
        trail: list[dict[str, str]] = []
        label: str | None = None
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key == cls.trail_param:
                trail = cls.decode_trail(value)
                continue
            if key == cls.label_param:
                label = cls.sanitize_label(value)
                continue
            if key == cls.ts_param:
                continue
            functional_pairs.append((key, value))
        return trail, label, functional_pairs

    @classmethod
    def strip_navigation_params(cls, target: str) -> str:
        """Odstran breadcrumb parametry a ponech jen funkcni URL."""
        parts = urlsplit(target)
        _, _, functional_pairs = cls.split_navigation_query(target)
        return urlunsplit(("", "", parts.path, urlencode(functional_pairs), parts.fragment))

    @classmethod
    def strip_ephemeral_params(cls, target: str) -> str:
        """Odstran jen cache-busting timestamp, ostatni query zachovej."""
        safe_target = cls.safe_back_target(target) or "/"
        parts = urlsplit(safe_target)
        query_pairs = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != cls.ts_param
        ]
        return urlunsplit(("", "", parts.path, urlencode(query_pairs), parts.fragment))

    @classmethod
    def breadcrumb_items_from_target(cls, target: str | None) -> list[dict[str, str]]:
        """Vypocti viditelny breadcrumb trail z encoded targetu."""
        safe_target = cls.safe_back_target(target)
        if not safe_target:
            return []

        trail, label, _ = cls.split_navigation_query(safe_target)
        if label:
            current_item = cls.normalize_item(cls.strip_ephemeral_params(safe_target), label)
            if current_item and (not trail or trail[-1] != current_item):
                trail.append(current_item)
        return trail

    @classmethod
    def build_target(
        cls,
        path: str,
        *,
        trail: list[dict[str, str]] | None = None,
        label: str | None = None,
        season: int | None = None,
        fragment: str | None = None,
    ) -> str:
        """Sloz interni target obohaceny o breadcrumb kontext.

        Funkcni query parametry zustavaji zachovane. Navigacni metadata
        se pred pridanim vzdy nejdriv ocisti, aby se pri opakovanem
        prochazeni appky nevrstvila zastarala data.
        """
        safe_path = cls.safe_back_target(path) or "/"
        parts = urlsplit(safe_path)
        query_pairs = list(parse_qsl(parts.query, keep_blank_values=True))
        query_pairs = [
            (key, value)
            for key, value in query_pairs
            if key not in {cls.trail_param, cls.label_param, cls.ts_param}
        ]
        if season is not None:
            query_pairs = [(key, value) for key, value in query_pairs if key != "season"]
            query_pairs.append(("season", str(season)))
        clean_trail = [item for item in (trail or []) if cls.normalize_item(item.get("url"), item.get("label"))]
        if clean_trail:
            query_pairs.append((cls.trail_param, cls.encode_trail(clean_trail)))
        safe_label = cls.sanitize_label(label)
        if safe_label:
            query_pairs.append((cls.label_param, safe_label))
        return urlunsplit(("", "", parts.path, urlencode(query_pairs), fragment if fragment is not None else parts.fragment))

    @classmethod
    def strip_return_to_param(cls, target: str) -> str:
        """Odstran technicky parametr `return_to` z aktualni URL."""
        safe_target = cls.safe_back_target(target) or "/"
        parts = urlsplit(safe_target)
        query_pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "return_to"]
        return urlunsplit(("", "", parts.path, urlencode(query_pairs), parts.fragment))

    @classmethod
    def build_context(
        cls,
        request: Request,
        current_label: str,
        *,
        return_to: str | None = None,
        default_trail: list[dict[str, str]] | None = None,
    ) -> dict[str, object]:
        """Spocitej `back_url`, breadcrumb trail a nove `page_return_to`.

        Tato metoda je hlavni vstup pro routy. Vezme aktualni request,
        pripadny parent target a rozhodne, jak ma vypadat navrat po
        formularove akci nebo dalsim drill-down kroku.
        """
        current_target = str(request.url.path if not request.url.query else f"{request.url.path}?{request.url.query}")
        current_trail, current_embedded_label, _ = cls.split_navigation_query(current_target)
        parent_target = cls.request_back_target(request, return_to)

        if return_to:
            breadcrumb_items = cls.breadcrumb_items_from_target(parent_target)
        elif current_embedded_label or current_trail:
            breadcrumb_items = current_trail
        else:
            breadcrumb_items = cls.breadcrumb_items_from_target(parent_target)

        if not breadcrumb_items and default_trail:
            breadcrumb_items = [item for item in default_trail if cls.normalize_item(item.get("url"), item.get("label"))]

        functional_current = cls.strip_navigation_params(current_target)
        functional_current = cls.strip_return_to_param(functional_current)
        page_return_to = cls.build_target(functional_current, trail=breadcrumb_items, label=current_label)
        back_url = breadcrumb_items[-1]["url"] if breadcrumb_items else parent_target
        return {"back_url": back_url, "breadcrumb_items": breadcrumb_items, "page_return_to": page_return_to}

    @classmethod
    def detail_return_target(
        cls,
        path: str,
        parent_return_to: str,
        *,
        season: int | None = None,
        fragment: str | None = None,
    ) -> str:
        """Sloz detail target, ktery zachova parent breadcrumb trail."""
        breadcrumb_items = cls.breadcrumb_items_from_target(parent_return_to)
        return cls.build_target(path, trail=breadcrumb_items, season=season, fragment=fragment)


def launch_homepage_warmup(tconsts: list[str]) -> None:
    """Warm selected title presentations in one background thread for the current page.

    The homepage repeatedly renders the same handful of title cards and detail links.
    Preloading their cached presentations lowers the cost of immediate follow-up clicks
    without blocking the request itself.
    """
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


def present_main_cast(main_cast: list[dict[str, object]]) -> list[dict[str, object]]:
    """Attach lightweight portrait metadata to main-cast people for title detail cards."""
    items: list[dict[str, object]] = []
    for person in main_cast:
        item = dict(person)
        nconst = str(person.get("nconst") or "").strip()
        if nconst:
            portrait = get_person_portrait_summary(nconst)
            item["portrait_url"] = portrait.get("portrait_url")
            item["has_portrait"] = bool(portrait.get("has_portrait"))
        else:
            item["portrait_url"] = None
            item["has_portrait"] = False
        items.append(item)
    return items


def attach_title_role_signals(
    main_cast: list[dict[str, object]],
    role_signals: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Attach title-local role signals to visible cast rows."""

    signals_by_nconst: dict[str, list[dict[str, object]]] = {}
    signals_by_character: dict[str, list[dict[str, object]]] = {}
    for signal in role_signals:
        nconst = str(signal.get("nconst") or "").strip()
        character_name = str(signal.get("character_name") or "").strip().casefold()
        if nconst:
            signals_by_nconst.setdefault(nconst, []).append(signal)
        if character_name:
            signals_by_character.setdefault(character_name, []).append(signal)

    items: list[dict[str, object]] = []
    for person in main_cast:
        item = dict(person)
        nconst = str(item.get("nconst") or "").strip()
        character_name = str(item.get("character") or "").strip().casefold()
        matched: list[dict[str, object]] = []
        if nconst:
            matched.extend(signals_by_nconst.get(nconst, []))
        if character_name:
            for signal in signals_by_character.get(character_name, []):
                if signal not in matched:
                    matched.append(signal)
        matched.sort(key=lambda signal: (int(signal.get("strength") or 0), str(signal.get("updated_at") or "")), reverse=True)
        item["role_signals"] = matched
        item["top_role_signal"] = matched[0] if matched else None
        item["role_signal_type_values"] = [str(signal.get("signal_type")) for signal in matched if signal.get("signal_type")]
        items.append(item)
    return items


def launch_person_portrait_warmup(main_cast: list[dict[str, object]]) -> None:
    """Fetch missing main-cast portraits in the background, one worker per person.

    The detail page should render immediately even if some people still miss portrait
    assets. This helper kicks off best-effort fetches only for the currently visible cast
    and avoids starting duplicate work for the same `nconst`.
    """
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


def launch_person_presentation_warmup(main_cast: list[dict[str, object]], limit: int = 8) -> None:
    """Warm full person presentations in background for the currently visible cast.

    The UI already knows the relevant `nconst` values from title detail. Prebuilding
    the person presentations makes the next click to actor/director detail much
    cheaper without blocking the title page itself.
    """
    candidate_nconsts = [
        str(person.get("nconst") or "").strip()
        for person in main_cast[: max(limit, 0)]
        if person.get("nconst")
    ]
    unique_nconsts = [nconst for nconst in dict.fromkeys(candidate_nconsts) if nconst]
    if not unique_nconsts:
        return

    def run(nconst: str) -> None:
        try:
            get_person_presentation(nconst)
        except Exception:
            pass
        finally:
            with _person_presentation_warmup_lock:
                _person_presentation_warmup_active.discard(nconst)

    for nconst in unique_nconsts:
        with _person_presentation_warmup_lock:
            if nconst in _person_presentation_warmup_active:
                continue
            _person_presentation_warmup_active.add(nconst)
        thread = threading.Thread(
            target=run,
            args=(nconst,),
            name=f"person-presentation-warmup-{nconst}",
            daemon=True,
        )
        thread.start()


def count_missing_portraits(main_cast: list[dict[str, object]]) -> int:
    """Spocita, kolik lidi v main cast payloadu nema portrait."""

    return sum(1 for person in main_cast if not person.get("has_portrait"))


def search_result_people_line(people: list[dict[str, object]] | None, limit: int = 4) -> str | None:
    """Slozi kratkou carkovou linku jmen pro search result kartu."""

    names = [str(person.get("name") or "").strip() for person in (people or []) if str(person.get("name") or "").strip()]
    if not names:
        return None
    visible = names[:limit]
    suffix = f" +{len(names) - limit}" if len(names) > limit else ""
    return ", ".join(visible) + suffix


def present_search_result_card(
    presentation: dict[str, object],
    *,
    match: dict[str, object] | None = None,
    return_to: str,
) -> dict[str, object]:
    """Normalize one title presentation into the compact card payload used by search UI."""
    library_state = presentation.get("library_state") or {}
    available_in_czechia = presentation.get("available_in_czechia") or []
    available_preview = list(available_in_czechia[:3])
    if len(available_in_czechia) > 3:
        available_preview.append(f"+{len(available_in_czechia) - 3}")
    fuzzy_score = match.get("fuzzy_score") if match else None
    return {
        "tconst": presentation.get("tconst"),
        "title": presentation.get("title"),
        "original_title": presentation.get("original_title"),
        "kind_label": presentation.get("kind_label"),
        "year": presentation.get("year"),
        "runtime_minutes": presentation.get("runtime_minutes"),
        "genres": presentation.get("genres") or [],
        "imdb_rating": presentation.get("imdb_rating"),
        "imdb_votes": presentation.get("imdb_votes"),
        "poster_url": presentation.get("poster_url"),
        "overview": presentation.get("overview"),
        "directed_by_line": search_result_people_line(presentation.get("directed_by")),
        "written_by_line": search_result_people_line(presentation.get("written_by")),
        "main_cast_line": search_result_people_line(presentation.get("main_cast"), limit=5),
        "created_by_line": search_result_people_line(presentation.get("created_by")),
        "available_in_czechia": available_preview,
        "watched_count": library_state.get("watched_count") or 0,
        "user_lists": library_state.get("lists") or [],
        "user_rating": (library_state.get("rating") or {}).get("value"),
        "detail_url": f"/titles/{presentation.get('tconst')}?return_to={quote_plus(return_to)}",
        "is_exact_match": bool(match and match.get("is_exact_match")),
        "fuzzy_score": fuzzy_score,
        "fuzzy_score_percent": round(float(fuzzy_score) * 100) if fuzzy_score is not None else None,
        "match_kind": "Exact" if match and match.get("is_exact_match") else "Closest",
    }


def present_person_search_result_card(
    presentation: dict[str, object],
    *,
    match: dict[str, object] | None = None,
    return_to: str,
) -> dict[str, object]:
    """Normalize one person presentation into the compact card payload used by search UI."""
    known_for_items = presentation.get("known_for_items") or []
    known_for_preview = [
        {
            "title": item.get("title"),
            "year": item.get("start_year"),
            "detail_url": f"/titles/{item.get('tconst')}?return_to={quote_plus(return_to)}",
        }
        for item in known_for_items[:4]
        if item.get("tconst") and item.get("title")
    ]
    filmography = presentation.get("filmography") or {}
    fuzzy_score = match.get("fuzzy_score") if match else None
    return {
        "nconst": presentation.get("nconst"),
        "name": presentation.get("name"),
        "birth_year": presentation.get("birth_year"),
        "death_year": presentation.get("death_year"),
        "primary_profession": presentation.get("primary_profession"),
        "credit_count": presentation.get("credit_count") or 0,
        "portrait_url": presentation.get("portrait_url"),
        "known_for_items": known_for_preview,
        "known_for_line": ", ".join(str(item.get("title")) for item in known_for_preview if item.get("title")),
        "acted_count": len(filmography.get("acted") or []),
        "directed_count": len(filmography.get("directed") or []),
        "written_count": len(filmography.get("written") or []),
        "created_count": len(filmography.get("created") or []),
        "detail_url": f"/people/{presentation.get('nconst')}?return_to={quote_plus(return_to)}",
        "is_exact_match": bool(match and match.get("is_exact_match")),
        "fuzzy_score": fuzzy_score,
        "fuzzy_score_percent": round(float(fuzzy_score) * 100) if fuzzy_score is not None else None,
    }


def signal_metadata_pipeline(reason: str, *, target_tconst: str | None = None) -> None:
    """Best-effort probudi background metadata pipeline po uzivatelske akci."""

    try:
        signal_background_activity(reason, target_tconst=target_tconst)
    except OSError:
        # Metadata wake-up is best-effort; the write action itself must still succeed.
        pass


def alias_bucket(alias: dict[str, object]) -> str | None:
    """Zaradi alias do preferovane jazykove skupiny pro zobrazeni."""

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


def present_title_aliases(presentation: dict[str, object]) -> list[dict[str, object]]:
    """Reduce raw aliases to one visible representative per preferred language bucket."""
    aliases = presentation.get("aliases") or []
    buckets: dict[str, list[dict[str, object]]] = {key: [] for key in ("en", "cs", "es", "de")}
    for alias in aliases:
        if isinstance(alias, dict):
            bucket = alias_bucket(alias)
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


def present_episode_seasons(episodes: list[object]) -> list[int]:
    """Vrat serazeny seznam unikatnich season cisel z episode payloadu."""
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


def present_title_episodes(episodes: list[object]) -> list[dict[str, object]]:
    """Preved interni episode tuple payload na citelne dict polozky pro sablonu."""
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


def selected_panel_page(selected_list: dict[str, object] | None, limit: int, offset: int = 0, available_in_cz: bool=False) -> dict[str, object]:
    """Resolve the selected homepage panel into the correct backing page source.

    The right-hand homepage panel can point either to a real user list or to derived
    read-only views such as `Watched` and `Recently Watched`. The template expects one
    uniform page-like payload, so this helper hides the branching.
    """
    if selected_list is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset, "list": None}
    if selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "hot_watchlist":
        return get_hot_watchlist_page(limit=limit, offset=offset, available_in_cz=available_in_cz)
    if selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "watched":
        return get_watched_page(limit=limit, offset=offset)
    if selected_list.get("item_type") == "view" and selected_list.get("view_kind") == "recently_watched":
        return get_recently_watched_page(limit=limit, offset=offset)
    return get_user_list_items_page(str(selected_list["id"]), limit=limit, offset=offset, available_in_cz=available_in_cz)


def card_action_move_targets(
    visible_lists: list[dict[str, object]],
    selected_list: dict[str, object] | None,
) -> list[dict[str, object]]:
    """Vrati seznamy, do kterych lze presunout kartu mimo aktualni panel."""

    selected_id = selected_list.get("id") if selected_list else None
    return [
        item
        for item in visible_lists
        if item.get("item_type") == "list" and item["id"] != selected_id
    ]


def redirect_back(return_to: str | None) -> RedirectResponse:
    """Redirect back to the calling page and force a fresh GET with a cache-busting stamp."""
    target = return_to or "/"
    parts = urlsplit(target)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query_pairs.append((_BREADCRUMB_TS_PARAM, str(time.time_ns())))
    refreshed = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment))
    response = RedirectResponse(url=refreshed, status_code=303)
    apply_html_cache_headers(response)
    return response


def apply_html_cache_headers(response: RedirectResponse | object) -> object:
    """Allow browser history/bfcache while still forcing revalidation on normal reloads.

    `no-store` was blocking efficient history navigation for detail pages. The
    app already appends a cache-busting `_ts` parameter after mutations, so we
    can switch to a softer policy that keeps stale content under control
    without disabling browser back/forward optimizations.
    """
    response.headers["Cache-Control"] = "private, no-cache, max-age=0, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


def safe_back_target(candidate: str | None) -> str | None:
    """Zpetne kompatibilni wrapper nad breadcrumb navigaci."""
    return BreadcrumbNavigation.safe_back_target(candidate)


def request_back_target(request: Request, return_to: str | None = None) -> str:
    """Zpetne kompatibilni wrapper pro vypocet rodicovskeho targetu."""
    return BreadcrumbNavigation.request_back_target(request, return_to)


def sanitize_breadcrumb_label(label: str | None) -> str | None:
    """Zpetne kompatibilni wrapper pro sanitizaci breadcrumb labelu."""
    return BreadcrumbNavigation.sanitize_label(label)


def normalize_breadcrumb_item(url: str | None, label: str | None) -> dict[str, str] | None:
    """Zpetne kompatibilni wrapper pro validni breadcrumb polozku."""
    return BreadcrumbNavigation.normalize_item(url, label)


def decode_breadcrumb_trail(value: str | None) -> list[dict[str, str]]:
    """Zpetne kompatibilni wrapper pro decode breadcrumb trailu."""
    return BreadcrumbNavigation.decode_trail(value)


def encode_breadcrumb_trail(trail: list[dict[str, str]]) -> str:
    """Zpetne kompatibilni wrapper pro encode breadcrumb trailu."""
    return BreadcrumbNavigation.encode_trail(trail)


def split_navigation_query(target: str) -> tuple[list[dict[str, str]], str | None, list[tuple[str, str]]]:
    """Zpetne kompatibilni wrapper pro rozdeleni navigacni query."""
    return BreadcrumbNavigation.split_navigation_query(target)


def strip_navigation_params(target: str) -> str:
    """Zpetne kompatibilni wrapper pro odstraneni breadcrumb parametru."""
    return BreadcrumbNavigation.strip_navigation_params(target)


def strip_ephemeral_params(target: str) -> str:
    """Zpetne kompatibilni wrapper pro odstraneni jen efemernich parametru."""
    return BreadcrumbNavigation.strip_ephemeral_params(target)


def breadcrumb_items_from_target(target: str | None) -> list[dict[str, str]]:
    """Zpetne kompatibilni wrapper pro vypocet breadcrumb polozek."""
    return BreadcrumbNavigation.breadcrumb_items_from_target(target)


def build_breadcrumb_target(
    path: str,
    *,
    trail: list[dict[str, str]] | None = None,
    label: str | None = None,
    season: int | None = None,
    fragment: str | None = None,
) -> str:
    """Zpetne kompatibilni wrapper pro skladani breadcrumb-aware targetu."""
    return BreadcrumbNavigation.build_target(
        path,
        trail=trail,
        label=label,
        season=season,
        fragment=fragment,
    )


def build_breadcrumb_context(
    request: Request,
    current_label: str,
    *,
    return_to: str | None = None,
    default_trail: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Zpetne kompatibilni wrapper pro kompletni breadcrumb kontext stranky."""
    return BreadcrumbNavigation.build_context(
        request,
        current_label,
        return_to=return_to,
        default_trail=default_trail,
    )


def strip_return_to_param(target: str) -> str:
    """Zpetne kompatibilni wrapper pro odstraneni `return_to` parametru."""
    return BreadcrumbNavigation.strip_return_to_param(target)


def detail_return_target(path: str, parent_return_to: str, *, season: int | None = None, fragment: str | None = None) -> str:
    """Zpetne kompatibilni wrapper pro detail target se zachovanym trail."""
    return BreadcrumbNavigation.detail_return_target(
        path,
        parent_return_to,
        season=season,
        fragment=fragment,
    )


def tmdb_asset_url(detail: dict[str, object] | None, asset_kind: str) -> str | None:
    """Najde lokalni URL konkretniho TMDB assetu v detail payloadu."""

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


def group_tmdb_providers(detail: dict[str, object] | None) -> list[dict[str, object]]:
    """Seskupi TMDB providery podle typu nabidky pro sablonu detailu."""

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


__all__ = [
    "RatingUpdateRequest",
    "WatchEventCreateRequest",
    "WatchlistUpdateRequest",
    "background_supervisor",
    "breadcrumb_items_from_target",
    "build_breadcrumb_context",
    "build_breadcrumb_target",
    "card_action_move_targets",
    "count_missing_portraits",
    "detail_return_target",
    "format_czech_datetime",
    "group_tmdb_providers",
    "launch_homepage_warmup",
    "launch_person_presentation_warmup",
    "launch_person_portrait_warmup",
    "present_episode_seasons",
    "present_main_cast",
    "present_person_search_result_card",
    "present_search_result_card",
    "present_title_episodes",
    "present_title_aliases",
    "redirect_back",
    "safe_back_target",
    "selected_panel_page",
    "signal_metadata_pipeline",
    "templates",
    "tmdb_asset_url",
]
