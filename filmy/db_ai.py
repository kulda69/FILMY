"""AI preference, scoring a import facade vyclenena z `filmy.db`."""

from __future__ import annotations

"""AI and suggestion-related DB facade operations extracted from `filmy.db`.

The module intentionally keeps `filmy.db` as the public facade for callers,
while moving cohesive AI/taste/suggestion logic behind a narrower boundary.
Private shared helpers still live in `filmy.db` for now and are imported
through `_db()` so the refactor can proceed in small verified steps.
"""

import importlib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from filmy.genre_scoring import compute_genre_scores
from filmy.runtime_postgres import (
    fetch_ai_noted_title_rows,
    fetch_ai_rated_title_rows,
    fetch_ai_taste_seed_rows,
    fetch_ai_watched_title_rows,
    fetch_catalog_genres as fetch_catalog_genres_postgres,
    fetch_favorite_genres as fetch_favorite_genres_postgres,
    fetch_favorite_traits as fetch_favorite_traits_postgres,
    fetch_genre_score_source_rows as fetch_genre_score_source_rows_postgres,
    fetch_home_suggestion_candidate_rows as fetch_home_suggestion_candidate_rows_postgres,
    fetch_latest_ai_recommendation_for_title as fetch_latest_ai_recommendation_for_title_postgres,
    fetch_latest_genre_scores as fetch_latest_genre_scores_postgres,
    fetch_watch_history as fetch_watch_history_postgres,
    insert_genre_score_snapshot,
    replace_favorite_genres as replace_favorite_genres_postgres,
    replace_favorite_traits as replace_favorite_traits_postgres,
)
from filmy.suggestion_engine import evaluate_new_imdb_candidate, evaluate_trait_candidate


def _db():
    """Nacti `filmy.db` az pri behu, aby fasada zustala bez cyklickeho importu."""

    return importlib.import_module("filmy.db")


def get_ai_taste_seed(source_list: str = "kouknout-znovu", limit: int = 50) -> dict[str, Any]:
    """Return read-only taste examples for an external AI recommendation layer."""
    safe_limit = max(1, min(int(limit), 200))
    return fetch_ai_taste_seed_rows(source_list=source_list, limit=safe_limit)


def get_ai_taste_inputs(limit_per_list: int = 25) -> dict[str, Any]:
    """Return AI taste inputs grouped by user-list AI role."""
    safe_limit = max(1, min(int(limit_per_list), 100))
    included_roles = ("strong_positive", "interested_owned", "interested_planned", "in_progress", "negative")
    excluded_roles = ("external_suggestion", "ignore")
    role_labels = {
        "strong_positive": "Silne pozitivni priklady.",
        "interested_owned": "Tituly, ktere Jiri ma nebo s nimi udelal rucni praci.",
        "interested_planned": "Tituly, ktere chce videt nebo stoji za pozornost.",
        "in_progress": "Rozkoukane tituly; opatrny signal zajmu.",
        "negative": "Negativni priklady nebo veci, ktere nemaji podporovat podobne pozitivni tipy.",
        "external_suggestion": "Vystup z AI; nepouzivat jako vstup.",
        "ignore": "Neutralni nebo technicke seznamy; nepouzivat jako vstup.",
    }
    db = _db()
    visible_lists = db.get_local_library_status().get("visible_lists") or []
    grouped: dict[str, list[dict[str, Any]]] = {role: [] for role in included_roles}
    excluded_sources: list[dict[str, Any]] = []
    for source_list in visible_lists:
        role = str(source_list.get("ai_input_role") or "ignore")
        source_summary = {
            "id": source_list.get("id"),
            "slug": source_list.get("slug"),
            "name": source_list.get("name"),
            "description": source_list.get("description"),
            "list_kind": source_list.get("list_kind"),
            "ai_input_role": role,
            "item_count": source_list.get("item_count"),
        }
        if role in excluded_roles or role not in grouped:
            excluded_sources.append(source_summary)
            continue
        seed = get_ai_taste_seed(source_list=str(source_list["id"]), limit=safe_limit)
        grouped[role].append(
            {
                "source_list": seed.get("source_list") or source_summary,
                "role_description": role_labels[role],
                "limit": seed.get("limit"),
                "items": seed.get("items") or [],
            }
        )
    return {
        "contract_version": 1,
        "limit_per_list": safe_limit,
        "included_roles": list(included_roles),
        "excluded_roles": list(excluded_roles),
        "role_descriptions": role_labels,
        "groups": grouped,
        "excluded_sources": excluded_sources,
        "usage_notes": [
            "`external_suggestion` a `ignore` se nikdy neposilaji jako vstup pro AI tipy.",
            "`negative` je vstup pro varovani a vymezovani vkusu, ne pozitivni seed.",
            "Polozky ve skupinach maji stejny tvar jako `/api/ai/taste-seed` vcetne people affinity a title role signals.",
        ],
    }


