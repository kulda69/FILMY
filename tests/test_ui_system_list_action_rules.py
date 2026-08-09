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


def test_system_list_action_rules_detail_shows_only_simple_editor() -> None:
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
    assert "Moje pravidla" in response.text
    assert "Zatím tu nemáš žádné vlastní pravidlo" in response.text
    assert "Přidat pravidlo" in response.text
    assert "Vyber akci" in response.text
    assert "Členství v původním seznamu" in response.text
    assert "Nevybrán" in response.text
    assert "Jakýkoli" in response.text
    assert "Stáhnout" in response.text
    assert "Immediate" not in response.text
    assert "Write rating" not in response.text
    assert "Add target membership" not in response.text


def test_system_list_action_rules_creates_simple_rating_rule() -> None:
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
    seeded_rule = {
        "rule_id": "rule:watchlist:set_rating:write_rating",
        "trigger_action": "set_rating",
        "target_list_id": None,
        "effect_type": "write_rating",
        "phase": "immediate",
        "order_index": 10,
        "enabled": True,
        "lock_reason_key": None,
        "lock_reason_text": None,
        "effect_params": {},
    }

    with (
        patch("filmy.routers.web_system.fetch_user_list", return_value=source_list),
        patch("filmy.routers.web_system.fetch_list_action_rules", return_value=[seeded_rule]),
        patch("filmy.routers.web_system.upsert_list_action_rule", return_value={"rule_id": "ok"}) as upsert_mock,
    ):
        response = client.post(
            "/system/list-action-rules/watchlist/simple-rules/create",
            data={
                "simple_action": "set_rating",
                "target_list_id": "",
                "source_membership": "deactivate",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/system/list-action-rules/watchlist?saved=created"
    calls = [call.kwargs for call in upsert_mock.call_args_list]
    assert calls[0]["rule_id"] == "rule:watchlist:set_rating:write_rating"
    assert calls[0]["enabled"] is False
    assert {call["effect_type"] for call in calls[1:]} == {
        "write_rating",
        "derive_watched",
        "write_watched",
        "deactivate_source_membership",
    }
    assert next(call for call in calls[1:] if call["effect_type"] == "write_watched")["phase"] == "finalize_only"
    assert next(call for call in calls[1:] if call["effect_type"] == "write_rating")["order_index"] == 20


def test_system_list_action_rules_avoids_disabled_seed_order_collision() -> None:
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
    seeded_rule = {
        "rule_id": "rule:watchlist:mark_watched:write_watched",
        "trigger_action": "mark_watched",
        "target_list_id": None,
        "effect_type": "write_watched",
        "phase": "finalize_only",
        "order_index": 10,
        "enabled": False,
        "lock_reason_key": None,
        "lock_reason_text": None,
        "effect_params": {},
    }

    with (
        patch("filmy.routers.web_system.fetch_user_list", return_value=source_list),
        patch("filmy.routers.web_system.fetch_list_action_rules", return_value=[seeded_rule]),
        patch("filmy.routers.web_system.upsert_list_action_rule", return_value={"rule_id": "ok"}) as upsert_mock,
    ):
        response = client.post(
            "/system/list-action-rules/watchlist/simple-rules/create",
            data={"simple_action": "mark_watched", "source_membership": "deactivate"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    calls = [call.kwargs for call in upsert_mock.call_args_list]
    created_calls = calls[1:]
    assert [(call["effect_type"], call["order_index"]) for call in created_calls] == [
        ("write_watched", 20),
        ("deactivate_source_membership", 30),
    ]


def test_system_list_action_rules_renders_edit_and_delete_for_simple_rule() -> None:
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
    simple_rules = [
        {
            "trigger_action": "move_to_list",
            "rules": [
                {
                    "rule_id": "rule:simple:group-1:add_target_membership",
                    "target_list_id": None,
                    "target_list_name": None,
                    "effect_type": "add_target_membership",
                    "is_enabled": True,
                    "is_locked": False,
                },
                {
                    "rule_id": "rule:simple:group-1:deactivate_source_membership",
                    "target_list_id": None,
                    "target_list_name": None,
                    "effect_type": "deactivate_source_membership",
                    "is_enabled": True,
                    "is_locked": False,
                },
            ],
        }
    ]

    with (
        patch("filmy.routers.web_system.fetch_user_list", return_value=source_list),
        patch("filmy.routers.web_system.fetch_user_lists", return_value=[source_list]),
        patch("filmy.routers.web_system._group_rules_for_list", return_value=simple_rules),
    ):
        response = client.get("/system/list-action-rules/watchlist")

    assert response.status_code == 200
    assert "Cílový seznam: Jakýkoli" in response.text
    assert ">Upravit</button>" in response.text
    assert ">Smazat</button>" in response.text
    assert "/simple-rules/group-1/update" in response.text
    assert "/simple-rules/group-1/delete" in response.text


def test_system_list_action_rules_creates_any_target_move_rule() -> None:
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

    with (
        patch("filmy.routers.web_system.fetch_user_list", return_value=source_list),
        patch("filmy.routers.web_system.fetch_list_action_rules", return_value=[]),
        patch("filmy.routers.web_system.upsert_list_action_rule", return_value={"rule_id": "ok"}) as upsert_mock,
    ):
        response = client.post(
            "/system/list-action-rules/watchlist/simple-rules/create",
            data={
                "simple_action": "target_list",
                "target_list_id": "__any__",
                "source_membership": "deactivate",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    calls = [call.kwargs for call in upsert_mock.call_args_list]
    assert {call["effect_type"] for call in calls} == {
        "add_target_membership",
        "deactivate_source_membership",
    }
    assert all(call["trigger_action"] == "move_to_list" for call in calls)
    assert all(call["target_list_id"] is None for call in calls)
    assert all(call["effect_params"]["any_target"] is True for call in calls)


def test_system_list_action_rules_updates_whole_simple_rule_group() -> None:
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
    existing_rules = [
        {
            "rule_id": "rule:simple:group-1:write_watched",
            "source_list_id": "watchlist",
            "trigger_action": "mark_watched",
            "target_list_id": None,
            "effect_type": "write_watched",
            "phase": "finalize_only",
            "order_index": 20,
            "enabled": True,
            "lock_reason_key": None,
            "lock_reason_text": None,
            "effect_params": {"simple_rule": True},
        },
        {
            "rule_id": "rule:simple:group-1:deactivate_source_membership",
            "source_list_id": "watchlist",
            "trigger_action": "mark_watched",
            "target_list_id": None,
            "effect_type": "deactivate_source_membership",
            "phase": "finalize_only",
            "order_index": 30,
            "enabled": True,
            "lock_reason_key": None,
            "lock_reason_text": None,
            "effect_params": {"simple_rule": True},
        },
    ]

    with (
        patch("filmy.routers.web_system.fetch_user_list", return_value=source_list),
        patch(
            "filmy.routers.web_system.fetch_list_action_rules",
            side_effect=[existing_rules, existing_rules, []],
        ),
        patch("filmy.routers.web_system.delete_list_action_rule", return_value=True) as delete_mock,
        patch("filmy.routers.web_system.upsert_list_action_rule", return_value={"rule_id": "ok"}) as upsert_mock,
    ):
        response = client.post(
            "/system/list-action-rules/watchlist/simple-rules/group-1/update",
            data={
                "simple_action": "target_list",
                "target_list_id": "__any__",
                "source_membership": "deactivate",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/system/list-action-rules/watchlist?saved=updated"
    assert delete_mock.call_count == 2
    assert {call.kwargs["effect_type"] for call in upsert_mock.call_args_list} == {
        "add_target_membership",
        "deactivate_source_membership",
    }


def test_system_list_action_rules_deletes_whole_simple_rule_group() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    source_list = {"id": "watchlist"}
    existing_rules = [
        {"rule_id": "rule:simple:group-1:write_watched"},
        {"rule_id": "rule:simple:group-1:deactivate_source_membership"},
        {"rule_id": "rule:simple:other:write_watched"},
    ]

    with (
        patch("filmy.routers.web_system.fetch_user_list", return_value=source_list),
        patch("filmy.routers.web_system.fetch_list_action_rules", return_value=existing_rules),
        patch("filmy.routers.web_system.delete_list_action_rule", return_value=True) as delete_mock,
    ):
        response = client.post(
            "/system/list-action-rules/watchlist/simple-rules/group-1/delete",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/system/list-action-rules/watchlist?saved=deleted"
    assert [call.args[0] for call in delete_mock.call_args_list] == [
        "rule:simple:group-1:write_watched",
        "rule:simple:group-1:deactivate_source_membership",
    ]


def test_system_list_action_rules_does_not_multiply_breadcrumbs() -> None:
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

    with (
        patch("filmy.routers.web_system.fetch_user_list", return_value=source_list),
        patch("filmy.routers.web_system.fetch_user_lists", return_value=[source_list]),
        patch("filmy.routers.web_system._group_rules_for_list", return_value=[]),
    ):
        response = client.get(
            "/system/list-action-rules/watchlist",
            params={"return_to": "/system/list-action-rules/watchlist?return_to=/system/list-action-rules"},
        )

    assert response.status_code == 200
    breadcrumb_html = response.text.split('<nav aria-label="Breadcrumb">', 1)[1].split("</nav>", 1)[0]
    assert breadcrumb_html.count(">List Action Rules</a>") == 1
    assert '<li class="breadcrumb-item active text-secondary" aria-current="page">Watchlist</li>' in response.text
    assert 'name="return_to" value="/system/list-action-rules"' in response.text


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
