from __future__ import annotations

from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from filmy.routers.ui import router


def test_title_role_signal_post_routes_to_db_facade() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with (
        patch("filmy.routers.ui.replace_title_role_signals") as replace_signal_mock,
        patch("filmy.routers.ui.signal_metadata_pipeline") as signal_mock,
    ):
        response = client.post(
            "/ui/title-role-signals/set",
            data={
                "tconst": "tt0379623",
                "nconst": "nm0395777",
                "character_name": "Ephram Brown",
                "signal_types": ["dialogue", "attraction"],
                "polarity": "positive",
                "strength": "10",
                "notes": "dialogy a chovani",
                "return_to": "/titles/tt0379623",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/titles/tt0379623"
    replace_signal_mock.assert_called_once_with(
        "tt0379623",
        nconst="nm0395777",
        character_name="Ephram Brown",
        signal_types=["dialogue", "attraction"],
        polarity="positive",
        strength=10,
        notes="dialogy a chovani",
    )
    signal_mock.assert_called_once_with("ui_title_role_signal_set", target_tconst="tt0379623")


def test_title_role_signal_delete_routes_to_db_facade() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with (
        patch("filmy.routers.ui.delete_title_role_signals") as delete_signal_mock,
        patch("filmy.routers.ui.signal_metadata_pipeline") as signal_mock,
    ):
        response = client.post(
            "/ui/title-role-signals/delete",
            data={
                "tconst": "tt0379623",
                "nconst": "nm0395777",
                "character_name": "Ephram Brown",
                "return_to": "/titles/tt0379623",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/titles/tt0379623"
    delete_signal_mock.assert_called_once_with(
        "tt0379623",
        nconst="nm0395777",
        character_name="Ephram Brown",
    )
    signal_mock.assert_called_once_with("ui_title_role_signal_delete", target_tconst="tt0379623")
