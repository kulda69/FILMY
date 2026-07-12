from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Sequence


ALGORITHM_VERSION = "genre-v2"


def compute_genre_scores(
    title_rows: Sequence[dict[str, Any]],
    favorite_genres: Sequence[dict[str, Any]],
    catalog_genres: Sequence[dict[str, Any]],
    *,
    generated_at: str | None = None,
) -> list[dict[str, Any]]:
    """Spocte jeden snapshot zanrovych preferenci z lokalniho chovani uzivatele.

    Ocekavane vstupy:
    - `title_rows`: jeden radek pro film nebo serial, kde jsou uz sloucena
      lokalni data o chovani. Typicky `rating`, `watch_count`,
      `last_watched_at` a seznam IMDb zanru.
    - `favorite_genres`: rucne zadane oblibene zanry z administracni stranky.
      Pouzivaji se jen aktivni zaznamy.
    - `catalog_genres`: seznam vsech znamych zanru s poctem titulu v katalogu.
      To neslouzi jako prime preference, ale jen jako kontext pro to, jestli je
      zanr bezny nebo spis vzacny.

    Algoritmus je zamerne jednoduchy a obhajitelny. Nesnazi se hadat skryty
    vkus pres embeddingy nebo cizi uzivatele. Jen kombinuje lokalni signaly,
    ktere uz v appce mame, a z nich vytvori jeden snapshot pro kazdy zanr.

    Signaly zapisovane do `genre_scores`:
    - `affinity_score`:
      Hlavni souhrnny signal pro dany zanr. Nejdriv se pocita na urovni
      jednotlivych titulu jako `title_affinity`, kde se micha hodnoceni,
      tendence ke znovusledovani, recency a jemne i oblibenost hercu. Na
      urovni zanru je to pak vazeny prumer techto hodnot, prevedeny do
      intervalu `0..1`.
    - `rating_signal_score`:
      Primy signal z lokalnich hodnoceni. Stred je zhruba kolem `5.5/10`, takze
      vyssi hodnoceni tlaci signal do plusu a slaba hodnoceni do minusu. Je to
      nejsilnejsi aktualni odpoved na otazku "tohle se mi libilo / nelibilo".
    - `watch_signal_score`:
      Signal z toho, kolikrat byl titul viden. Pouziva logaritmickou krivku,
      takze prvni rewatch ma vetsi vyznam a desaty uz score neodpali do vesmiru.
      Rewatch se bere jako slabsi pozitivni preference.
    - `recency_score`:
      Signal cerstvosti odvozeny z `last_watched_at`. Novejsi aktivita ma vyssi
      vahu a casem exponencialne slabi. Tim se do vysledku trochu promita
      aktualni nalada, ale nepremaze starsi vkus.
    - `actor_affinity_score`:
      Slaby pomocny signal z lokalne hodnocenych hercu. Bere se jen hlavni
      obsazeni (`cast`) a jen osoby, ktere uzivatel opravdu ohodnotil.
      Smysl je jednoduchy: pokud se v nejakem zanru opakovane objevuje obsazeni,
      ktere mam rad, ma ten zanr dostat mirny bonus. Tento signal schvalne
      nema vahu srovnatelnou s ratingem titulu, aby oblibeny herec nepretlacil
      film, ktery me realne nebavil.
    - `frequency_score`:
      Hruby signal hustoty dukazu. Nejde o to, kolikrat byl prehrany jeden
      konkretni titul, ale jak casto se cely zanr objevuje v historii sledovani.
    - `consistency_score`:
      Rika, jestli jsou dukazy pro zanr konzistentni. Kdyz ma zanr vetsinou
      dobra hodnoceni, score roste. Kdyz ma hodne slabych hodnoceni, klesa.
      Pokud ratings chybi, pouzije se jako nahrada podil titulu s kladnou
      `title_affinity`.
    - `confidence_score`:
      Mira duvery ve vysledek podle toho, kolik realnych dukazu vubec mame.
      Rated tituly pocitaji vic nez obycejne watch events. Neni to preference
      sama o sobe, ale informace, jak moc mame ostatnim signalum verit.
    - `preference_overlap_score`:
      Rucni preference z `favorite_genres`. Lepsi priorita dava silnejsi boost.
      Volitelna manualni `weight` to jeste jemne upravi. Pokud zanr neni mezi
      rucne zadanymi oblibenymi, signal je nulovy.
    - `preference_alignment_score`:
      Smes skutecne pozorovane affinity a rucni preference. Rika, jak moc se
      explicitni preference a realne chovani potkavaji stejnym smerem.
    - `novelty_score`:
      Malinky bonus za relativni vzacnost zanru v celem lokalnim katalogu.
      Vzacnejsi zanry dostanou trochu prostoru navic, masove zanry mene. Je to
      schvalne slaby signal.
    - `manual_adjustment_score`:
      Drobny bonus odvozeny primo z rucnich preferenci. Neni to dalsi zdroj
      pravdy, jen jemne postrceni, aby uzivatelem vybrane zanry nezanikly v
      sumu historie.
    - `final_score`:
      Finalni score `0..100`, podle ktereho se zanry radi v jednom snapshotu.
      Je to vazena kombinace vsech signalu vyse. Dobra hodnoceni pomahaji,
      vyrazne negativni hodnoceni skodi a manualni preference slouzi jen jako
      sekundarni korekce.

    Doplňková data, ktera se ukladaji spolu se score:
    - `contributing_titles`: nejsilnejsi pozitivni tituly pro dany zanr.
    - `excluded_titles`: nejslabsi tituly pro dany zanr, uzitecne pro debug.
    - `metrics`: kratky technicky kontext, napr. verze algoritmu a velikost
      katalogu v okamziku vypoctu.
    - `explanation`: kratka lidsky citelna veta, proc zanr dostal dane score.

    Aktualni navrhove predpoklady:
    - ratingy jsou nejsilnejsi preference signal
    - rewatches maji smysl, ale mensi nez ratingy
    - recentni chovani ma hrat roli, ale ma postupne slabnout
    - oblibenost hercu ma fungovat jen jako jemna korekce
    - rucne zadane oblibene zanry maji vysledek ovlivnit, ale nemaji plne
      prepsat protichudnou lokalni historii
    """
    observed_at = _coerce_datetime(generated_at) if generated_at else datetime.now(UTC)
    catalog_counts = {
        str(item.get("genre")): int(item.get("title_count") or 0)
        for item in catalog_genres
        if item.get("genre")
    }
    max_catalog_count = max(catalog_counts.values(), default=1)
    favorite_lookup = _build_favorite_lookup(favorite_genres)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in title_rows:
        genres = [genre for genre in row.get("genres") or [] if genre]
        if not genres:
            continue

        rating_signal = _rating_signal(row.get("rating"))
        watch_signal = _watch_signal(row.get("watch_count"))
        recency_signal = _recency_signal(row.get("last_watched_at"), observed_at)
        actor_affinity_signal = _actor_affinity_signal(row.get("actor_affinity_rating"))
        title_affinity = _clamp(
            (0.56 * rating_signal)
            + (0.20 * watch_signal)
            + (0.14 * recency_signal)
            + (0.10 * actor_affinity_signal),
            -1.0,
            1.0,
        )
        title_weight = 1.0
        if row.get("rating") is not None:
            title_weight += 0.75
        title_weight += 0.20 * min(int(row.get("watch_count") or 0), 3)
        if row.get("actor_affinity_rating") is not None:
            title_weight += 0.15

        prepared = {
            "tconst": row.get("tconst"),
            "title": row.get("title"),
            "year": row.get("year"),
            "rating": row.get("rating"),
            "watch_count": int(row.get("watch_count") or 0),
            "last_watched_at": row.get("last_watched_at"),
            "actor_affinity_rating": row.get("actor_affinity_rating"),
            "actor_affinity_signal": actor_affinity_signal,
            "rating_signal": rating_signal,
            "watch_signal": watch_signal,
            "recency_signal": recency_signal,
            "title_affinity": title_affinity,
            "title_weight": title_weight,
        }
        for genre in genres:
            grouped.setdefault(genre, []).append(prepared)

    genre_scores: list[dict[str, Any]] = []
    max_favorite_weight = max((float(item.get("weight") or 1.0) for item in favorite_genres), default=1.0)
    for genre, items in grouped.items():
        weights = [item["title_weight"] for item in items]
        affinity_raw = _weighted_average([item["title_affinity"] for item in items], weights)
        affinity_score = _clamp((affinity_raw + 1.0) / 2.0, 0.0, 1.0)
        rating_signal_score = _weighted_average([item["rating_signal"] for item in items], weights)
        watch_signal_score = _weighted_average([item["watch_signal"] for item in items], weights)
        recency_score = _weighted_average([item["recency_signal"] for item in items], weights)
        actor_affinity_score = _actor_affinity_score(items)
        frequency_score = _clamp(sum(item["watch_count"] for item in items) / max(len(items) * 2.5, 1.0), 0.0, 1.0)
        consistency_score = _consistency_score(items)
        confidence_score = _confidence_score(items)

        favorite = favorite_lookup.get(genre)
        preference_overlap_score = _favorite_overlap_score(favorite, max_favorite_weight=max_favorite_weight)
        preference_alignment_score = _clamp((0.75 * affinity_score) + (0.25 * preference_overlap_score), 0.0, 1.0)
        novelty_score = _novelty_score(catalog_counts.get(genre, 0), max_catalog_count=max_catalog_count)
        manual_adjustment_score = round(preference_overlap_score * 0.20, 4)

        positive_rating_bonus = max(rating_signal_score, 0.0)
        negative_rating_penalty = max(-rating_signal_score, 0.0)
        final_normalized = _clamp(
            (0.26 * affinity_score)
            + (0.16 * positive_rating_bonus)
            + (0.13 * watch_signal_score)
            + (0.10 * recency_score)
            + (0.07 * actor_affinity_score)
            + (0.09 * frequency_score)
            + (0.08 * consistency_score)
            + (0.05 * confidence_score)
            + (0.05 * preference_alignment_score)
            + (0.03 * novelty_score)
            + manual_adjustment_score
            - (0.18 * negative_rating_penalty),
            0.0,
            1.0,
        )
        final_score = round(final_normalized * 100.0, 4)

        contributing_titles = _top_titles(items, limit=10, reverse=True)
        excluded_titles = _top_titles([item for item in items if item["title_affinity"] < 0], limit=5, reverse=False)
        explanation = _build_explanation(
            affinity_score=affinity_score,
            rating_signal_score=rating_signal_score,
            watch_signal_score=watch_signal_score,
            recency_score=recency_score,
            actor_affinity_score=actor_affinity_score,
            preference_overlap_score=preference_overlap_score,
            titles_considered=len(items),
        )

        genre_scores.append(
            {
                "genre": genre,
                "titles_considered": len(items),
                "watched_titles_considered": sum(1 for item in items if item["watch_count"] > 0),
                "rated_titles_considered": sum(1 for item in items if item["rating"] is not None),
                "contributing_titles": contributing_titles,
                "excluded_titles": excluded_titles,
                "favorite_genre_weight": float(favorite.get("weight") or 1.0) if favorite else None,
                "preference_overlap_score": round(preference_overlap_score, 4),
                "preference_alignment_score": round(preference_alignment_score, 4),
                "affinity_score": round(affinity_score, 4),
                "rating_signal_score": round(rating_signal_score, 4),
                "watch_signal_score": round(watch_signal_score, 4),
                "recency_score": round(recency_score, 4),
                "actor_affinity_score": round(actor_affinity_score, 4),
                "frequency_score": round(frequency_score, 4),
                "consistency_score": round(consistency_score, 4),
                "novelty_score": round(novelty_score, 4),
                "confidence_score": round(confidence_score, 4),
                "manual_adjustment_score": round(manual_adjustment_score, 4),
                "final_score": final_score,
                "normalized_score": round(final_normalized, 4),
                "metrics": {
                    "algorithm_version": ALGORITHM_VERSION,
                    "catalog_title_count": catalog_counts.get(genre, 0),
                    "max_catalog_title_count": max_catalog_count,
                    "observed_at": observed_at.isoformat(),
                },
                "explanation": explanation,
            }
        )

    genre_scores.sort(key=lambda item: (-float(item["final_score"]), item["genre"]))
    for index, item in enumerate(genre_scores, start=1):
        item["rank_in_run"] = index
    return genre_scores


