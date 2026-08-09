from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from filmy.routers.api import router


def test_ai_context_endpoint_returns_context_payload() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with patch(
        "filmy.routers.api.get_ai_context",
        return_value={
            "contract_version": 1,
            "rating_scales": {
                "user_rating": {"min": 1, "max": 10},
                "title_role_signal_strength": {"min": 0, "max": 10},
            },
            "title_role_signal_definitions": {
                "signal_types": {"attraction": "Pritazlivost nebo charisma role."},
                "polarities": {"positive": "Pozitivni signal."},
                "notes": "Textovy kontext.",
            },
            "favorite_genres": [{"genre": "Drama"}],
            "favorite_traits": [{"trait": "slow-burn"}],
            "score_signal_notes": {},
            "usage_notes": [],
        },
    ):
        response = client.get("/api/ai/context")

    assert response.status_code == 200
    payload = response.json()
    assert payload["contract_version"] == 1
    assert payload["rating_scales"]["user_rating"] == {"min": 1, "max": 10}
    assert payload["rating_scales"]["title_role_signal_strength"] == {"min": 0, "max": 10}
    assert "attraction" in payload["title_role_signal_definitions"]["signal_types"]
    assert payload["favorite_genres"][0]["genre"] == "Drama"
    assert payload["favorite_traits"][0]["trait"] == "slow-burn"


def test_ai_taste_seed_endpoint_returns_people_affinity_payload() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with patch(
        "filmy.routers.api.get_ai_taste_seed",
        return_value={
            "source_list": {"query": "kouknout-znovu", "found": True},
            "limit": 2,
            "items": [
                {
                    "imdb_id": "tt0133093",
                    "tconst": "tt0133093",
                    "title": "The Matrix",
                    "actor_affinity_rating": 9.0,
                    "people_affinity": [
                        {
                            "nconst": "nm0000206",
                            "name": "Keanu Reeves",
                            "credit_group": "cast",
                            "ordering": 1,
                            "affinity_rating": 9,
                            "is_favorite": True,
                        }
                    ],
                    "title_role_signals": [
                        {
                            "signal_key": "role-signal:tt0133093:nm0000206:neo:attraction",
                            "nconst": "nm0000206",
                            "person_name": "Keanu Reeves",
                            "character_name": "Neo",
                            "signal_type": "attraction",
                            "polarity": "positive",
                            "strength": 9,
                            "notes": "visualni pritazlivost role",
                        }
                    ],
                    "genre_score_signals": [],
                }
            ],
        },
    ) as taste_seed_mock:
        response = client.get("/api/ai/taste-seed?source_list=kouknout-znovu&limit=2")

    assert response.status_code == 200
    taste_seed_mock.assert_called_once_with(source_list="kouknout-znovu", limit=2)
    payload = response.json()
    assert payload["items"][0]["people_affinity"][0]["name"] == "Keanu Reeves"
    assert payload["items"][0]["people_affinity"][0]["affinity_rating"] == 9
    assert payload["items"][0]["title_role_signals"][0]["signal_type"] == "attraction"
    assert payload["items"][0]["title_role_signals"][0]["strength"] == 9


def test_ai_rated_titles_endpoint_routes_filters() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with patch(
        "filmy.routers.api.get_ai_rated_titles",
        return_value={
            "filters": {"min_user_rating": 8, "title_type": "movie"},
            "limit": 3,
            "items": [
                {
                    "imdb_id": "tt0133093",
                    "tconst": "tt0133093",
                    "title": "The Matrix",
                    "user_rating": 9,
                    "title_role_signals": [],
                    "people_affinity": [],
                    "genre_score_signals": [],
                }
            ],
        },
    ) as rated_titles_mock:
        response = client.get("/api/ai/rated-titles?min_user_rating=8&limit=3&title_type=movie")

    assert response.status_code == 200
    rated_titles_mock.assert_called_once_with(min_user_rating=8, limit=3, title_type="movie")
    payload = response.json()
    assert payload["filters"]["min_user_rating"] == 8
    assert payload["items"][0]["user_rating"] == 9


