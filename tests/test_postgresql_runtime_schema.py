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
