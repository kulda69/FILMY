from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

from filmy import db


def test_lookup_title_bypasses_recall_for_ambiguous_exact_local_match() -> None:
    shelter_2026 = {
        "tconst": "tt32357218",
        "title_type": "movie",
        "primary_title": "Shelter",
        "original_title": "Shelter",
        "start_year": 2026,
        "runtime_minutes": 107,
        "genres": ["Action", "Thriller"],
        "average_rating": 6.2,
        "num_votes": 36304,
        "fuzzy_score": 1.0,
    }
    shelter_2007 = {
        "tconst": "tt0942384",
        "title_type": "movie",
        "primary_title": "Shelter",
        "original_title": "Shelter",
        "start_year": 2007,
        "runtime_minutes": 97,
        "genres": ["Drama", "Romance", "Sport"],
        "average_rating": 7.6,
        "num_votes": 25806,
    }

    def search_candidates(*args, **kwargs):
        return deepcopy([shelter_2026, shelter_2007])

    def library_summary(_conn, tconst: str, _title_type: str | None):
        if tconst == "tt0942384":
            return {
                "watched_count": 1,
                "in_watchlist": False,
                "rating": {"value": 8},
                "lists": [{"name": "AI návrhy"}],
            }
        return {"watched_count": 0, "in_watchlist": False, "rating": None, "lists": []}

    with (
        patch("filmy.db.fetch_search_recall_match", return_value=("tt32357218", 1.0)),
        patch(
            "filmy.db.fetch_catalog_title_row",
            return_value=(
                "tt32357218",
                "movie",
                "Shelter",
                "Shelter",
                2026,
                None,
                107,
                "Action,Thriller",
                6.2,
                36304,
            ),
        ),
        patch("filmy.db._search_catalog_for_lookup", side_effect=search_candidates),
        patch("filmy.db._search_catalog_aliases_for_lookup", return_value=[]),
        patch("filmy.db._fetch_library_summary", side_effect=library_summary),
        patch("filmy.db._record_search_recall_entry"),
    ):
        result = db.lookup_title_by_query("Shelter", title_type="movie", candidates_limit=5)

    assert result is not None
    assert result["selected_tconst"] == "tt0942384"
    assert result["candidates"][0]["tconst"] == "tt0942384"
    assert result["candidate_count"] == 2
