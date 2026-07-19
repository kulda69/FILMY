from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from filmy.routers.web import router


def _presentation() -> dict:
    return {
        "tconst": "tt2316411",
        "title": "Enemy",
        "original_title": "Enemy",
        "kind": "title",
        "kind_label": "Movie",
        "year": 2013,
        "end_year": None,
        "runtime_minutes": 91,
        "imdb_rating": 6.9,
        "genres": ["Drama", "Mystery"],
        "directed_by": [],
        "written_by": [],
        "created_by": [],
        "main_cast": [],
        "episodes": [],
        "poster_url": None,
        "backdrop_url": None,
        "tmdb_providers": [],
        "tmdb_details": {},
        "content_state": {},
        "library_state": {
            "rating": {
                "value": 8,
                "liked_notes": "Moje klady.",
                "disliked_notes": "Moje zapory.",
            }
        },
    }


def test_title_detail_renders_ai_fit_and_risk_reasons_inside_rating_notes() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    ai_recommendation = {
        "source_filename": "recommendations.json",
        "confidence": "high",
        "imported_at": datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        "fit_reasons": ["AI fit reason"],
        "risk_reasons": ["AI risk reason"],
    }

    with (
        patch("filmy.routers.web.get_title_presentation", return_value=_presentation()),
        patch("filmy.routers.web.signal_metadata_pipeline"),
        patch("filmy.routers.web.get_tmdb_status", return_value={"assets": {}, "has_mapping": True}),
        patch("filmy.routers.web.get_title_role_signals", return_value=[]),
        patch("filmy.routers.web.get_latest_ai_recommendation_for_title", return_value=ai_recommendation),
        patch("filmy.routers.web.launch_person_presentation_warmup"),
        patch("filmy.routers.web.launch_person_portrait_warmup"),
        patch("filmy.routers.web.get_local_library_status", return_value={"visible_lists": []}),
    ):
        response = client.get("/titles/tt2316411")

    assert response.status_code == 200
    assert "Slovní hodnocení" in response.text
    assert "Moje klady." in response.text
    assert "Moje zapory." in response.text
    assert "AI fit reason" in response.text
    assert "AI risk reason" in response.text
    assert "recommendations.json" in response.text
    assert "Hodnocení AI" in response.text
    assert "jistota high" in response.text
