from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg

from filmy.routers.web import router


def test_system_list_action_rules_overview_renders_roomy_list() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    list_rows = [
        {
            "id": "watchlist",
            "slug": "watchlist",
            "name": "Watchlist",
            "description": "To watch",
            "list_kind": "watchlist",
            "ai_input_role": "interested_planned",
        },
        {
            "id": "stahnout",
            "slug": "stahnout",
            "name": "Stáhnout",
            "description": "Silné kusy ke stažení",
            "list_kind": "custom",
            "ai_input_role": "interested_owned",
        },
    ]
    rules_by_list = {
        "watchlist": [
            {"rule_id": "r1", "enabled": True, "updated_at": datetime(2026, 7, 29, 10, 0, 0), "lock_reason_key": None, "lock_reason_text": None},
            {"rule_id": "r2", "enabled": True, "updated_at": datetime(2026, 7, 29, 10, 5, 0), "lock_reason_key": None, "lock_reason_text": None},
        ],
        "stahnout": [
            {"rule_id": "r3", "enabled": False, "updated_at": datetime(2026, 7, 29, 11, 0, 0), "lock_reason_key": "disabled", "lock_reason_text": "Disabled for now"},
        ],
    }

    with (
        patch("filmy.routers.web_system.fetch_user_lists", return_value=list_rows),
        patch("filmy.routers.web_system.fetch_list_action_rules", side_effect=lambda **kwargs: rules_by_list[kwargs["source_list_id"]]),
    ):
        response = client.get("/system/list-action-rules")

    assert response.status_code == 200
    assert "List Action Rules" in response.text
    assert "Watchlist" in response.text
    assert "Stáhnout" in response.text
    assert "Open editor" in response.text
    assert "3 rules" in response.text
    assert "1 locked" in response.text


def test_system_list_action_rules_detail_groups_rules_by_trigger() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    source_list = {
        "id": "watchlist",
        "slug": "watchlist",
        "name": "Watchlist",
        "description": "To watch",
        "list_kind": "watchlist",
        "ai_input_role": "interested_planned",
    }
    all_lists = [
        source_list,
        {
            "id": "stahnout",
            "slug": "stahnout",
            "name": "Stáhnout",
            "description": "Silné kusy ke stažení",
            "list_kind": "custom",
            "ai_input_role": "interested_owned",
        },
    ]

    def rules_for_trigger(*, source_list_id: str, trigger_action: str | None = None, **_: object) -> list[dict[str, object]]:
        assert source_list_id == "watchlist"
        if trigger_action == "set_rating":
            return [
                {
                    "rule_id": "rule:set-rating",
                    "effect_type": "write_rating",
                    "phase": "immediate",
                    "target_list_id": None,
                    "enabled": True,
                    "updated_at": datetime(2026, 7, 29, 11, 0, 0),
                    "lock_reason_key": None,
                    "lock_reason_text": None,
                    "order_index": 10,
                }
            ]
        if trigger_action == "copy_to_list":
            return [
                {
                    "rule_id": "rule:copy",
                    "effect_type": "add_target_membership",
                    "phase": "immediate",
                    "target_list_id": "stahnout",
                    "enabled": True,
                    "updated_at": datetime(2026, 7, 29, 11, 5, 0),
                    "lock_reason_key": None,
                    "lock_reason_text": None,
                    "order_index": 10,
                }
            ]
        return []

    with (
        patch("filmy.routers.web_system.fetch_user_list", return_value=source_list),
        patch("filmy.routers.web_system.fetch_user_lists", return_value=all_lists),
        patch("filmy.routers.web_system.fetch_list_action_rules", side_effect=rules_for_trigger),
    ):
        response = client.get("/system/list-action-rules/watchlist")

    assert response.status_code == 200
    assert "Watchlist" in response.text
    assert "Set Rating" in response.text
    assert "Copy To List" in response.text
    assert "Stáhnout" in response.text
    assert "Write rating" in response.text
    assert "Add target membership" in response.text
    assert "Přidat nový řádek" in response.text
    assert "Uložit řádek" in response.text


def test_system_list_action_rules_overview_handles_missing_db_upgrade() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with (
        patch("filmy.routers.web_system.fetch_user_lists", return_value=[{"id": "watchlist", "name": "Watchlist", "list_kind": "watchlist", "slug": "watchlist", "description": "To watch", "ai_input_role": "interested_planned"}]),
        patch("filmy.routers.web_system.fetch_list_action_rules", side_effect=psycopg.errors.UndefinedTable("relation app.list_action_rules does not exist")),
    ):
        response = client.get("/system/list-action-rules")

    assert response.status_code == 200
    assert "app.list_action_rules" in response.text
    assert "filmy-upgrade-database" in response.text


def test_system_list_action_rules_create_redirects_with_success() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    source_list = {
        "id": "watchlist",
        "slug": "watchlist",
        "name": "Watchlist",
        "description": "To watch",
        "list_kind": "watchlist",
        "ai_input_role": "interested_planned",
    }
    target_list = {
        "id": "stahnout",
        "slug": "stahnout",
        "name": "Stáhnout",
        "description": "Silné kusy ke stažení",
        "list_kind": "custom",
        "ai_input_role": "interested_owned",
    }

    with (
        patch("filmy.routers.web_system.fetch_user_list", side_effect=lambda list_id: source_list if list_id == "watchlist" else target_list if list_id == "stahnout" else None),
        patch("filmy.routers.web_system.upsert_list_action_rule", return_value={"rule_id": "rule:new"}) as upsert_mock,
    ):
        response = client.post(
            "/system/list-action-rules/watchlist/rules/create",
            data={
                "trigger_action": "copy_to_list",
                "target_list_id": "stahnout",
                "effect_type": "add_target_membership",
                "phase": "finalize_only",
                "order_index": "30",
                "enabled": "true",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/system/list-action-rules/watchlist?saved=created"
    upsert_mock.assert_called_once()


def test_system_list_action_rules_create_rejects_watchlist_target() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    source_list = {
        "id": "stahnout",
        "slug": "stahnout",
        "name": "Stáhnout",
        "description": "Silné kusy ke stažení",
        "list_kind": "custom",
        "ai_input_role": "interested_planned",
    }
    blocked_target = {
        "id": "watchlist",
        "slug": "watchlist",
        "name": "Watchlist",
        "description": "To watch",
        "list_kind": "watchlist",
        "ai_input_role": "interested_planned",
    }

    with patch(
        "filmy.routers.web_system.fetch_user_list",
        side_effect=lambda list_id: source_list if list_id == "stahnout" else blocked_target if list_id == "watchlist" else None,
    ):
        response = client.post(
            "/system/list-action-rules/stahnout/rules/create",
            data={
                "trigger_action": "copy_to_list",
                "target_list_id": "watchlist",
                "effect_type": "add_target_membership",
                "phase": "immediate",
                "order_index": "10",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "Watchlistu" in response.headers["location"]