def get_ai_rated_titles(*, min_user_rating: int = 8, limit: int = 50, title_type: str | None = None) -> dict[str, Any]:
    """Return locally rated titles for an external AI recommendation layer."""
    safe_rating = max(1, min(int(min_user_rating), 10))
    safe_limit = max(1, min(int(limit), 200))
    cleaned_title_type = (title_type or "").strip() or None
    return fetch_ai_rated_title_rows(min_user_rating=safe_rating, limit=safe_limit, title_type=cleaned_title_type)


def get_ai_noted_titles(*, notes: str = "any", min_user_rating: int | None = None, limit: int = 50) -> dict[str, Any]:
    """Return titles with written local notes for an external AI layer."""
    cleaned_notes = (notes or "any").strip().lower()
    if cleaned_notes not in {"any", "liked", "disliked"}:
        cleaned_notes = "any"
    safe_min_rating = None
    if min_user_rating is not None:
        safe_min_rating = max(1, min(int(min_user_rating), 10))
    safe_limit = max(1, min(int(limit), 200))
    return fetch_ai_noted_title_rows(notes=cleaned_notes, min_user_rating=safe_min_rating, limit=safe_limit)


def get_ai_watched_titles(*, include_rated: bool = True, include_negative: bool = True) -> dict[str, Any]:
    """Return a complete hard exclusion list for external AI recommendations."""
    return fetch_ai_watched_title_rows(include_rated=include_rated, include_negative=include_negative)


def import_ai_recommendations_file(path: str | Path) -> dict[str, Any]:
    """Importuj jeden stabilni JSON soubor AI doporuceni do PostgreSQL."""

    from filmy.ai_recommendations import import_ai_recommendations_file as _impl

    return _impl(path)


def list_ai_recommendation_files() -> list[dict[str, Any]]:
    """Vrat prehled importovatelnych JSON souboru ve `filmy_output`."""

    from filmy.ai_recommendations import list_ai_recommendation_files as _impl

    return _impl()


def delete_ai_recommendation_file(filename: str) -> dict[str, Any]:
    """Smaz jeden importni JSON soubor AI doporuceni ze stabilniho adresare."""

    from filmy.ai_recommendations import delete_ai_recommendation_file as _impl

    return _impl(filename)


def get_latest_ai_recommendation_for_title(tconst: str) -> dict[str, Any] | None:
    """Vrat posledni importovane AI doporuceni navazane na dany titul."""

    return fetch_latest_ai_recommendation_for_title_postgres(tconst)


