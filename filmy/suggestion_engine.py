"""Heuristiky pro homepage a zanrove suggestion kandidaty."""

from __future__ import annotations

from datetime import UTC, datetime
from math import log10
from typing import Any, Sequence


TRAIT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "cerebral": ("cerebral", "intellectual", "philosophical"),
    "mind-bending": ("mind-bending", "mind bending", "reality-bending", "surreal"),
    "thought-provoking": ("thought-provoking", "thought provoking", "provocative"),
    "dark": ("dark", "bleak", "grim"),
    "gritty": ("gritty", "raw", "hard-edged"),
    "tense": ("tense", "pressure", "high-stakes"),
    "atmospheric": ("atmospheric", "moody", "immersive"),
    "slow-burn": ("slow burn", "slow-burn", "gradually"),
    "mysterious": ("mysterious", "mystery", "enigmatic"),
    "twisty": ("twist", "twisty", "unexpected turn"),
    "emotional": ("emotional", "moving", "touching"),
    "melancholic": ("melancholic", "melancholy", "wistful"),
    "haunting": ("haunting", "disturbing", "lingering"),
    "romantic": ("romantic", "romance", "love story"),
    "feel-good": ("feel good", "feel-good", "uplifting"),
    "uplifting": ("uplifting", "inspiring", "hopeful"),
    "heartwarming": ("heartwarming", "warmhearted", "tender"),
    "funny": ("funny", "hilarious", "comedic"),
    "witty": ("witty", "sharp", "clever"),
    "stylized": ("stylized", "stylised", "visually bold"),
    "visually striking": ("visually striking", "visually stunning", "spectacular visuals"),
    "intense": ("intense", "relentless", "brutal"),
    "suspenseful": ("suspenseful", "suspense", "edge of your seat"),
    "character-driven": ("character-driven", "character driven", "character study"),
    "dialogue-driven": ("dialogue-driven", "dialogue driven", "talky"),
    "psychological": ("psychological", "psychology", "mental"),
    "dystopian": ("dystopian", "post-apocalyptic", "authoritarian"),
    "coming-of-age": ("coming-of-age", "coming of age", "adolescence"),
    "queer": ("queer", "gay", "lesbian", "lgbt"),
    "high-concept": ("high-concept", "high concept", "conceptual"),
}