def _build_favorite_lookup(favorite_genres: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("genre")): item
        for item in favorite_genres
        if item.get("genre") and item.get("is_active", True)
    }


def _rating_signal(value: Any) -> float:
    if value is None:
        return 0.0
    rating = float(value)
    return _clamp((rating - 5.5) / 4.5, -1.0, 1.0)


def _watch_signal(value: Any) -> float:
    watch_count = int(value or 0)
    if watch_count <= 0:
        return 0.0
    return _clamp(math.log1p(watch_count) / math.log(4.0), 0.0, 1.0)


def _recency_signal(value: Any, observed_at: datetime) -> float:
    if value is None:
        return 0.0
    last_watched_at = _coerce_datetime(value)
    age_days = max((observed_at - last_watched_at).days, 0)
    return round(math.exp(-age_days / 240.0), 4)


def _actor_affinity_signal(value: Any) -> float:
    if value is None:
        return 0.0
    rating = float(value)
    if rating <= 0:
        return 0.0
    return _clamp((rating - 5.0) / 5.0, -1.0, 1.0)


def _favorite_overlap_score(item: dict[str, Any] | None, *, max_favorite_weight: float) -> float:
    if not item:
        return 0.0
    rank = int(item.get("preference_rank") or 999)
    rank_signal = 1.0 / math.sqrt(max(rank, 1))
    weight_signal = float(item.get("weight") or 1.0) / max(max_favorite_weight, 1.0)
    return _clamp((0.65 * rank_signal) + (0.35 * weight_signal), 0.0, 1.0)