def get_ai_context() -> dict[str, Any]:
    """Return stable local taste context for an external AI recommendation layer."""
    return {
        "contract_version": 1,
        "rating_scales": {
            "user_rating": {
                "min": 1,
                "max": 10,
                "type": "integer",
                "description": "Jiriho lokalni hodnoceni titulu; vyssi cislo znamena silnejsi oblibu.",
            },
            "person_affinity_rating": {
                "min": 0,
                "max": 10,
                "type": "integer",
                "description": "Jiriho oblibenost osoby; 0 znamena bez pozitivni affinity.",
            },
            "title_role_signal_strength": {
                "min": 0,
                "max": 10,
                "type": "integer",
                "description": "Sila konkretniho signalu role/postavy v jednom titulu; neni to celkovy rating titulu ani affinity k herci.",
            },
            "imdb_rating": {
                "min": 0,
                "max": 10,
                "type": "decimal",
                "description": "Externi IMDb rating; neni to Jiriho lokalni hodnoceni.",
            },
            "favorite_preference_rank": {
                "min": 1,
                "max": 10,
                "type": "integer",
                "description": "Rucni priorita favorite genres/traits; nizsi cislo znamena silnejsi preferenci, null znamena nehodnoceno.",
            },
        },
        "favorite_genres": get_favorite_genres(active_only=False),
        "favorite_traits": get_favorite_traits(active_only=False),
        "score_signal_notes": {
            "genre_score_signals": "Lokalni preference zanru podle historie, ratingu a dalsich signalu.",
            "actor_affinity_rating": "Souhrnny signal oblibenosti hodnocenych hercu navazanych na titul.",
            "people_affinity": "Konkretni osoby z titulu, ktere maji rucni affinity rating; kontrakt pro navazujici rozsireni taste-seed.",
            "title_role_signals": "Konkretni role/postavy v titulu, ktere Jiri oznacil jako pozitivni, negativni nebo smisene signaly. Tento signal je oddeleny od ratingu titulu i affinity k herci.",
        },
        "title_role_signal_definitions": {
            "signal_types": {
                "character": "Postava jako celek.",
                "dialogue": "Dialogy, hlas, slovni projev nebo zpusob komunikace postavy.",
                "behavior": "Chovani, rozhodovani a reakce postavy.",
                "relationship_dynamic": "Vztahova dynamika postavy s ostatnimi.",
                "performance": "Herecke provedeni v konkretni roli.",
                "visual_appeal": "Vzhled, styl nebo vizualni pusobeni role v danem titulu.",
                "attraction": "Pritazlivost nebo charisma role v danem titulu a dobe.",
                "other": "Jiny titulove vazany signal role/postavy.",
            },
            "polarities": {
                "positive": "Signal, ktery muze podporit podobna doporuceni.",
                "negative": "Signal, ktery muze podobna doporuceni oslabit nebo vyloucit.",
                "mixed": "Signal je dulezity, ale neni jednoznacne pozitivni ani negativni.",
            },
            "notes": "Poznamka je textovy kontext pro cloveka a pozdeji AI interpretaci; nema se sama prevadet na ciselne skore bez opatrnosti.",
        },
        "usage_notes": [
            "Endpoint je read-only a nevola externi AI ani online katalogy.",
            "Navazujici AI projekt ho ma volat jako obecny kontext pred praci s konkretnimi tituly.",
            "Favorite genres a favorite traits se vraci cele, vcetne neaktivnich polozek.",
            "Title role signals mohou byt silne i u titulu s nizkym celkovym hodnocenim; napr. nebrat cely serial jako oblibeny, ale brat konkretni postavu/dialogy/chovani jako vzor.",
        ],
    }