def test_ai_noted_titles_endpoint_routes_filters() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with patch(
        "filmy.routers.api.get_ai_noted_titles",
        return_value={
            "filters": {"notes": "liked", "min_user_rating": 7},
            "limit": 5,
            "items": [
                {
                    "imdb_id": "tt0242795",
                    "tconst": "tt0242795",
                    "title": "Everwood",
                    "user_rating": 7,
                    "liked_notes": "Ephram jako postava a dialogy.",
                    "disliked_notes": None,
                    "title_role_signals": [],
                    "people_affinity": [],
                    "genre_score_signals": [],
                }
            ],
        },
    ) as noted_titles_mock:
        response = client.get("/api/ai/noted-titles?notes=liked&min_user_rating=7&limit=5")

    assert response.status_code == 200
    noted_titles_mock.assert_called_once_with(notes="liked", min_user_rating=7, limit=5)
    payload = response.json()
    assert payload["filters"]["notes"] == "liked"
    assert payload["items"][0]["liked_notes"] == "Ephram jako postava a dialogy."


def test_ai_watched_titles_endpoint_always_routes_complete_blacklist() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with patch(
        "filmy.routers.api.get_ai_watched_titles",
        return_value={
            "contract_version": 2,
            "filters": {
                "mode": "complete_hard_blacklist",
                "include_rated": True,
                "include_negative": True,
                "include_in_progress": True,
                "include_strong_positive": True,
            },
            "item_count": 1,
            "unresolved_item_count": 0,
            "source_counts": {"watch_event": 1},
            "items": [
                {
                    "imdb_id": "tt0242795",
                    "tconst": "tt0242795",
                    "tmdb_id": 1953,
                    "title": "Everwood",
                    "sources": ["watch_event"],
                }
            ],
        },
    ) as watched_titles_mock:
        response = client.get("/api/ai/watched-titles?include_rated=false&include_negative=false")

    assert response.status_code == 200
    watched_titles_mock.assert_called_once_with()
    payload = response.json()
    assert payload["contract_version"] == 2
    assert payload["filters"]["mode"] == "complete_hard_blacklist"
    assert payload["item_count"] == 1
    assert payload["unresolved_item_count"] == 0
    assert payload["items"][0]["tconst"] == "tt0242795"
    assert payload["source_counts"]["watch_event"] == 1


def test_ai_taste_inputs_endpoint_routes_limit() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with patch(
        "filmy.routers.api.get_ai_taste_inputs",
        return_value={
            "contract_version": 1,
            "limit_per_list": 4,
            "included_roles": ["strong_positive"],
            "excluded_roles": ["external_suggestion", "ignore"],
            "groups": {"strong_positive": []},
            "excluded_sources": [],
        },
    ) as taste_inputs_mock:
        response = client.get("/api/ai/taste-inputs?limit_per_list=4")

    assert response.status_code == 200
    taste_inputs_mock.assert_called_once_with(limit_per_list=4)
    payload = response.json()
    assert payload["limit_per_list"] == 4
    assert "external_suggestion" in payload["excluded_roles"]


def test_ai_scoring_explainer_endpoint_returns_scoring_semantics() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with patch(
        "filmy.routers.api.get_ai_scoring_explainer",
        return_value={
            "contract_version": 1,
            "score_scope": "default",
            "signals": {
                "title_role_signals": {
                    "current_scoring_inclusion": False,
                    "meaning": "Samostatna vrstva.",
                }
            },
            "known_limitations": ["Title role signals zatim nejsou zapocitane."],
        },
    ) as explainer_mock:
        response = client.get("/api/ai/scoring-explainer")

    assert response.status_code == 200
    explainer_mock.assert_called_once_with()
    payload = response.json()
    assert payload["score_scope"] == "default"
    assert payload["signals"]["title_role_signals"]["current_scoring_inclusion"] is False