def evaluate_trait_candidate(
    row: dict[str, Any],
    active_traits: Sequence[dict[str, Any]],
    genre_score_lookup: dict[str, float],
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Vyhodnoti, jak moc titul odpovida rucne zadaným favorite traits.

    Smysl neni delat plny semantic engine. Tahle vrstva ma byt lehka,
    vysvetlitelna a postavena jen na tom, co uz v lokalnich datech mame:
    TMDB/IMDb overview, lokalni zanrove score a oblibenost hercu.

    Pouzite signaly:
    - `trait_score`: shoda s textovymi klicovymi slovy pro aktivni traits
    - `genre_alignment_score`: bonus za zanry, ktere uz vysly dobre z historie
    - `imdb_quality_score`: jemny signal z IMDb ratingu a poctu hlasu
    - `freshness_score`: maly bonus za novejsi titul
    - `actor_affinity_score`: bonus za oblibene herce v hlavnim obsazeni
    """
    current_time = observed_at or datetime.now(UTC)
    matched_traits = _matched_traits(str(row.get("overview") or ""), active_traits)
    trait_score = _trait_match_score(matched_traits, active_traits)
    genre_alignment_score = _genre_alignment_score(row.get("genres") or [], genre_score_lookup)
    imdb_quality_score = _imdb_quality_score(row.get("average_rating"), row.get("num_votes"))
    freshness_score = _freshness_score(row.get("start_year"), row.get("release_date"), observed_at=current_time)
    actor_affinity_score = _actor_affinity_score(row.get("actor_affinity_rating"))
    total_score = _clamp(
        (0.46 * trait_score)
        + (0.18 * genre_alignment_score)
        + (0.16 * imdb_quality_score)
        + (0.10 * freshness_score)
        + (0.10 * actor_affinity_score),
        0.0,
        1.0,
    )
    return {
        "matched_traits": matched_traits,
        "trait_score": round(trait_score, 4),
        "genre_alignment_score": round(genre_alignment_score, 4),
        "imdb_quality_score": round(imdb_quality_score, 4),
        "freshness_score": round(freshness_score, 4),
        "actor_affinity_score": round(actor_affinity_score, 4),
        "total_score": round(total_score, 4),
    }


def evaluate_new_imdb_candidate(
    row: dict[str, Any],
    active_traits: Sequence[dict[str, Any]],
    genre_score_lookup: dict[str, float],
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Vyhodnoti titul pro blok `New on IMDb` z lokalniho katalogu.

    `New on IMDb` zde neznamena externi trending feed. Je to lokalni shortlist
    z nasi IMDb katalogove vrstvy: nove nebo cerstve uvedene tituly, ktere jeste
    nejsou watched a zaroven maji aspon trochu signal kvality nebo shody s
    preferencemi.
    """
    current_time = observed_at or datetime.now(UTC)
    freshness_score = _freshness_score(row.get("start_year"), row.get("release_date"), observed_at=current_time)
    is_recent = freshness_score >= 0.45
    matched_traits = _matched_traits(str(row.get("overview") or ""), active_traits)
    trait_score = _trait_match_score(matched_traits, active_traits)
    genre_alignment_score = _genre_alignment_score(row.get("genres") or [], genre_score_lookup)
    imdb_quality_score = _imdb_quality_score(row.get("average_rating"), row.get("num_votes"))
    actor_affinity_score = _actor_affinity_score(row.get("actor_affinity_rating"))
    total_score = _clamp(
        (0.34 * freshness_score)
        + (0.24 * imdb_quality_score)
        + (0.18 * genre_alignment_score)
        + (0.14 * trait_score)
        + (0.10 * actor_affinity_score),
        0.0,
        1.0,
    )
    return {
        "is_recent": is_recent,
        "matched_traits": matched_traits,
        "trait_score": round(trait_score, 4),
        "genre_alignment_score": round(genre_alignment_score, 4),
        "imdb_quality_score": round(imdb_quality_score, 4),
        "freshness_score": round(freshness_score, 4),
        "actor_affinity_score": round(actor_affinity_score, 4),
        "total_score": round(total_score, 4),
    }


def match_traits_for_text(overview: str, active_traits: Sequence[dict[str, Any]]) -> list[str]:
    """Public helper for lightweight trait matching over free text."""
    return _matched_traits(overview, active_traits)


def _matched_traits(overview: str, active_traits: Sequence[dict[str, Any]]) -> list[str]:
    """Najdi traits, jejichz klicova slova se objevila v overview."""

    normalized_overview = overview.casefold()
    if not normalized_overview:
        return []
    matches: list[str] = []
    for item in active_traits:
        trait = str(item.get("trait") or "").strip()
        if not trait:
            continue
        keywords = TRAIT_KEYWORDS.get(trait.casefold(), (trait.casefold(),))
        if any(keyword in normalized_overview for keyword in keywords):
            matches.append(trait)
    return matches


def _trait_match_score(matched_traits: Sequence[str], active_traits: Sequence[dict[str, Any]]) -> float:
    """Spocitej skore shody traits s vahou podle preference ranku."""

    if not matched_traits:
        return 0.0
    priority_lookup = {
        str(item.get("trait") or "").strip(): _priority_weight(item.get("preference_rank"))
        for item in active_traits
        if item.get("trait")
    }
    raw = sum(priority_lookup.get(trait, 0.0) for trait in matched_traits)
    return _clamp(raw, 0.0, 1.0)


def _priority_weight(priority: Any) -> float:
    """Preved preference rank na normalizovanou vahu 0..1."""

    if priority is None:
        return 0.0
    value = int(priority)
    return _clamp((11 - value) / 10.0, 0.0, 1.0)


def _genre_alignment_score(genres: Sequence[str], genre_score_lookup: dict[str, float]) -> float:
    """Vrat nejsilnejsi zanrovy signal pro dany titul."""

    if not genres:
        return 0.0
    scores = [float(genre_score_lookup.get(str(genre), 0.0)) for genre in genres if genre]
    if not scores:
        return 0.0
    return _clamp(max(scores), 0.0, 1.0)


def _imdb_quality_score(average_rating: Any, num_votes: Any) -> float:
    """Sloz jemny quality score z IMDb ratingu a poctu hlasu."""

    if average_rating is None:
        return 0.0
    rating_score = _clamp((float(average_rating) - 5.5) / 4.0, 0.0, 1.0)
    votes = max(int(num_votes or 0), 0)
    vote_score = _clamp(log10(votes + 1) / 5.0, 0.0, 1.0)
    return _clamp((0.68 * rating_score) + (0.32 * vote_score), 0.0, 1.0)


def _freshness_score(start_year: Any, release_date: Any, *, observed_at: datetime) -> float:
    """Spocitej bonus za novost titulu podle data vydani nebo roku."""

    if release_date:
        try:
            released = datetime.fromisoformat(str(release_date)).replace(tzinfo=UTC)
            age_days = max((observed_at - released).days, 0)
            return _clamp(1.0 - (age_days / 540.0), 0.0, 1.0)
        except ValueError:
            pass
    if start_year is None:
        return 0.0
    delta = observed_at.year - int(start_year)
    if delta <= -1:
        return 1.0
    if delta == 0:
        return 0.92
    if delta == 1:
        return 0.68
    if delta == 2:
        return 0.44
    return 0.0


def _actor_affinity_score(actor_affinity_rating: Any) -> float:
    """Normalizuj souhrnny affinity signal hercu do rozsahu 0..1."""

    if actor_affinity_rating is None:
        return 0.0
    return _clamp((float(actor_affinity_rating) - 5.0) / 5.0, 0.0, 1.0)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    """Omez cislo do zadaneho uzavreneho intervalu."""

    return max(minimum, min(maximum, value))