def get_ai_scoring_explainer() -> dict[str, Any]:
    """Explain local scoring semantics for an external AI recommendation layer."""
    return {
        "contract_version": 1,
        "score_scope": "default",
        "status": {
            "implemented_scoring": "Aktualni lokalni scoring pocita hlavne zanrove a titulove signaly z historie, lokalnich ratingu, watch signalu, people affinity a rucnich favorite genres.",
            "role_signals_status": "Title role signals jsou nova samostatna vrstva. Zatim se nepositaji do genre_score_signals ani final_score.",
            "future_role_signal_task": "Pozdeji navrhnout samostatnou scoring vetev pro role/postava signaly, napr. role_signal_score nebo character_preference_signals. Nezvedat tim automaticky celkove hodnoceni titulu.",
        },
        "principles": [
            "Lokalni score je pomocny signal pro razeni a vysvetleni, ne definitivni pravda.",
            "Jiriho lokalni rating ma vyssi vyznam nez externi IMDb rating.",
            "IMDb rating je verejny externi signal kvality/popularity, ne osobni preference.",
            "People affinity je osobni signal k osobe, ne obecna popularita herce.",
            "Title role signals mohou byt silne i u titulu s nizkym celkovym ratingem; cist je samostatne.",
            "Negativni seznamy a negativni signaly maji pomahat vymezit vkus, ne mechanicky mazat vsechny podobne tituly.",
        ],
        "signals": {
            "final_score": {
                "meaning": "Normalizovane lokalni skore kandidata nebo zanru v danem score scope.",
                "ai_usage": "Pouzit jako podpurny signal razeni, ne jako jediny duvod doporuceni.",
            },
            "watch_signal_score": {
                "meaning": "Signal odvozeny z historie sledovani a opakovanych lokalnich interakci.",
                "ai_usage": "Ukazuje, ze Jiri s podobnym obsahem realne travil cas.",
            },
            "rating_signal_score": {
                "meaning": "Signal odvozeny z Jiriho lokalnich ratingu.",
                "ai_usage": "Silnejsi osobni signal nez IMDb rating; stale ho cist spolecne se slovnimi poznamkami.",
            },
            "actor_affinity_score": {
                "meaning": "Signal odvozeny z oblibenosti osob navazanych na titul.",
                "ai_usage": "Pouzit opatrne: osoba neni totez jako role v konkretnim titulu.",
            },
            "genre_score_signals": {
                "meaning": "Zanrove signaly, ktere ukazuji, proc se nejaky zanr nebo titul muze potkavat s lokalnim vkusem.",
                "ai_usage": "Pouzit jako kontext k zanrum, ne jako samostatne vysvetleni celeho vkusu.",
            },
            "favorite_genres": {
                "meaning": "Rucni zanrove preference; nizsi preference_rank znamena silnejsi preferenci.",
                "ai_usage": "Cist jako explicitni korekci automatickych signalu.",
            },
            "favorite_traits": {
                "meaning": "Rucni jemne preference typu slow-burn, dialogue-driven nebo atmospheric.",
                "ai_usage": "Zatim hlavne interpretacni kontext; nemusi byt plne zapocitany ve vsech scoring vypoctech.",
            },
            "people_affinity": {
                "meaning": "Konkretni osoby v titulu, ktere maji rucni affinity rating.",
                "ai_usage": "Cist jako osobni vztah k osobe, oddelene od role/postavy.",
            },
            "title_role_signals": {
                "meaning": "Konkretni signaly role/postavy v jednom titulu: postava, dialogy, chovani, vztahova dynamika, provedeni, vzhled nebo pritazlivost.",
                "ai_usage": "Cist samostatne mimo final_score. Priklad: nizky rating celeho serialu muze koexistovat se silnym pozitivnim signalem jedne postavy.",
                "current_scoring_inclusion": False,
            },
        },
        "known_limitations": [
            "Title role signals zatim nejsou zapocitane do final_score ani genre_score_signals.",
            "Favorite traits jsou sbirane jako jemny kontext; jejich vliv na scoring se muze dal menit.",
            "Bez slovnich poznamek muze byt duvod ratingu nejasny.",
            "Seznamove role ai_input_role rikaji vyznam zdroje, ale samy o sobe nejsou detailni vysvetleni vkusu.",
        ],
        "recommended_ai_reading_order": [
            "Nejdrive nacist /api/ai/context kvuli skalam a definicim.",
            "Potom nacist /api/ai/scoring-explainer kvuli vyznamu score poli.",
            "Potom nacist /api/ai/taste-inputs pro sirsi vstupy podle ai_input_role.",
            "Podle potreby doplnit /api/ai/rated-titles pro silne lokalne hodnocene tituly.",
            "Pri interpretaci kazde polozky kombinovat user_rating, liked/disliked notes, people_affinity, title_role_signals a genre_score_signals.",
        ],
    }


def get_favorite_genres(active_only: bool = True) -> list[dict[str, Any]]:
    """Return locally curated favorite genres ordered by preference and weight."""
    return fetch_favorite_genres_postgres(active_only=active_only)


def get_catalog_genres() -> list[dict[str, Any]]:
    """Return all distinct catalog genres with how many titles use them."""
    return fetch_catalog_genres_postgres()


def get_favorite_traits(active_only: bool = True) -> list[dict[str, Any]]:
    """Return locally curated favorite traits ordered by preference and weight."""
    return fetch_favorite_traits_postgres(active_only=active_only)


def get_genre_score_source_rows() -> list[dict[str, Any]]:
    """Return title-level behavioral inputs for genre scoring."""
    return fetch_genre_score_source_rows_postgres()


