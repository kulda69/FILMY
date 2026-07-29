"""Title a person lookup engine nad PostgreSQL katalogem."""

from __future__ import annotations

"""Title/person lookup operations extracted from `filmy.db`.

`filmy.db` remains the public facade and still exposes wrapper helpers so
existing tests can patch the same names. The actual lookup implementation lives
here to reduce the responsibility surface of the main module.
"""

import difflib
import importlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from filmy.runtime_postgres import (
    _connect as _pg_connect,
    fetch_catalog_search_rows,
    fetch_catalog_title_row,
    fetch_people_for_lookup_fuzzy_rows,
    fetch_people_for_lookup_levenshtein_rows,
    fetch_people_for_lookup_rows,
    fetch_person_lookup_row,
    fetch_search_recall_match,
)

logger = logging.getLogger(__name__)


def _db():
    """Vrat facade modul `filmy.db` kvuli zpetne kompatibilnim helperum."""
    return importlib.import_module("filmy.db")


class TitleLookupEngine:
    """Orchestrace lookupu titulu nad jednim dotazem."""

    def __init__(
        self,
        *,
        query: str,
        title_type: str | None,
        candidates_limit: int,
        allow_expensive_fallback: bool,
    ) -> None:
        """Priprav stav lookupu jednoho title dotazu."""
        self.db = _db()
        self.query = query
        self.title_type = title_type
        self.candidates_limit = candidates_limit
        self.allow_expensive_fallback = allow_expensive_fallback
        self.started_at = time.perf_counter()
        self.query_key = self.db._normalize_match_key(query)
        self.query_tokens = self.db._match_tokens(self.query_key)

    def run(self) -> dict[str, Any] | None:
        """Proved cely title lookup flow od recall po pripadny wide fallback."""
        recalled = self.try_recall()
        if recalled is not None:
            self._log_and_return("recall", recalled)
            return recalled
        candidates = self.load_direct_candidates()
        direct_result = self.try_direct_result(candidates)
        if direct_result is not None:
            self._log_and_return("direct", direct_result)
            return direct_result
        candidates = self.expand_to_fuzzy_if_needed(candidates)
        if not candidates:
            logger.info(
                "lookup_title_by_query query=%r mode=miss elapsed_ms=%.1f",
                self.query,
                self.elapsed_ms,
            )
            return None
        selected = self.db._pick_best_title_match(self.query, candidates)
        used_wide_mode = False
        if self.should_expand_to_wide(selected):
            candidates = self.expand_to_wide(candidates)
            if not candidates:
                logger.info(
                    "lookup_title_by_query query=%r mode=wide-miss elapsed_ms=%.1f",
                    self.query,
                    self.elapsed_ms,
                )
                return None
            selected = self.db._pick_best_title_match(self.query, candidates)
            used_wide_mode = True
        result = self.db._build_title_lookup_result(
            query=self.query,
            title_type=self.title_type,
            selected=selected,
            candidates=candidates,
            candidates_limit=self.candidates_limit,
        )
        if result is not None:
            self.db._remember_title_lookup(self.query, selected)
            self._log_and_return("wide" if used_wide_mode else "fuzzy", result)
        return result

    @property
    def expanded_limit(self) -> int:
        """Vrat vetsi internni limit pro mezikroky lookupu."""
        return max(self.candidates_limit, 1) * 5

    @property
    def elapsed_ms(self) -> float:
        """Vrat dosud uplynuly cas lookupu v milisekundach."""
        return (time.perf_counter() - self.started_at) * 1000

    def try_recall(self) -> dict[str, Any] | None:
        """Zkus lookup obslouzit pres search recall tabulku."""
        return self.db._lookup_title_from_search_recall(
            self.query,
            title_type=self.title_type,
            candidates_limit=self.candidates_limit,
        )

    def load_direct_candidates(self) -> list[dict[str, Any]]:
        """Nacti primarni kandidaty z title lookupu a aliasu."""
        candidates = self.db._search_catalog_for_lookup(
            query=self.query,
            title_type=self.title_type,
            limit=self.expanded_limit,
        )
        if candidates or len(self.query_tokens) != 1 or len(self.query_key) < 5:
            alias_candidates = self.db._search_catalog_aliases_for_lookup(
                query=self.query,
                title_type=self.title_type,
                limit=self.expanded_limit,
            )
            candidates = self.db._merge_lookup_candidates(candidates, alias_candidates)
        return candidates

    def try_direct_result(self, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Vrat vysledek hned, pokud uz prime kandidaty staci bez fuzzy kroku."""
        if not candidates:
            return None
        self.db._attach_library_summaries_to_exact_title_candidates(self.query, candidates)
        direct_selected = self.db._pick_best_title_match(self.query, candidates)
        if not self.db._is_direct_enough_lookup(self.query, direct_selected):
            return None
        result = self.db._build_title_lookup_result(
            query=self.query,
            title_type=self.title_type,
            selected=direct_selected,
            candidates=candidates,
            candidates_limit=self.candidates_limit,
        )
        if result is not None:
            self.db._remember_title_lookup(self.query, direct_selected)
        return result

    def expand_to_fuzzy_if_needed(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rozsir kandidaty o levnejsi fuzzy hledani nad tituly a aliasy."""
        if not candidates and not self.db._should_expand_to_fuzzy(self.query, candidates):
            return candidates
        if candidates and not self.db._should_expand_to_fuzzy(self.query, candidates):
            return candidates
        fuzzy_candidates = self.db._search_catalog_for_lookup_fuzzy(
            query=self.query,
            title_type=self.title_type,
            limit=self.expanded_limit,
        )
        candidates = self.db._merge_lookup_candidates(candidates, fuzzy_candidates)
        alias_fuzzy_candidates = self.db._search_catalog_aliases_for_lookup_fuzzy(
            query=self.query,
            title_type=self.title_type,
            limit=self.expanded_limit,
        )
        candidates = self.db._merge_lookup_candidates(candidates, alias_fuzzy_candidates)
        self.db._attach_library_summaries_to_exact_title_candidates(self.query, candidates)
        return candidates

    def should_expand_to_wide(self, selected: dict[str, Any]) -> bool:
        """Rozhodni, zda ma smysl drazsi wide fallback."""
        return (
            self.allow_expensive_fallback
            and len(self.query_tokens) > 1
            and not self.db._is_confident_lookup(self.query, selected)
        )

    def expand_to_wide(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rozsir kandidaty o drazsi Levenshtein fallback."""
        wide_candidates = self.db._search_catalog_for_lookup_levenshtein(
            query=self.query,
            title_type=self.title_type,
            limit=self.expanded_limit,
        )
        candidates = self.db._merge_lookup_candidates(candidates, wide_candidates)
        alias_wide_candidates = self.db._search_catalog_aliases_for_lookup_levenshtein(
            query=self.query,
            title_type=self.title_type,
            limit=self.expanded_limit,
        )
        return self.db._merge_lookup_candidates(candidates, alias_wide_candidates)

    def _log_and_return(self, mode: str, result: dict[str, Any]) -> None:
        """Zaloguj uspesny lookup mod a pocet kandidatu."""
        logger.info(
            "lookup_title_by_query query=%r mode=%s candidates=%s elapsed_ms=%.1f",
            self.query,
            mode,
            result.get("candidate_count"),
            self.elapsed_ms,
        )


class PersonLookupEngine:
    """Orchestrace lookupu osoby nad jednim dotazem."""

    def __init__(self, *, query: str, candidates_limit: int) -> None:
        """Priprav stav lookupu jedne osoby."""
        self.db = _db()
        self.query = query
        self.candidates_limit = candidates_limit

    @property
    def expanded_limit(self) -> int:
        """Vrat vetsi internni limit pro person lookup mezikroky."""
        return max(self.candidates_limit, 1) * 5

    def run(self) -> dict[str, Any] | None:
        """Proved cely lookup osoby vcetne fuzzy a wide fallbacku."""
        recalled = self.db._lookup_person_from_search_recall(
            self.query,
            candidates_limit=max(self.candidates_limit, 1),
        )
        if recalled is not None:
            return recalled
        candidates = self.db._search_people_for_lookup(query=self.query, limit=self.expanded_limit)
        if not candidates or self.db._should_expand_people_to_fuzzy(self.query, candidates):
            fuzzy_candidates = self.db._search_people_for_lookup_fuzzy(
                query=self.query,
                limit=self.expanded_limit,
            )
            candidates = self.db._merge_lookup_candidates(candidates, fuzzy_candidates)
        if not candidates:
            return None
        selected = self.db._pick_best_person_match(self.query, candidates)
        if not self.db._is_confident_person_lookup(self.query, selected):
            wide_candidates = self.db._search_people_for_lookup_levenshtein(
                query=self.query,
                limit=self.expanded_limit,
            )
            candidates = self.db._merge_lookup_candidates(candidates, wide_candidates)
            if not candidates:
                return None
            selected = self.db._pick_best_person_match(self.query, candidates)
        else:
            selected = self.db._pick_best_person_match(self.query, candidates)
        selected_key = selected["nconst"]
        ordered_candidates = sorted(
            candidates,
            key=lambda item: (
                0 if item["nconst"] == selected_key else 1,
                -(item.get("birth_year") or 0),
                item["primary_name"],
            ),
        )
        result = {
            "query": self.query,
            "selected_nconst": selected_key,
            "selected": self.db._build_person_lookup_candidate(selected, query=self.query, is_selected=True),
            "candidates": [
                self.db._build_person_lookup_candidate(
                    candidate,
                    query=self.query,
                    is_selected=candidate["nconst"] == selected_key,
                )
                for candidate in ordered_candidates[: max(self.candidates_limit, 1)]
            ],
            "candidate_count": len(candidates),
        }
        self.db._remember_person_lookup(self.query, selected)
        return result


@dataclass
class TitleLookupCandidate:
    """Interni reprezentace kandidata pro lookup titulu."""

    tconst: str
    primary_title: str | None = None
    original_title: str | None = None
    title_type: str | None = None
    start_year: int | None = None
    runtime_minutes: int | None = None
    genres: list[str] = field(default_factory=list)
    average_rating: float | None = None
    num_votes: int | None = None
    library: dict[str, Any] = field(default_factory=dict)
    matched_alias_title: str | None = None
    fuzzy_score: float | None = None
    alias_region: str | None = None
    alias_language: str | None = None
    alias_priority: int | None = None

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "TitleLookupCandidate":
        """Preved slovnikovy lookup payload na typovany title kandidat."""
        return cls(
            tconst=str(item["tconst"]),
            primary_title=item.get("primary_title"),
            original_title=item.get("original_title"),
            title_type=item.get("title_type"),
            start_year=item.get("start_year"),
            runtime_minutes=item.get("runtime_minutes"),
            genres=list(item.get("genres") or []),
            average_rating=item.get("average_rating"),
            num_votes=item.get("num_votes"),
            library=dict(item.get("library") or {}),
            matched_alias_title=item.get("matched_alias_title"),
            fuzzy_score=item.get("fuzzy_score"),
            alias_region=item.get("alias_region"),
            alias_language=item.get("alias_language"),
            alias_priority=item.get("alias_priority"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Preved title kandidata zpet na bezny slovnik."""
        return {
            "tconst": self.tconst,
            "primary_title": self.primary_title,
            "original_title": self.original_title,
            "title_type": self.title_type,
            "start_year": self.start_year,
            "runtime_minutes": self.runtime_minutes,
            "genres": list(self.genres),
            "average_rating": self.average_rating,
            "num_votes": self.num_votes,
            "library": dict(self.library),
            "matched_alias_title": self.matched_alias_title,
            "fuzzy_score": self.fuzzy_score,
            "alias_region": self.alias_region,
            "alias_language": self.alias_language,
            "alias_priority": self.alias_priority,
        }


@dataclass
class PersonLookupCandidate:
    """Interni reprezentace kandidata pro lookup osoby."""

    nconst: str
    primary_name: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    primary_profession: str | None = None
    known_for_titles: str | None = None
    filmography: dict[str, Any] = field(default_factory=dict)
    credit_count: int = 0
    fuzzy_score: float | None = None

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "PersonLookupCandidate":
        """Preved slovnikovy lookup payload na typovany person kandidat."""
        return cls(
            nconst=str(item["nconst"]),
            primary_name=item.get("primary_name"),
            birth_year=item.get("birth_year"),
            death_year=item.get("death_year"),
            primary_profession=item.get("primary_profession"),
            known_for_titles=item.get("known_for_titles"),
            filmography=dict(item.get("filmography") or {}),
            credit_count=int(item.get("credit_count") or 0),
            fuzzy_score=item.get("fuzzy_score"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Preved person kandidata zpet na bezny slovnik."""
        return {
            "nconst": self.nconst,
            "primary_name": self.primary_name,
            "birth_year": self.birth_year,
            "death_year": self.death_year,
            "primary_profession": self.primary_profession,
            "known_for_titles": self.known_for_titles,
            "filmography": dict(self.filmography),
            "credit_count": self.credit_count,
            "fuzzy_score": self.fuzzy_score,
        }


def describe_title_by_query(query: str, title_type: str | None = None) -> dict[str, Any] | None:
    """Najdi titul podle dotazu a vrat jeho presentation detail s match metadaty."""
    lookup = lookup_title_by_query(query=query, title_type=title_type, candidates_limit=5)
    if lookup is None:
        return None
    source_presentation = _db().get_title_presentation(lookup["selected_tconst"])
    if source_presentation is None:
        return None
    presentation = dict(source_presentation)
    presentation["query"] = query
    presentation["match"] = dict(lookup["selected"])
    return presentation


def describe_person_by_query(query: str) -> dict[str, Any] | None:
    """Najdi osobu podle dotazu a vrat jeji presentation detail s match metadaty."""
    from filmy.db_people import get_person_presentation

    lookup = lookup_person_by_query(query=query, candidates_limit=5)
    if lookup is None:
        return None
    presentation = get_person_presentation(lookup["selected_nconst"])
    if presentation is None:
        return None
    selected = dict(presentation)
    selected["query"] = query
    selected["match"] = dict(lookup["selected"])
    return selected


def _title_candidate_from_presentation(
    presentation: dict[str, Any],
    *,
    fuzzy_score: float | None = None,
    matched_alias_title: str | None = None,
) -> dict[str, Any]:
    """Vytvor title lookup kandidata z jiz slozene presentation struktury."""
    return {
        "tconst": presentation["tconst"],
        "primary_title": presentation.get("title"),
        "original_title": presentation.get("original_title"),
        "title_type": presentation.get("title_type"),
        "start_year": presentation.get("year"),
        "runtime_minutes": presentation.get("runtime_minutes"),
        "genres": presentation.get("genres") or [],
        "average_rating": presentation.get("imdb_rating"),
        "num_votes": presentation.get("imdb_votes"),
        "library": presentation.get("library_state") or {},
        "matched_alias_title": matched_alias_title,
        "fuzzy_score": fuzzy_score,
    }


def _person_candidate_from_presentation(
    presentation: dict[str, Any], *, fuzzy_score: float | None = None
) -> dict[str, Any]:
    """Vytvor person lookup kandidata z jiz slozene presentation struktury."""
    return {
        "nconst": presentation["nconst"],
        "primary_name": presentation.get("name"),
        "birth_year": presentation.get("birth_year"),
        "death_year": presentation.get("death_year"),
        "primary_profession": presentation.get("primary_profession"),
        "known_for_titles": presentation.get("known_for_titles"),
        "filmography": presentation.get("filmography") or {},
        "credit_count": presentation.get("credit_count") or 0,
        "fuzzy_score": fuzzy_score,
    }


def _lookup_title_from_search_recall(
    query: str, *, title_type: str | None, candidates_limit: int
) -> dict[str, Any] | None:
    """Try to satisfy title lookup from the small recent-search recall table first."""
    db = _db()
    query_key = db._normalize_match_key(query)
    query_text = db._normalize_search_query_text(query)
    if not query_key or not query_text:
        return None
    match = fetch_search_recall_match(
        entity_type="title",
        query_key=query_key,
        query_text_fold=query_text.casefold(),
    )
    row = None if match is None else (match[0], match[1], None)
    if row is None:
        return None
    title_row = fetch_catalog_title_row(str(row[0]))
    if title_row is None:
        return None
    if title_type is not None and str(title_row[1]) != str(title_type):
        return None
    candidate = db._catalog_row_to_dict(title_row)
    candidate["fuzzy_score"] = row[1]
    candidate["matched_alias_title"] = None
    if not _db()._is_safe_recalled_title(
        query, title_type=title_type, recalled=candidate, candidates_limit=candidates_limit
    ):
        return None
    result = _db()._build_title_lookup_result(
        query=query,
        title_type=title_type,
        selected=candidate,
        candidates=[candidate],
        candidates_limit=candidates_limit,
    )
    if result is not None:
        db._record_search_recall_entry(
            entity_type="title",
            query=query,
            target_id=str(candidate["tconst"]),
            target_label=str(candidate.get("primary_title") or ""),
            target_title_type=str(candidate.get("title_type") or ""),
            matched_alias_title=candidate.get("matched_alias_title"),
            fuzzy_score=candidate.get("fuzzy_score"),
        )
    return result


def _remember_title_lookup(query: str, selected: dict[str, Any]) -> None:
    """Uloz dostatecne jisty title lookup do search recall vrstvy."""
    db = _db()
    if not db._is_confident_lookup(query, selected) and (not db._is_direct_enough_lookup(query, selected)):
        return
    db._record_search_recall_entry(
        entity_type="title",
        query=query,
        target_id=str(selected["tconst"]),
        target_label=str(selected.get("primary_title") or ""),
        target_title_type=str(selected.get("title_type") or ""),
        matched_alias_title=selected.get("matched_alias_title"),
        fuzzy_score=selected.get("fuzzy_score"),
    )


def lookup_title_by_query(
    query: str,
    title_type: str | None = None,
    candidates_limit: int = 5,
    allow_expensive_fallback: bool = False,
) -> dict[str, Any] | None:
    """Verejna facade pro title lookup nad textovym dotazem."""
    engine = TitleLookupEngine(
        query=query,
        title_type=title_type,
        candidates_limit=candidates_limit,
        allow_expensive_fallback=allow_expensive_fallback,
    )
    return engine.run()


def lookup_person_by_query(query: str, candidates_limit: int = 5) -> dict[str, Any] | None:
    """Verejna facade pro lookup osoby nad textovym dotazem."""
    engine = PersonLookupEngine(query=query, candidates_limit=candidates_limit)
    return engine.run()


def _pick_best_title_match(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Vyber nejlepsi title shodu z kandidatu podle exact a fuzzy signalu."""
    query_key = _db()._normalize_match_key(query)
    if not candidates:
        raise ValueError("Candidates are required.")
    exact_matches = [candidate for candidate in candidates if _is_exact_title_match(query, candidate)]
    if exact_matches:
        if len(exact_matches) > 1:
            exact_matches.sort(
                key=lambda item: (
                    _lookup_local_signal_score(item),
                    -int(item.get("alias_priority") or 99),
                    item.get("num_votes") or 0,
                    item.get("start_year") or 0,
                ),
                reverse=True,
            )
        return exact_matches[0]
    scored_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_obj = TitleLookupCandidate.from_dict(candidate)
        variants = [
            candidate_obj.primary_title,
            candidate_obj.original_title,
            candidate_obj.matched_alias_title,
        ]
        candidate_obj.fuzzy_score = max(
            candidate_obj.fuzzy_score or 0.0,
            _best_title_similarity(query_key, variants),
        )
        scored_candidates.append(candidate_obj.to_dict())
    scored_candidates.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            item.get("num_votes") or 0,
            item.get("start_year") or 0,
        ),
        reverse=True,
    )
    return scored_candidates[0]


def _build_title_lookup_result(
    *,
    query: str,
    title_type: str | None,
    selected: dict[str, Any],
    candidates: list[dict[str, Any]],
    candidates_limit: int,
) -> dict[str, Any]:
    """Sloz finalni title lookup payload se selected i shortlistem kandidatu."""
    selected_key = selected["tconst"]
    ordered_candidates = sorted(
        candidates,
        key=lambda item: (
            0 if item["tconst"] == selected_key else 1,
            -float(item.get("fuzzy_score") or 0.0),
            -(item.get("num_votes") or 0),
            -(item.get("start_year") or 0),
            str(item.get("primary_title") or ""),
        ),
    )
    return {
        "query": query,
        "title_type": title_type,
        "selected_tconst": selected_key,
        "selected": _build_lookup_candidate(selected, query=query, is_selected=True),
        "candidates": [
            _build_lookup_candidate(candidate, query=query, is_selected=candidate["tconst"] == selected_key)
            for candidate in ordered_candidates[: max(candidates_limit, 1)]
        ],
        "candidate_count": len(candidates),
    }


def _lookup_person_from_search_recall(query: str, *, candidates_limit: int) -> dict[str, Any] | None:
    """Zkus obslouzit person lookup pres malou recall tabulku."""
    db = _db()
    query_key = db._normalize_match_key(query)
    query_text = db._normalize_search_query_text(query)
    if not query_key or not query_text:
        return None
    match = fetch_search_recall_match(
        entity_type="person",
        query_key=query_key,
        query_text_fold=query_text.casefold(),
    )
    row = None if match is None else (match[0], match[1], None)
    if row is None:
        return None
    person_row = fetch_person_lookup_row(str(row[0]))
    if person_row is None:
        return None
    candidate = _person_lookup_item_from_row(person_row)
    candidate["fuzzy_score"] = row[1]
    result = {
        "query": query,
        "selected_nconst": str(candidate["nconst"]),
        "selected": _build_person_lookup_candidate(candidate, query=query, is_selected=True),
        "candidates": [_build_person_lookup_candidate(candidate, query=query, is_selected=True)],
        "candidate_count": 1,
    }
    db._record_search_recall_entry(
        entity_type="person",
        query=query,
        target_id=str(candidate["nconst"]),
        target_label=str(candidate.get("primary_name") or ""),
        target_title_type=None,
        matched_alias_title=None,
        fuzzy_score=candidate.get("fuzzy_score"),
    )
    return result


def _remember_person_lookup(query: str, selected: dict[str, Any]) -> None:
    """Uloz dostatecne jisty person lookup do search recall vrstvy."""
    db = _db()
    if not _is_confident_person_lookup(query, selected):
        return
    db._record_search_recall_entry(
        entity_type="person",
        query=query,
        target_id=str(selected["nconst"]),
        target_label=str(selected.get("primary_name") or ""),
        target_title_type=None,
        matched_alias_title=None,
        fuzzy_score=selected.get("fuzzy_score"),
    )


def _pick_best_person_match(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Vyber nejlepsi person shodu z kandidatu podle exact a fuzzy signalu."""
    query_key = _db()._normalize_match_key(query)
    if not candidates:
        raise ValueError("Candidates are required.")
    exact_matches = [
        candidate
        for candidate in candidates
        if _db()._normalize_match_key(candidate.get("primary_name")) == query_key
    ]
    if exact_matches:
        exact_matches.sort(
            key=lambda item: (item.get("credit_count") or 0, item.get("birth_year") or 0),
            reverse=True,
        )
        return exact_matches[0]
    scored_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_obj = PersonLookupCandidate.from_dict(candidate)
        candidate_obj.fuzzy_score = max(
            candidate_obj.fuzzy_score or 0.0,
            _best_person_name_similarity(query_key, candidate_obj.primary_name),
        )
        scored_candidates.append(candidate_obj.to_dict())
    scored_candidates.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            item.get("credit_count") or 0,
            item.get("birth_year") or 0,
        ),
        reverse=True,
    )
    return scored_candidates[0]


def _build_lookup_candidate(candidate: dict[str, Any], *, query: str, is_selected: bool) -> dict[str, Any]:
    """Obal title kandidata o spolecna lookup metadata."""
    return {**candidate, "query": query, "is_selected": is_selected}


def _build_person_lookup_candidate(candidate: dict[str, Any], *, query: str, is_selected: bool) -> dict[str, Any]:
    """Obal person kandidata o spolecna lookup metadata."""
    return {**candidate, "query": query, "is_selected": is_selected}


def _is_confident_person_lookup(query: str, candidate: dict[str, Any]) -> bool:
    """Rozhodni, zda je nalezena osoba dostatecne jista bez dalsiho fallbacku."""
    if _db()._normalize_match_key(candidate.get("primary_name")) == _db()._normalize_match_key(query):
        return True
    return (candidate.get("fuzzy_score") or 0.0) >= 0.82


def _should_expand_people_to_fuzzy(query: str, candidates: list[dict[str, Any]]) -> bool:
    """Rozhodni, zda ma person lookup prejit z primych kandidatu na fuzzy hledani."""
    query_key = _db()._normalize_match_key(query)
    if not query_key or not candidates:
        return True
    for candidate in candidates[:3]:
        if _db()._normalize_match_key(candidate.get("primary_name")) == query_key:
            return False
    best_direct_score = max(
        (_best_person_name_similarity(query_key, candidate.get("primary_name")) for candidate in candidates[:5])
    )
    return best_direct_score < 0.72


def _person_lookup_item_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Preved SQL radek osoby na normalizovany person lookup kandidat."""
    return PersonLookupCandidate(
        nconst=str(row[0]),
        primary_name=row[1],
        birth_year=row[2],
        death_year=row[3],
        primary_profession=row[4],
        known_for_titles=row[5],
        credit_count=int(row[6] or 0),
    ).to_dict()


def _search_people_for_lookup(query: str, limit: int) -> list[dict[str, Any]]:
    """Nacti prime person kandidaty z PostgreSQL lookupu."""
    rows = fetch_people_for_lookup_rows(query, limit)
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _person_lookup_item_from_row(row)
        item["fuzzy_score"] = _best_person_name_similarity(_db()._normalize_match_key(query), item["primary_name"])
        items.append(item)
    return items


def _search_people_for_lookup_fuzzy(query: str, limit: int) -> list[dict[str, Any]]:
    """Nacti person kandidaty z levnejsi fuzzy vrstvy."""
    query_key = _db()._normalize_match_key(query)
    if len(query_key) < 3:
        return []
    rows = fetch_people_for_lookup_fuzzy_rows(query_key, 500)
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _person_lookup_item_from_row(row)
        item["fuzzy_score"] = _best_person_name_similarity(query_key, item["primary_name"])
        items.append(item)
    items.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            item.get("credit_count") or 0,
            item.get("birth_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in items if (item.get("fuzzy_score") or 0.0) >= 0.65][:limit]


def _search_people_for_lookup_levenshtein(query: str, limit: int) -> list[dict[str, Any]]:
    """Nacti person kandidaty z drazsi Levenshtein fallback vrstvy."""
    query_key = _db()._normalize_match_key(query)
    if len(query_key) < 4:
        return []
    rows = fetch_people_for_lookup_levenshtein_rows(query_key, 500)
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _person_lookup_item_from_row(row[:7])
        item["fuzzy_score"] = _best_person_name_similarity(query_key, item["primary_name"])
        items.append(item)
    items.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            item.get("credit_count") or 0,
            item.get("birth_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in items if (item.get("fuzzy_score") or 0.0) >= 0.65][:limit]


def _is_confident_lookup(query: str, candidate: dict[str, Any]) -> bool:
    """Rozhodni, zda je title kandidat dostatecne jisty bez dalsiho fallbacku."""
    db = _db()
    if db._normalize_match_key(candidate.get("primary_title")) == db._normalize_match_key(query):
        return True
    if db._normalize_match_key(candidate.get("original_title")) == db._normalize_match_key(query):
        return True
    if db._normalize_match_key(candidate.get("matched_alias_title")) == db._normalize_match_key(query):
        return True
    if db._normalize_match_key(candidate.get("primary_title"), strip_leading_articles=True) == db._normalize_match_key(
        query, strip_leading_articles=True
    ):
        return True
    if db._normalize_match_key(candidate.get("original_title"), strip_leading_articles=True) == db._normalize_match_key(
        query, strip_leading_articles=True
    ):
        return True
    if db._normalize_match_key(
        candidate.get("matched_alias_title"), strip_leading_articles=True
    ) == db._normalize_match_key(query, strip_leading_articles=True):
        return True
    return (candidate.get("fuzzy_score") or 0.0) >= 0.82


def _is_direct_enough_lookup(query: str, candidate: dict[str, Any]) -> bool:
    """Rozhodni, zda prime title kandidaty uz staci bez fuzzy rozsirovani."""
    db = _db()
    query_key = db._normalize_match_key(query)
    query_key_articleless = db._normalize_match_key(query, strip_leading_articles=True)
    if not query_key:
        return False
    for variant in [candidate.get("primary_title"), candidate.get("original_title"), candidate.get("matched_alias_title")]:
        if db._normalize_match_key(variant) == query_key:
            return True
        if db._normalize_match_key(variant, strip_leading_articles=True) == query_key_articleless:
            return True
    return False


def _is_exact_title_match(query: str, candidate: dict[str, Any]) -> bool:
    """Vrat, zda kandidat odpovida dotazu jako presna title shoda."""
    return _is_direct_enough_lookup(query, candidate)


def _lookup_local_signal_score(candidate: dict[str, Any]) -> int:
    """Score exact-title ambiguities by Jiri's local library signals."""
    library = candidate.get("library") or {}
    score = 0
    if int(library.get("watched_count") or 0) > 0:
        score += 80
    rating = library.get("rating") or {}
    if rating.get("value") is not None:
        score += 60 + int(rating.get("value") or 0)
    if library.get("in_watchlist"):
        score += 40
    if library.get("lists"):
        score += 20
    return score


def _attach_library_summaries_to_exact_title_candidates(query: str, candidates: list[dict[str, Any]]) -> None:
    """Attach local state only where it helps disambiguate identical titles."""
    exact_candidates = [candidate for candidate in candidates if _is_exact_title_match(query, candidate)]
    if len(exact_candidates) < 2:
        return
    db = _db()
    for candidate in exact_candidates:
        if candidate.get("library"):
            continue
        try:
            candidate["library"] = db._fetch_library_summary(None, str(candidate["tconst"]), candidate.get("title_type"))
        except Exception:
            logger.debug("lookup library summary failed for %s", candidate.get("tconst"), exc_info=True)
            candidate["library"] = {}


def _is_safe_recalled_title(
    query: str, *, title_type: str | None, recalled: dict[str, Any], candidates_limit: int
) -> bool:
    """Avoid recall shortcuts for ambiguous exact-title queries."""
    db = _db()
    query_key = db._normalize_match_key(query)
    if len(_match_tokens(query_key)) != 1 or len(query_key) < 5:
        return True
    candidates = _search_catalog_for_lookup(
        query=query, title_type=title_type, limit=max(max(candidates_limit, 1) * 5, 25)
    )
    alias_candidates = _search_catalog_aliases_for_lookup(
        query=query, title_type=title_type, limit=max(max(candidates_limit, 1) * 5, 25)
    )
    candidates = _merge_lookup_candidates(candidates, alias_candidates)
    exact_candidates = [candidate for candidate in candidates if _is_exact_title_match(query, candidate)]
    return len(exact_candidates) < 2


def _should_expand_to_fuzzy(query: str, candidates: list[dict[str, Any]]) -> bool:
    """Rozhodni, zda ma title lookup prejit na fuzzy hledani."""
    db = _db()
    query_key = db._normalize_match_key(query)
    query_key_articleless = db._normalize_match_key(query, strip_leading_articles=True)
    if not query_key or not candidates:
        return True
    for candidate in candidates[:3]:
        if db._normalize_match_key(candidate.get("primary_title")) == query_key:
            return False
        if db._normalize_match_key(candidate.get("original_title")) == query_key:
            return False
        if db._normalize_match_key(candidate.get("matched_alias_title")) == query_key:
            return False
        if db._normalize_match_key(candidate.get("primary_title"), strip_leading_articles=True) == query_key_articleless:
            return False
        if db._normalize_match_key(candidate.get("original_title"), strip_leading_articles=True) == query_key_articleless:
            return False
        if db._normalize_match_key(candidate.get("matched_alias_title"), strip_leading_articles=True) == query_key_articleless:
            return False
    best_direct_score = max(
        (
            _best_title_similarity(
                query_key,
                [
                    candidate.get("primary_title"),
                    candidate.get("original_title"),
                    candidate.get("matched_alias_title"),
                ],
            )
            for candidate in candidates[:5]
        )
    )
    return best_direct_score < 0.72


def _merge_lookup_candidates(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sluc dva kandidatni seznamy a nech pro stejnou identitu lepsi skore."""
    merged: dict[str, dict[str, Any]] = {_lookup_identity_key(item): item for item in primary}
    for item in secondary:
        identity = _lookup_identity_key(item)
        existing = merged.get(identity)
        if existing is None:
            merged[identity] = item
            continue
        existing_score = existing.get("fuzzy_score") or 0.0
        new_score = item.get("fuzzy_score") or 0.0
        if new_score > existing_score:
            if "tconst" in item:
                merged[identity] = TitleLookupCandidate.from_dict({**existing, **item}).to_dict()
            else:
                merged[identity] = PersonLookupCandidate.from_dict({**existing, **item}).to_dict()
    return list(merged.values())


def _lookup_identity_key(item: dict[str, Any]) -> str:
    """Vrat stabilni identitu kandidata bez ohledu na typ entity."""
    return str(item.get("tconst") or item.get("nconst") or "")


def _alias_priority_case_sql(region_column: str, language_column: str) -> str:
    """Vrat SQL `CASE` pro preferenci ceskych a anglickych aliasu."""
    return f"""
        CASE
            WHEN lower(coalesce({language_column}, '')) = 'cs' OR upper(coalesce({region_column}, '')) = 'CZ' THEN 0
            WHEN lower(coalesce({language_column}, '')) = 'en'
                 OR upper(coalesce({region_column}, '')) IN ('US', 'GB', 'CA', 'IE', 'AU', 'NZ', 'IN') THEN 1
            ELSE 2
        END
    """


def _catalog_row_from_alias_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Preved aliasovy SQL radek na title lookup kandidata."""
    item = TitleLookupCandidate.from_dict(_db()._catalog_row_to_dict(row[:9]))
    item.matched_alias_title = row[9]
    item.alias_region = row[10]
    item.alias_language = row[11]
    item.alias_priority = row[12]
    return item.to_dict()


def _search_catalog_aliases_for_lookup(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Nacti prime aliasove kandidaty pro title lookup."""
    db = _db()
    query_key = db._normalize_match_key(query)
    query_key_articleless = db._normalize_match_key(query, strip_leading_articles=True)
    if not query_key:
        return []
    with _pg_connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
                SELECT
                    t.tconst,
                    t.title_type,
                    t.primary_title,
                    t.original_title,
                    t.start_year,
                    t.runtime_minutes,
                    t.genres,
                    t.average_rating,
                    t.num_votes,
                    a.title AS matched_alias_title,
                    a.region,
                    a.language,
                    a.alias_priority
                FROM app.title_alias_lookup AS a
                JOIN app.catalog_titles AS t ON t.tconst = a.tconst
                WHERE (
                    a.alias_key = %s
                    OR a.alias_key_articleless = %s
                    OR a.alias_key LIKE %s || '%%'
                    OR a.alias_key_articleless LIKE %s || '%%'
                )
                  AND (%s::text IS NULL OR t.title_type = %s::text)
                ORDER BY
                    a.alias_priority,
                    CASE
                        WHEN a.alias_key = %s THEN 0
                        WHEN a.alias_key_articleless = %s THEN 1
                        WHEN a.alias_key LIKE %s || '%%' THEN 2
                        WHEN a.alias_key_articleless LIKE %s || '%%' THEN 3
                        ELSE 4
                    END,
                    t.start_year DESC NULLS LAST,
                    t.num_votes DESC NULLS LAST,
                    t.primary_title
                LIMIT %s
                """,
            (
                query_key,
                query_key_articleless or query_key,
                query_key,
                query_key_articleless or query_key,
                title_type,
                title_type,
                query_key,
                query_key_articleless or query_key,
                query_key,
                query_key_articleless or query_key,
                limit,
            ),
        )
        rows = cursor.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _catalog_row_from_alias_row(row)
        item["fuzzy_score"] = _best_title_similarity(query_key, [item.get("matched_alias_title")])
        items.append(item)
    return items


def _search_catalog_aliases_for_lookup_fuzzy(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Nacti aliasove kandidaty z levnejsi fuzzy vrstvy."""
    db = _db()
    query_key = db._normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 3:
        return []
    prefix3 = query_key[:3]
    prefix2 = query_key[:2]
    length_floor = max(len(query_key) - 2, 1)
    length_ceiling = len(query_key) + 3
    scan_limit = 200
    with _pg_connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
                SELECT
                    t.tconst,
                    t.title_type,
                    t.primary_title,
                    t.original_title,
                    t.start_year,
                    t.runtime_minutes,
                    t.genres,
                    t.average_rating,
                    t.num_votes,
                    a.title AS matched_alias_title,
                    a.region,
                    a.language,
                    a.alias_priority
                FROM app.title_alias_lookup AS a
                JOIN app.catalog_titles AS t ON t.tconst = a.tconst
                WHERE (%s::text IS NULL OR t.title_type = %s::text)
                  AND (
                    a.alias_prefix3_articleless = %s
                    OR a.alias_prefix2_articleless = %s
                  )
                  AND a.alias_length_articleless BETWEEN %s AND %s
                ORDER BY
                    a.alias_priority,
                    t.num_votes DESC NULLS LAST,
                    t.average_rating DESC NULLS LAST,
                    t.start_year DESC NULLS LAST
                LIMIT %s
                """,
            (title_type, title_type, prefix3, prefix2, length_floor, length_ceiling, scan_limit),
        )
        rows = cursor.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _catalog_row_from_alias_row(row)
        item["fuzzy_score"] = _best_title_similarity(query_key, [item.get("matched_alias_title")])
        items.append(item)
    items.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            -int(item.get("alias_priority") or 99),
            item.get("num_votes") or 0,
            item.get("start_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in items if (item.get("fuzzy_score") or 0.0) >= 0.55][:limit]


def _search_catalog_aliases_for_lookup_levenshtein(
    query: str, title_type: str | None, limit: int
) -> list[dict[str, Any]]:
    """Nacti aliasove kandidaty z drazsi Levenshtein fallback vrstvy."""
    db = _db()
    query_key = db._normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 4:
        return []
    first_letter = query_key[0]
    query_len = len(query_key)
    length_floor = max(query_len - 4, 1)
    length_ceiling = query_len + 4
    with _pg_connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
                SELECT
                    t.tconst,
                    t.title_type,
                    t.primary_title,
                    t.original_title,
                    t.start_year,
                    t.runtime_minutes,
                    t.genres,
                    t.average_rating,
                    t.num_votes,
                    a.title AS matched_alias_title,
                    a.region,
                    a.language,
                    a.alias_priority
                FROM app.title_alias_lookup AS a
                JOIN app.catalog_titles AS t ON t.tconst = a.tconst
                WHERE (%s::text IS NULL OR t.title_type = %s::text)
                  AND a.alias_prefix1_articleless = %s
                  AND a.alias_length_articleless BETWEEN %s AND %s
                ORDER BY
                    a.alias_priority,
                    t.num_votes DESC NULLS LAST,
                    t.start_year DESC NULLS LAST
                LIMIT 500
                """,
            (title_type, title_type, first_letter, length_floor, length_ceiling),
        )
        rows = cursor.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _catalog_row_from_alias_row(row)
        item["fuzzy_score"] = _best_title_similarity(query_key, [item.get("matched_alias_title")])
        items.append(item)
    items.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            -int(item.get("alias_priority") or 99),
            item.get("num_votes") or 0,
            item.get("start_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in items if (item.get("fuzzy_score") or 0.0) >= 0.55][:limit]


def _search_catalog_for_lookup_fuzzy(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Nacti title kandidaty z levnejsi fuzzy vrstvy nad title lookupem."""
    db = _db()
    query_key = db._normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 3:
        return []
    prefix3 = query_key[:3]
    prefix2 = query_key[:2]
    length_floor = max(len(query_key) - 2, 1)
    length_ceiling = len(query_key) + 3
    scan_limit = 200
    with _pg_connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"""
                SELECT
                    tconst,
                    title_type,
                    primary_title,
                    original_title,
                    start_year,
                    runtime_minutes,
                    genres,
                    average_rating,
                    num_votes
                FROM app.title_lookup
                WHERE (%s::text IS NULL OR title_type = %s::text)
                  AND (
                    primary_prefix3 = %s
                    OR original_prefix3 = %s
                    OR primary_prefix2 = %s
                    OR original_prefix2 = %s
                  )
                  AND (
                    primary_length BETWEEN %s AND %s
                    OR original_length BETWEEN %s AND %s
                  )
                ORDER BY
                    num_votes DESC NULLS LAST,
                    average_rating DESC NULLS LAST,
                    start_year DESC NULLS LAST
                LIMIT {scan_limit}
                """,
            (title_type, title_type, prefix3, prefix3, prefix2, prefix2, length_floor, length_ceiling, length_floor, length_ceiling),
        )
        rows = cursor.fetchall()
    scored: list[dict[str, Any]] = []
    for row in rows:
        item = db._catalog_row_to_dict(row)
        item["fuzzy_score"] = _best_title_similarity(query_key, [item.get("primary_title"), item.get("original_title")])
        scored.append(item)
    scored.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            item.get("num_votes") or 0,
            item.get("start_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in scored if (item.get("fuzzy_score") or 0.0) >= 0.55][:limit]


def _search_catalog_for_lookup_levenshtein(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Nacti title kandidaty z drazsi Levenshtein fallback vrstvy nad title lookupem."""
    db = _db()
    query_key = db._normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 4:
        return []
    first_letter = query_key[0]
    query_len = len(query_key)
    length_floor = max(query_len - 4, 1)
    length_ceiling = query_len + 4
    with _pg_connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
                SELECT
                    tconst,
                    title_type,
                    primary_title,
                    original_title,
                    start_year,
                    runtime_minutes,
                    genres,
                    average_rating,
                    num_votes
                FROM app.title_lookup
                WHERE (%s::text IS NULL OR title_type = %s::text)
                  AND (
                    primary_prefix1 = %s
                    OR original_prefix1 = %s
                  )
                  AND (
                    primary_length BETWEEN %s AND %s
                    OR original_length BETWEEN %s AND %s
                  )
                ORDER BY num_votes DESC NULLS LAST, average_rating DESC NULLS LAST, start_year DESC NULLS LAST
                LIMIT 500
                """,
            (title_type, title_type, first_letter, first_letter, length_floor, length_ceiling, length_floor, length_ceiling),
        )
        rows = cursor.fetchall()
    scored: list[dict[str, Any]] = []
    for row in rows:
        item = db._catalog_row_to_dict(row[:9])
        item["fuzzy_score"] = _best_title_similarity(query_key, [item.get("primary_title"), item.get("original_title")])
        scored.append(item)
    scored.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            item.get("num_votes") or 0,
            item.get("start_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in scored if (item.get("fuzzy_score") or 0.0) >= 0.55][:limit]


def _best_title_similarity(query_key: str, variants: list[Any]) -> float:
    """Spocti nejlepsi similarity score dotazu proti vice title variantam."""
    query_tokens = _match_tokens(query_key)
    best = 0.0
    db = _db()
    for variant in variants:
        for variant_key in {
            db._normalize_match_key(variant),
            db._normalize_match_key(variant, strip_leading_articles=True),
        }:
            if not variant_key:
                continue
            sequence_score = difflib.SequenceMatcher(a=query_key, b=variant_key).ratio()
            token_score = _token_similarity_score(query_key, variant_key)
            score = sequence_score * 0.6 + token_score * 0.4 if len(query_tokens) > 1 else max(sequence_score, token_score)
            if variant_key.startswith(query_key) or query_key.startswith(variant_key):
                score = max(score, 0.8)
            best = max(best, score)
    return best


def _best_person_name_similarity(query_key: str, primary_name: Any) -> float:
    """Return the best fuzzy score for a person name across full-name and token variants."""
    db = _db()
    name_key = db._normalize_match_key(primary_name)
    if not query_key or not name_key:
        return 0.0
    name_tokens = _match_tokens(name_key)
    variants: list[str] = [name_key, name_key.replace(" ", "")]
    variants.extend(name_tokens)
    if len(name_tokens) > 1:
        variants.append(name_tokens[-1])
    seen: set[str] = set()
    ordered_variants: list[str] = []
    for variant in variants:
        if variant and variant not in seen:
            seen.add(variant)
            ordered_variants.append(variant)
    return _best_title_similarity(query_key, ordered_variants)


def _token_similarity_score(query_key: str, variant_key: str) -> float:
    """Spocti tokenove similarity score vcetne maleho bonusu za poradi tokenu."""
    query_tokens = _match_tokens(query_key)
    variant_tokens = _match_tokens(variant_key)
    if not query_tokens or not variant_tokens:
        return 0.0
    per_token_scores: list[float] = []
    for query_token in query_tokens:
        best_score = 0.0
        for variant_token in variant_tokens:
            best_score = max(best_score, difflib.SequenceMatcher(a=query_token, b=variant_token).ratio())
        per_token_scores.append(best_score)
    base_score = sum(per_token_scores) / len(per_token_scores)
    ordered_token_bonus = 0.05 if _tokens_are_subsequence(query_tokens, variant_tokens) else 0.0
    return min(1.0, base_score + ordered_token_bonus)


def _match_tokens(value: str) -> list[str]:
    """Rozdel normalizovany text na neprazdne tokeny."""
    return [token for token in value.split(" ") if token]


def _tokens_are_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """Zjisti, zda se tokeny dotazu objevuji ve stejnem poradi v kandidatu."""
    if not needle:
        return False
    index = 0
    for token in haystack:
        if token == needle[index]:
            index += 1
            if index == len(needle):
                return True
    return False


def _search_catalog_for_lookup(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Nacti prime title kandidaty z PostgreSQL title lookupu."""
    rows = fetch_catalog_search_rows(query=query, title_type=title_type, limit=limit)
    return [_db()._catalog_row_to_dict(row) for row in rows]