def _novelty_score(catalog_title_count: int, *, max_catalog_count: int) -> float:
    if catalog_title_count <= 0 or max_catalog_count <= 0:
        return 0.0
    share = catalog_title_count / max_catalog_count
    return _clamp(1.0 - math.sqrt(share), 0.0, 1.0)


def _consistency_score(items: Sequence[dict[str, Any]]) -> float:
    rated = [item for item in items if item["rating"] is not None]
    if rated:
        positive = sum(1 for item in rated if float(item["rating"]) >= 7.0)
        negative = sum(1 for item in rated if float(item["rating"]) <= 4.5)
        return _clamp((positive - negative + len(rated)) / (2 * len(rated)), 0.0, 1.0)
    positive_affinity = sum(1 for item in items if item["title_affinity"] > 0.15)
    return _clamp(positive_affinity / max(len(items), 1), 0.0, 1.0)


def _actor_affinity_score(items: Sequence[dict[str, Any]]) -> float:
    rated_items = [item for item in items if item.get("actor_affinity_rating") is not None]
    if not rated_items:
        return 0.0
    raw = _weighted_average(
        [float(item["actor_affinity_signal"]) for item in rated_items],
        [float(item["title_weight"]) for item in rated_items],
    )
    return _clamp((raw + 1.0) / 2.0, 0.0, 1.0)