def get_home_suggestion_sections(*, limit_per_section: int | None = 4) -> dict[str, Any]:
    """Build the two homepage suggestion buckets from local metadata only."""
    db = _db()
    active_traits = [item for item in fetch_favorite_traits_postgres(active_only=True) if item.get("preference_rank") is not None]
    latest_genre_scores = fetch_latest_genre_scores_postgres(score_scope="default", limit=None)
    genre_score_lookup = {
        str(item.get("genre")): float(item.get("normalized_score") or 0.0)
        for item in (latest_genre_scores or {}).get("items") or []
        if item.get("genre")
    }
    candidate_rows = db._get_home_suggestion_candidate_rows(None)
    trait_matches: list[dict[str, Any]] = []
    new_on_imdb: list[dict[str, Any]] = []
    for row in candidate_rows:
        trait_eval = evaluate_trait_candidate(row, active_traits, genre_score_lookup)
        if trait_eval["matched_traits"]:
            trait_matches.append({**row, **trait_eval})
        new_eval = evaluate_new_imdb_candidate(row, active_traits, genre_score_lookup)
        if new_eval["is_recent"] and new_eval["imdb_quality_score"] >= 0.35 and (
            int(row.get("cz_provider_count") or 0) > 0
            or new_eval["trait_score"] >= 0.2
            or new_eval["actor_affinity_score"] >= 0.15
        ):
            new_on_imdb.append({**row, **new_eval})
    trait_matches.sort(
        key=lambda item: (
            -float(item["total_score"]),
            -len(item.get("matched_traits") or []),
            -int(item.get("num_votes") or 0),
            -float(item.get("average_rating") or 0.0),
            -int(item.get("start_year") or 0),
            str(item.get("primary_title") or ""),
        )
    )
    new_on_imdb.sort(
        key=lambda item: (
            -float(item["total_score"]),
            -float(item.get("freshness_score") or 0.0),
            -int(item.get("num_votes") or 0),
            -float(item.get("average_rating") or 0.0),
            -int(item.get("start_year") or 0),
            str(item.get("primary_title") or ""),
        )
    )
    trait_items = trait_matches if limit_per_section is None else trait_matches[:limit_per_section]
    new_items = new_on_imdb if limit_per_section is None else new_on_imdb[:limit_per_section]
    return {"trait_matches": trait_items, "new_on_imdb": new_items, "active_traits": active_traits}


def get_genre_suggestion_candidates(genre: str, *, limit: int | None = 24) -> dict[str, Any]:
    """Return current unwatched recommendation candidates for one genre."""
    db = _db()
    resolved_genre = genre.strip()
    active_traits = [item for item in fetch_favorite_traits_postgres(active_only=True) if item.get("preference_rank") is not None]
    latest_genre_scores = fetch_latest_genre_scores_postgres(score_scope="default", limit=None)
    genre_score_lookup = {
        str(item.get("genre")): float(item.get("normalized_score") or 0.0)
        for item in (latest_genre_scores or {}).get("items") or []
        if item.get("genre")
    }
    candidate_rows = db._get_home_suggestion_candidate_rows(None)
    items: list[dict[str, Any]] = []
    for row in candidate_rows:
        genres = [str(item) for item in row.get("genres") or []]
        if resolved_genre not in genres:
            continue
        trait_eval = evaluate_trait_candidate(row, active_traits, genre_score_lookup)
        if not (trait_eval["trait_score"] >= 0.2 or trait_eval["actor_affinity_score"] >= 0.15):
            continue
        candidate_score = min(1.0, float(trait_eval["total_score"]) + 0.18 * float(trait_eval["genre_alignment_score"]))
        items.append({**row, **trait_eval, "candidate_score": round(candidate_score, 4)})
    items.sort(
        key=lambda item: (
            -float(item["candidate_score"]),
            -len(item.get("matched_traits") or []),
            -int(item.get("cz_provider_count") or 0),
            -float(item.get("average_rating") or 0.0),
            -int(item.get("num_votes") or 0),
            -int(item.get("start_year") or 0),
            str(item.get("primary_title") or ""),
        )
    )
    if limit is not None:
        items = items[:limit]
    return {"genre": resolved_genre, "items": items, "active_traits": active_traits}


