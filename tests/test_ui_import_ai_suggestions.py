from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from filmy.routers.web import router


def _file_rows() -> list[dict]:
    return [
        {
            "filename": "recommendations.json",
            "path": "/tmp/recommendations.json",
            "contract_version": 1,
            "created_at": "2026-07-19T13:00:00+02:00",
            "intent": "watch_next",
            "status": "draft_for_review",
            "recommendation_count": 2,
            "checksum": "abc",
            "imported": False,
            "imported_run_id": None,
            "imported_at": None,
            "error": None,
        },
        {
            "filename": "already.json",
            "path": "/tmp/already.json",
            "contract_version": 1,
            "created_at": "2026-07-19T14:00:00+02:00",
            "intent": "external",
            "status": "draft_for_review",
            "recommendation_count": 1,
            "checksum": "def",
            "imported": True,
            "imported_run_id": "ai-rec-run-1",
            "imported_at": datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
            "error": None,
        },
    ]


def test_import_ai_suggestions_page_replaces_import_tools_menu() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with patch("filmy.routers.web.list_ai_recommendation_files", return_value=_file_rows()):
        response = client.get("/system/import-ai-suggestions")

    assert response.status_code == 200
    assert "Import AI suggestions" in response.text
    assert "Import Tools" not in response.text
    assert "recommendations.json" in response.text
    assert "already.json" in response.text
    assert "Smazat" in response.text


def test_import_ai_suggestions_post_routes_selected_file_to_importer() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with (
        patch("filmy.routers.web.list_ai_recommendation_files", return_value=_file_rows()),
        patch(
            "filmy.routers.web.import_ai_recommendations_file",
            return_value={
                "source_filename": "recommendations.json",
                "recommendations": 2,
                "resolved": 2,
                "unresolved": 0,
                "list_inserted": 2,
                "list_updated": 0,
            },
        ) as import_mock,
    ):
        response = client.post(
            "/system/import-ai-suggestions",
            data={"filename": "recommendations.json", "return_to": "/system/import-ai-suggestions"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/system/import-ai-suggestions"
    query = parse_qs(location.query)
    assert query["imported"] == ["1"]
    assert query["recommendations"] == ["2"]
    import_mock.assert_called_once_with("/tmp/recommendations.json")


def test_import_ai_suggestions_delete_routes_filename_to_delete_helper() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with patch(
        "filmy.routers.web.delete_ai_recommendation_file",
        return_value={"deleted": True, "filename": "recommendations.json"},
    ) as delete_mock:
        response = client.post(
            "/system/import-ai-suggestions/delete",
            data={"filename": "recommendations.json", "return_to": "/system/import-ai-suggestions"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/system/import-ai-suggestions"
    query = parse_qs(location.query)
    assert query["deleted"] == ["1"]
    assert query["deleted_filename"] == ["recommendations.json"]
    delete_mock.assert_called_once_with("recommendations.json")