def _confidence_score(items: Sequence[dict[str, Any]]) -> float:
    evidence = sum(1.5 for item in items if item["rating"] is not None) + sum(
        1.0 for item in items if item["watch_count"] > 0
    )
    if evidence <= 0:
        return 0.0
    return _clamp(math.log1p(evidence) / math.log(8.0), 0.0, 1.0)


def _top_titles(items: Sequence[dict[str, Any]], *, limit: int, reverse: bool) -> list[dict[str, Any]]:
    ordered = sorted(items, key=lambda item: (item["title_affinity"], item["watch_count"], item["title"] or ""), reverse=reverse)
    result: list[dict[str, Any]] = []
    for item in ordered[:limit]:
        result.append(
            {
                "tconst": item["tconst"],
                "title": item["title"],
                "year": item["year"],
                "rating": item["rating"],
                "watch_count": item["watch_count"],
                "title_affinity": round(float(item["title_affinity"]), 4),
            }
        )
    return result


def _build_explanation(
    *,
    affinity_score: float,
    rating_signal_score: float,
    watch_signal_score: float,
    recency_score: float,
    actor_affinity_score: float,
    preference_overlap_score: float,
    titles_considered: int,
) -> str:
    reasons: list[str] = []
    if rating_signal_score > 0.20:
        reasons.append("strong ratings")
    elif rating_signal_score < -0.20:
        reasons.append("weak ratings")
    if watch_signal_score > 0.20:
        reasons.append("repeat viewing")
    if recency_score > 0.35:
        reasons.append("recent activity")
    if actor_affinity_score > 0.62:
        reasons.append("liked cast")
    if preference_overlap_score > 0.20:
        reasons.append("manual preference")
    if not reasons:
        reasons.append("light evidence")
    return (
        f"{titles_considered} titles contributed; "
        f"affinity={affinity_score:.2f}; "
        f"signals={', '.join(reasons)}"
    )


def _weighted_average(values: Sequence[float], weights: Sequence[float]) -> float:
    weighted_sum = 0.0
    total_weight = 0.0
    for value, weight in zip(values, weights, strict=False):
        weighted_sum += value * weight
        total_weight += weight
    if total_weight <= 0:
        return 0.0
    return weighted_sum / total_weight


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise TypeError(f"Unsupported datetime value: {value!r}")


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