def replace_favorite_genres(
    genres: Sequence[str | dict[str, Any]],
    *,
    source_origin: str = "local_app",
    source_ref: str | None = None,
    archive_missing: bool = True,
) -> dict[str, Any]:
    """Replace or refresh the curated favorite genre list."""
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(genres, start=1):
        if isinstance(item, str):
            genre = item.strip()
            payload = {"genre": genre, "weight": 1.0, "preference_rank": index, "notes": None, "is_active": True}
        else:
            genre = str(item.get("genre") or "").strip()
            payload = {
                "genre": genre,
                "weight": float(item.get("weight", 1.0)),
                "preference_rank": item.get("preference_rank", index),
                "notes": item.get("notes"),
                "is_active": bool(item.get("is_active", True)),
            }
        if not genre:
            raise ValueError("Kazdy zanr musi mit neprazdny nazev.")
        normalized.append(payload)
    db = _db()
    now = db._now_iso()
    normalized_genres = {item["genre"] for item in normalized}
    replace_favorite_genres_postgres(
        items=normalized,
        source_origin=source_origin,
        source_ref=source_ref,
        archive_missing=archive_missing,
        now=now,
    )
    return {"count": len(normalized), "genres": sorted(normalized_genres), "updated_at": now}


def replace_favorite_traits(
    traits: Sequence[str | dict[str, Any]],
    *,
    source_origin: str = "local_app",
    source_ref: str | None = None,
    archive_missing: bool = True,
) -> dict[str, Any]:
    """Replace or refresh the curated favorite trait list."""
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(traits, start=1):
        if isinstance(item, str):
            trait = item.strip()
            payload = {"trait": trait, "weight": 1.0, "preference_rank": index, "notes": None, "is_active": True}
        else:
            trait = str(item.get("trait") or "").strip()
            payload = {
                "trait": trait,
                "weight": float(item.get("weight", 1.0)),
                "preference_rank": item.get("preference_rank", index),
                "notes": item.get("notes"),
                "is_active": bool(item.get("is_active", True)),
            }
        if not trait:
            raise ValueError("Kazdy trait musi mit neprazdny nazev.")
        normalized.append(payload)
    db = _db()
    now = db._now_iso()
    normalized_traits = {item["trait"] for item in normalized}
    replace_favorite_traits_postgres(
        items=normalized,
        source_origin=source_origin,
        source_ref=source_ref,
        archive_missing=archive_missing,
        now=now,
    )
    return {"count": len(normalized), "traits": sorted(normalized_traits), "updated_at": now}


