from __future__ import annotations

from pathlib import Path


def test_record_watched_tracks_row_count_as_integer() -> None:
    schema_sql = Path("migrations/postgresql/002_runtime_schema.sql").read_text(encoding="utf-8")

    assert "v_content_state_rows integer := 0;" in schema_sql
    assert "GET DIAGNOSTICS v_content_state_rows = ROW_COUNT;" in schema_sql
    assert "v_content_state_changed := v_content_state_rows > 0;" in schema_sql
    assert "GET DIAGNOSTICS v_content_state_changed = ROW_COUNT;" not in schema_sql


def test_archive_user_list_group_is_server_side_action() -> None:
    schema_sql = Path("migrations/postgresql/002_runtime_schema.sql").read_text(encoding="utf-8")
    runtime_postgres = Path("filmy/runtime_postgres.py").read_text(encoding="utf-8")
    db_library = Path("filmy/db_library.py").read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION app.archive_user_list_group(" in schema_sql
    assert "RETURNS TABLE(list_found boolean, archived_items integer)" in schema_sql
    assert "COALESCE(episode.series_tconst, item.tconst, item.parent_tconst) = p_display_tconst" in schema_sql
    assert "FROM app.archive_user_list_group(%s, %s, %s::timestamp)" in runtime_postgres
    assert "for item in items:\n        archive_user_list_item(str(list_id)" not in db_library


def test_person_lookup_levenshtein_extension_is_part_of_bootstrap_contract() -> None:
    bootstrap_sql = Path("migrations/postgresql/001_bootstrap.sql").read_text(encoding="utf-8")
    bootstrap_runner = Path("filmy/scripts/bootstrap_postgresql.py").read_text(encoding="utf-8")
    runtime_postgres = Path("filmy/runtime_postgres.py").read_text(encoding="utf-8")

    assert "CREATE EXTENSION IF NOT EXISTS fuzzystrmatch WITH SCHEMA public;" in bootstrap_sql
    assert "('fuzzystrmatch')" in bootstrap_sql
    assert "('fuzzystrmatch')" in bootstrap_runner
    assert "public.levenshtein(%s::text, name_key::text)" in runtime_postgres


def test_database_upgrade_runner_tracks_versioned_steps() -> None:
    upgrade_runner = Path("filmy/scripts/upgrade_database.py").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    install_doc = Path("INSTALACE.md").read_text(encoding="utf-8")

    assert "app.database_upgrades" in upgrade_runner
    assert "SELECT to_regnamespace('app') IS NOT NULL;" in upgrade_runner
    assert "skip 0001-bootstrap" in upgrade_runner
    assert '"0002-runtime-schema", "002_runtime_schema.sql"' in upgrade_runner
    assert '"0005-catalog-grants", "005_catalog_grants.sql"' in upgrade_runner
    assert '"0006-list-actions-session-schema", "006_list_actions_session_schema.sql"' in upgrade_runner
    assert '"0007-list-actions-session-grants", "007_list_actions_session_grants.sql"' in upgrade_runner
    assert '"0008-list-action-rule-seed", "008_list_action_rule_seed.sql"' in upgrade_runner
    assert '"0009-list-action-target-rule-seed", "009_list_action_target_rule_seed.sql"' in upgrade_runner
    assert "filmy-upgrade-database = \"filmy.scripts.upgrade_database:main\"" in pyproject
    assert "uv run filmy-upgrade-database" in install_doc


def test_list_actions_session_schema_upgrade_defines_core_tables_and_triggers() -> None:
    schema_sql = Path("migrations/postgresql/006_list_actions_session_schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS app.list_action_rules (" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS app.title_sessions (" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS app.title_session_actions (" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS app.title_session_effect_queue (" in schema_sql
    assert "list_action_rules_trigger_action_check" in schema_sql
    assert "title_sessions_status_check" in schema_sql
    assert "title_session_actions_session_order_key" in schema_sql
    assert "title_session_effect_queue_effect_status_check" in schema_sql
    assert "trg_list_action_rules_touch_updated_at" in schema_sql
    assert "trg_title_sessions_touch_updated_at" in schema_sql


def test_list_actions_session_grants_upgrade_covers_new_tables() -> None:
    grants_sql = Path("migrations/postgresql/007_list_actions_session_grants.sql").read_text(encoding="utf-8")

    assert "app.list_action_rules" in grants_sql
    assert "app.title_sessions" in grants_sql
    assert "app.title_session_actions" in grants_sql
    assert "app.title_session_effect_queue" in grants_sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE" in grants_sql


def test_list_action_rule_seed_upgrade_covers_current_lists_and_targetless_actions() -> None:
    seed_sql = Path("migrations/postgresql/008_list_action_rule_seed.sql").read_text(encoding="utf-8")

    assert "'watchlist'" in seed_sql
    assert "'koukni-rychle'" in seed_sql
    assert "'plex-library'" in seed_sql
    assert "'stahnout'" in seed_sql
    assert "'set_rating'" in seed_sql
    assert "'mark_watched'" in seed_sql
    assert "'write_rating'" in seed_sql
    assert "'write_watched'" in seed_sql
    assert "'deactivate_source_membership'" in seed_sql
    assert "'preserve_source_membership'" in seed_sql


def test_list_action_target_rule_seed_upgrade_covers_move_and_copy_rules() -> None:
    seed_sql = Path("migrations/postgresql/009_list_action_target_rule_seed.sql").read_text(encoding="utf-8")

    assert "'copy_to_list'" in seed_sql
    assert "'move_to_list'" in seed_sql
    assert "'add_target_membership'" in seed_sql
    assert "'deactivate_source_membership'" in seed_sql
    assert "'watchlist'" not in seed_sql.split("target_lists AS", 1)[1]
    assert "'ai-navrhy'" not in seed_sql.split("target_lists AS", 1)[1]


def test_bootstrap_accepts_pg_database_owner_for_public_schema() -> None:
    bootstrap_sql = Path("migrations/postgresql/001_bootstrap.sql").read_text(encoding="utf-8")
    bootstrap_runner = Path("filmy/scripts/bootstrap_postgresql.py").read_text(encoding="utf-8")

    assert "schema_name = 'public' AND schema_owner = 'pg_database_owner'" in bootstrap_sql
    assert "expected.name = 'public' AND pg_get_userbyid(namespace.nspowner) = 'pg_database_owner'" in bootstrap_sql
    assert "expected.name = 'public' AND pg_get_userbyid(namespace.nspowner) = 'pg_database_owner'" in bootstrap_runner
