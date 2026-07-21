from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from filmy.routers.ui import router as ui_router
from filmy.routers.web import router


def test_home_replaces_continue_watching_with_ai_suggestions() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    library_status = {
        "counts": {"lists": 1},
        "visible_lists": [
            {
                "id": "watchlist",
                "slug": "watchlist",
                "name": "Watchlist",
                "description": "To watch",
                "list_kind": "watchlist",
                "item_count": 0,
                "item_type": "list",
                "ai_input_role": "interested_planned",
            }
        ],
    }
    ai_page = {
        "list": {
            "id": "ai-suggestions",
            "slug": "ai-navrhy",
            "name": "AI návrhy",
            "list_kind": "custom",
        },
        "total": 1,
        "limit": 24,
        "offset": 0,
        "items": [
            {
                "tconst": "tt4643084",
                "title": "Counterpart",
                "title_type": "tvSeries",
                "year": 2017,
                "poster_url": "/assets/tmdb/tt4643084/poster.jpg",
                "series_title": "Counterpart",
                "season_number": None,
                "episode_number": None,
                "notes": "AI fit reason",
                "added_at": datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
            }
        ],
    }

    with (
        patch("filmy.routers.web.get_local_library_status", return_value=library_status),
        patch(
            "filmy.routers.web.selected_panel_page",
            return_value={"items": [], "total": 0, "limit": 50, "offset": 0, "list": None},
        ),
        patch("filmy.routers.web.get_user_list_items_page", return_value=ai_page) as list_page_mock,
        patch("filmy.routers.web.get_favorite_traits", return_value=[]),
        patch("filmy.routers.web.get_latest_genre_scores", return_value=None),
        patch("filmy.routers.web.background_supervisor.homepage_snapshot", return_value={}),
        patch("filmy.routers.web.launch_homepage_warmup"),
    ):
        response = client.get("/")

    assert response.status_code == 200
    assert "AI návrhy" in response.text
    assert "Vybrané tipy z importovaných AI doporučení." in response.text
    assert "Counterpart" in response.text
    assert "AI fit reason" not in response.text
    assert "Continue Watching" not in response.text
    list_page_mock.assert_called_once_with("ai-suggestions", limit=24)


def test_ai_suggestions_list_detail_shows_clear_button() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    ai_page = {
        "list": {
            "id": "ai-suggestions",
            "slug": "ai-navrhy",
            "name": "AI návrhy",
            "description": "Inbox",
            "list_kind": "custom",
            "ai_input_role": "external_suggestion",
        },
        "total": 0,
        "limit": 50,
        "offset": 0,
        "items": [],
    }

    with (
        patch("filmy.routers.web.get_user_list_items_page", return_value=ai_page),
        patch("filmy.routers.web.get_local_library_status", return_value={"visible_lists": []}),
    ):
        response = client.get("/lists/ai-suggestions")

    assert response.status_code == 200
    assert 'action="/ui/lists/clear-ai-suggestions"' in response.text
    assert "Vyčistit" in response.text


def test_regular_list_detail_does_not_show_clear_button() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    list_page = {
        "list": {
            "id": "watchlist",
            "slug": "watchlist",
            "name": "Watchlist",
            "description": "To watch",
            "list_kind": "watchlist",
            "ai_input_role": "interested_planned",
        },
        "total": 0,
        "limit": 50,
        "offset": 0,
        "items": [],
    }

    with (
        patch("filmy.routers.web.get_user_list_items_page", return_value=list_page),
        patch("filmy.routers.web.get_local_library_status", return_value={"visible_lists": []}),
    ):
        response = client.get("/lists/watchlist")

    assert response.status_code == 200
    assert 'action="/ui/lists/clear-ai-suggestions"' not in response.text
    assert "Vyčistit" not in response.text


def test_clear_ai_suggestions_route_redirects_back() -> None:
    app = FastAPI()
    app.include_router(ui_router)
    client = TestClient(app)

    with (
        patch("filmy.routers.ui.clear_ai_suggestions_list_items", return_value={"deleted_items": 17}) as clear_mock,
        patch("filmy.routers.ui.signal_metadata_pipeline") as signal_mock,
    ):
        response = client.post(
            "/ui/lists/clear-ai-suggestions",
            data={"return_to": "/lists/ai-suggestions?page=1"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/lists/ai-suggestions?page=1")
    clear_mock.assert_called_once_with()
    signal_mock.assert_called_once_with("ui_clear_ai_suggestions")