def record_genre_score_snapshot(
    scores: Sequence[dict[str, Any]],
    *,
    score_scope: str = "default",
    algorithm_version: str | None = None,
    source_origin: str = "local_app",
    source_ref: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Persist one genre-score snapshot run with per-genre score breakdown."""
    if not scores:
        raise ValueError("Je potreba dodat alespon jeden zanr se score.")
    db = _db()
    snapshot_time = generated_at or db._now_iso()
    datetime.fromisoformat(snapshot_time.replace("Z", "+00:00"))
    prepared_rows: list[dict[str, Any]] = []
    for index, item in enumerate(scores, start=1):
        genre = str(item.get("genre") or "").strip()
        if not genre:
            raise ValueError("Kazdy zaznam genre_scores musi mit genre.")
        if item.get("final_score") is None:
            raise ValueError(f"Zaznam pro zanr '{genre}' nema final_score.")
        prepared_rows.append(
            {
                "id": str(uuid.uuid4()),
                "genre": genre,
                "titles_considered": item.get("titles_considered"),
                "watched_titles_considered": item.get("watched_titles_considered"),
                "rated_titles_considered": item.get("rated_titles_considered"),
                "contributing_titles_json": db._dumps_json_or_none(item.get("contributing_titles")),
                "excluded_titles_json": db._dumps_json_or_none(item.get("excluded_titles")),
                "favorite_genre_weight": item.get("favorite_genre_weight"),
                "preference_overlap_score": item.get("preference_overlap_score"),
                "preference_alignment_score": item.get("preference_alignment_score"),
                "affinity_score": item.get("affinity_score"),
                "rating_signal_score": item.get("rating_signal_score"),
                "watch_signal_score": item.get("watch_signal_score"),
                "recency_score": item.get("recency_score"),
                "actor_affinity_score": item.get("actor_affinity_score"),
                "frequency_score": item.get("frequency_score"),
                "consistency_score": item.get("consistency_score"),
                "novelty_score": item.get("novelty_score"),
                "confidence_score": item.get("confidence_score"),
                "manual_adjustment_score": item.get("manual_adjustment_score"),
                "final_score": item.get("final_score"),
                "normalized_score": item.get("normalized_score"),
                "rank_in_run": item.get("rank_in_run", index),
                "metrics_json": db._dumps_json_or_none(item.get("metrics")),
                "explanation": item.get("explanation"),
            }
        )
    return insert_genre_score_snapshot(
        rows=[
            {
                "id": item["id"],
                "genre": item["genre"],
                "generated_at": snapshot_time,
                "algorithm_version": algorithm_version,
                "score_scope": score_scope,
                "source_origin": source_origin,
                "source_ref": source_ref,
                "titles_considered": item["titles_considered"],
                "watched_titles_considered": item["watched_titles_considered"],
                "rated_titles_considered": item["rated_titles_considered"],
                "contributing_titles_json": item["contributing_titles_json"],
                "excluded_titles_json": item["excluded_titles_json"],
                "favorite_genre_weight": item["favorite_genre_weight"],
                "preference_overlap_score": item["preference_overlap_score"],
                "preference_alignment_score": item["preference_alignment_score"],
                "affinity_score": item["affinity_score"],
                "rating_signal_score": item["rating_signal_score"],
                "watch_signal_score": item["watch_signal_score"],
                "recency_score": item["recency_score"],
                "actor_affinity_score": item["actor_affinity_score"],
                "frequency_score": item["frequency_score"],
                "consistency_score": item["consistency_score"],
                "novelty_score": item["novelty_score"],
                "confidence_score": item["confidence_score"],
                "manual_adjustment_score": item["manual_adjustment_score"],
                "final_score": item["final_score"],
                "normalized_score": item["normalized_score"],
                "rank_in_run": item["rank_in_run"],
                "metrics_json": item["metrics_json"],
                "explanation": item["explanation"],
                "created_at": snapshot_time,
            }
            for item in prepared_rows
        ]
    )


def compute_and_record_genre_scores(
    *,
    score_scope: str = "default",
    algorithm_version: str | None = None,
    source_origin: str = "local_app",
    source_ref: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Compute one genre-score snapshot from local data and store it."""
    db = _db()
    snapshot_time = generated_at or db._now_iso()
    title_rows = fetch_genre_score_source_rows_postgres()
    favorite_genres = fetch_favorite_genres_postgres(active_only=True)
    catalog_genres = fetch_catalog_genres_postgres()
    scores = compute_genre_scores(title_rows, favorite_genres, catalog_genres, generated_at=snapshot_time)
    if not scores:
        raise ValueError("Pro vypocet genre_scores zatim nejsou zadna lokalni data.")
    resolved_algorithm_version = algorithm_version or ((scores[0].get("metrics") or {}).get("algorithm_version") if scores else None)
    summary = record_genre_score_snapshot(
        scores,
        score_scope=score_scope,
        algorithm_version=resolved_algorithm_version,
        source_origin=source_origin,
        source_ref=source_ref,
        generated_at=snapshot_time,
    )
    top_rows = fetch_latest_genre_scores_postgres(score_scope=score_scope, limit=10)
    return {
        **summary,
        "titles_considered": len(title_rows),
        "favorite_genres_count": len(favorite_genres),
        "top_genres": top_rows["items"] if top_rows else [],
    }


def get_latest_genre_scores(*, score_scope: str | None = None, limit: int | None = None) -> dict[str, Any] | None:
    """Load the newest genre-score snapshot, optionally within one scope."""
    return fetch_latest_genre_scores_postgres(score_scope=score_scope, limit=limit)


def get_watch_history(limit: int = 100, source: str | None = None) -> list[dict[str, Any]]:
    """Vrat posledni udalosti sledovani s volitelnym filtrem podle zdroje."""

    return fetch_watch_history_postgres(limit=limit, source=source)
