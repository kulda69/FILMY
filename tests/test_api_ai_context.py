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
            "rating_scales": {"user_rating": {"min": 1, "max": 10}},
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
    assert payload["favorite_genres"][0]["genre"] == "Drama"
    assert payload["favorite_traits"][0]["trait"] == "slow-burn"
