"""Migruje app-state vrstvu FILMY z DuckDB do PostgreSQL.

Zdrojova DuckDB zustava beze zmeny. Data se nejprve exportuji do docasnych CSV,
v PostgreSQL se nactou do TEMP tabulek a teprve potom se v jedne transakci
nahradi cilovy obsah. Opakovany beh tedy nevytvari duplicity ani mezistav.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile

import duckdb
from dotenv import dotenv_values

from filmy.paths import DB_PATH
from filmy.scripts.bootstrap_postgresql import (
    PostgreSQLConfig,
    _assert_secure_env_file,
    _resolve_psql,
    check as check_bootstrap,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
SCHEMA_MIGRATION = PROJECT_ROOT / "migrations" / "postgresql" / "002_runtime_schema.sql"
GRANTS_MIGRATION = PROJECT_ROOT / "migrations" / "postgresql" / "003_runtime_grants.sql"
APP_ROLE = "filmy_app"
TARGET_DATABASE = "filmy"
LEGACY_RUNTIME_TABLES = (
    "user_lists",
    "user_list_items",
    "watch_events",
    "user_ratings",
    "content_state",
    "user_people",
)
OPTIONAL_RUNTIME_VIEWS = (
    "latest_title_posters",
    "catalog_title_cards",
    "watched_display_rollup",
    "active_user_list_display_items",
)
OPTIONAL_RUNTIME_TRIGGERS = (
    "user_lists.trg_user_lists_touch_updated_at",
    "user_list_items.trg_user_list_items_touch_updated_at",
    "user_ratings.trg_user_ratings_touch_updated_at",
    "user_people.trg_user_people_touch_updated_at",
    "user_title_role_signals.trg_user_title_role_signals_touch_updated_at",
    "favorite_genres.trg_favorite_genres_touch_updated_at",
    "favorite_traits.trg_favorite_traits_touch_updated_at",
)
APP_READ_ONLY_RELATIONS = (
    "catalog_titles",
    "catalog_episodes",
    "title_aliases",
    "catalog_people",
    "title_credits",
    "title_alias_lookup",
    "title_lookup",
    "person_lookup",
    "latest_title_posters",
    "catalog_title_cards",
    "watched_display_rollup",
    "active_user_list_display_items",
)
CATALOG_APP_TABLES = (
    "catalog_titles",
    "catalog_episodes",
    "title_aliases",
    "catalog_people",
    "title_credits",
    "title_alias_lookup",
    "title_lookup",
    "person_lookup",
)
RAW_READ_ONLY_RELATIONS = (
    "title_episode",
    "title_principals",
)
OLD_RUNTIME_TABLES = (
    "trakt_sync_runs",
    "trakt_sync_files",
    "trakt_history_events",
    "trakt_ratings",
    "trakt_lists",
    "trakt_list_items",
    "trakt_collection_items",
    "trakt_history_snapshot",
    "trakt_ratings_snapshot",
    "trakt_list_items_snapshot",
    "trakt_collection_snapshot",
    "imdb_list_sync_runs",
    "imdb_watchlist_items",
    "imdb_favorite_people",
    "plex_sync_runs",
    "plex_library_items",
)
ALLOWED_FUNCTION_EXECUTE_PRIVILEGES = (
    "app.normalize_match_key",
    "app.alias_priority",
)

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "imdb_file_manifest": (
        "source_key", "source_path", "source_mtime", "source_size", "source_sha256", "recorded_at",
    ),
    "catalog_refresh_meta": (
        "source_key", "fingerprint",
    ),
    "tmdb_title_map": (
        "tconst", "tmdb_media_type", "tmdb_id", "matched_by", "matched_at", "sync_status", "last_error",
    ),
    "tmdb_title_details": (
        "tconst", "locale", "display_title", "original_title", "overview", "poster_path",
        "backdrop_path", "release_date", "genres_json", "raw_json", "synced_at",
    ),
    "tmdb_watch_providers": (
        "tconst", "country_code", "provider_type", "provider_id", "provider_name", "logo_path",
        "display_priority", "synced_at",
    ),
    "tmdb_assets": (
        "id", "tconst", "asset_kind", "relative_path", "local_path", "fetch_reason",
        "status", "sha256", "fetched_at",
    ),
    "import_batches": (
        "id", "source", "filename", "checksum", "status", "created_at",
    ),
    "import_rows": (
        "id", "batch_id", "source", "row_number", "raw_json", "parsed_title", "parsed_year",
        "parsed_watched_on", "parsed_season_number", "parsed_episode_number", "parsed_imdb_id",
        "parsed_tmdb_id", "resolution_status", "resolved_tconst", "resolution_confidence", "resolution_note",
    ),
    "local_seed_meta": (
        "seed_name", "seeded_at", "note",
    ),
    "user_lists": (
        "id", "slug", "name", "list_kind", "source_origin", "source_ref",
        "created_at", "updated_at", "description", "ai_input_role",
    ),
    "user_list_items": (
        "id", "list_id", "canonical_key", "tconst", "media_type", "imdb_id",
        "tmdb_id", "trakt_id", "parent_tconst", "parent_title", "title",
        "season_number", "episode_number", "rank", "added_at", "notes",
        "source_origin", "source_ref", "is_archived", "created_at", "updated_at",
    ),
    "watch_events": (
        "id", "tconst", "event_scope", "watched_on", "source", "batch_id",
        "import_row_id", "rating", "notes", "created_at",
    ),
    "user_ratings": (
        "canonical_key", "tconst", "media_type", "imdb_id", "tmdb_id",
        "trakt_id", "parent_tconst", "parent_title", "title", "season_number",
        "episode_number", "rating", "rated_at", "source_origin", "source_ref",
        "created_at", "updated_at", "liked_notes", "disliked_notes",
    ),
    "content_state": (
        "tconst", "interest_state", "last_previewed_at", "last_watched_at", "updated_at",
    ),
    "user_people": (
        "person_key", "nconst", "name", "known_for", "birth_date", "source_origin",
        "source_ref", "is_favorite", "created_at", "updated_at", "affinity_rating",
    ),
    "user_title_role_signals": (
        "signal_key", "tconst", "nconst", "character_name", "signal_type", "polarity",
        "strength", "notes", "source_origin", "source_ref", "created_at", "updated_at",
    ),
    "favorite_genres": (
        "genre", "weight", "preference_rank", "source_origin", "source_ref", "notes",
        "is_active", "created_at", "updated_at",
    ),
    "favorite_traits": (
        "trait", "weight", "preference_rank", "source_origin", "source_ref", "notes",
        "is_active", "created_at", "updated_at",
    ),
    "genre_scores": (
        "id", "genre", "generated_at", "algorithm_version", "score_scope", "source_origin",
        "source_ref", "titles_considered", "watched_titles_considered", "rated_titles_considered",
        "contributing_titles_json", "excluded_titles_json", "favorite_genre_weight",
        "preference_overlap_score", "preference_alignment_score", "affinity_score",
        "rating_signal_score", "watch_signal_score", "recency_score", "actor_affinity_score",
        "frequency_score", "consistency_score", "novelty_score", "confidence_score",
        "manual_adjustment_score", "final_score", "normalized_score", "rank_in_run",
        "metrics_json", "explanation", "created_at",
    ),
    "search_recall": (
        "id", "entity_type", "query_text", "query_text_fold", "query_key", "target_id",
        "target_label", "target_title_type", "matched_alias_title", "fuzzy_score",
        "first_searched_at", "last_searched_at", "hit_count",
    ),
}

EXPECTED_COLUMNS: dict[str, tuple[tuple[str, str, bool, str], ...]] = {
    "imdb_file_manifest": (
        ("source_key", "text", True, ""), ("source_path", "text", True, ""),
        ("source_mtime", "bigint", True, ""), ("source_size", "bigint", True, ""),
        ("source_sha256", "text", True, ""), ("recorded_at", "timestamp without time zone", True, ""),
    ),
    "catalog_refresh_meta": (
        ("source_key", "text", True, ""), ("fingerprint", "text", True, ""),
    ),
    "tmdb_title_map": (
        ("tconst", "text", True, ""), ("tmdb_media_type", "text", True, ""),
        ("tmdb_id", "bigint", True, ""), ("matched_by", "text", True, ""),
        ("matched_at", "timestamp without time zone", True, ""),
        ("sync_status", "text", True, ""), ("last_error", "text", False, ""),
    ),
    "tmdb_title_details": (
        ("tconst", "text", True, ""), ("locale", "text", True, ""),
        ("display_title", "text", False, ""), ("original_title", "text", False, ""),
        ("overview", "text", False, ""), ("poster_path", "text", False, ""),
        ("backdrop_path", "text", False, ""), ("release_date", "text", False, ""),
        ("genres_json", "text", False, ""), ("raw_json", "text", True, ""),
        ("synced_at", "timestamp without time zone", True, ""),
    ),
    "tmdb_watch_providers": (
        ("tconst", "text", True, ""), ("country_code", "text", True, ""),
        ("provider_type", "text", True, ""), ("provider_id", "bigint", True, ""),
        ("provider_name", "text", False, ""), ("logo_path", "text", False, ""),
        ("display_priority", "integer", False, ""), ("synced_at", "timestamp without time zone", True, ""),
    ),
    "tmdb_assets": (
        ("id", "text", True, ""), ("tconst", "text", True, ""),
        ("asset_kind", "text", True, ""), ("relative_path", "text", True, ""),
        ("local_path", "text", True, ""), ("fetch_reason", "text", True, ""),
        ("status", "text", True, ""), ("sha256", "text", False, ""),
        ("fetched_at", "timestamp without time zone", True, ""),
    ),
    "import_batches": (
        ("id", "text", True, ""), ("source", "text", True, ""),
        ("filename", "text", True, ""), ("checksum", "text", True, ""),
        ("status", "text", True, ""), ("created_at", "timestamp without time zone", True, ""),
    ),
    "import_rows": (
        ("id", "text", True, ""), ("batch_id", "text", True, ""),
        ("source", "text", True, ""), ("row_number", "integer", True, ""),
        ("raw_json", "text", True, ""), ("parsed_title", "text", False, ""),
        ("parsed_year", "integer", False, ""), ("parsed_watched_on", "date", False, ""),
        ("parsed_season_number", "integer", False, ""), ("parsed_episode_number", "integer", False, ""),
        ("parsed_imdb_id", "text", False, ""), ("parsed_tmdb_id", "bigint", False, ""),
        ("resolution_status", "text", True, ""), ("resolved_tconst", "text", False, ""),
        ("resolution_confidence", "double precision", False, ""), ("resolution_note", "text", False, ""),
    ),
    "local_seed_meta": (
        ("seed_name", "text", True, ""), ("seeded_at", "timestamp without time zone", True, ""),
        ("note", "text", False, ""),
    ),
    "user_lists": (
        ("id", "text", True, ""), ("slug", "text", True, ""),
        ("name", "text", True, ""), ("list_kind", "text", True, ""),
        ("source_origin", "text", True, ""), ("source_ref", "text", False, ""),
        ("created_at", "timestamp without time zone", True, ""),
        ("updated_at", "timestamp without time zone", True, ""),
        ("description", "text", False, ""),
        ("ai_input_role", "text", True, "'ignore'::text"),
    ),
    "user_list_items": (
        ("id", "text", True, ""), ("list_id", "text", True, ""),
        ("canonical_key", "text", True, ""), ("tconst", "text", False, ""),
        ("media_type", "text", True, ""), ("imdb_id", "text", False, ""),
        ("tmdb_id", "bigint", False, ""), ("trakt_id", "bigint", False, ""),
        ("parent_tconst", "text", False, ""), ("parent_title", "text", False, ""),
        ("title", "text", False, ""), ("season_number", "integer", False, ""),
        ("episode_number", "integer", False, ""), ("rank", "integer", False, ""),
        ("added_at", "timestamp without time zone", False, ""),
        ("notes", "text", False, ""), ("source_origin", "text", True, ""),
        ("source_ref", "text", False, ""), ("is_archived", "boolean", True, "false"),
        ("created_at", "timestamp without time zone", True, ""),
        ("updated_at", "timestamp without time zone", True, ""),
    ),
    "watch_events": (
        ("id", "text", True, ""), ("tconst", "text", True, ""),
        ("event_scope", "text", True, ""), ("watched_on", "date", True, ""),
        ("source", "text", True, ""), ("batch_id", "text", False, ""),
        ("import_row_id", "text", False, ""), ("rating", "smallint", False, ""),
        ("notes", "text", False, ""),
        ("created_at", "timestamp without time zone", True, ""),
    ),
    "user_ratings": (
        ("canonical_key", "text", True, ""), ("tconst", "text", False, ""),
        ("media_type", "text", True, ""), ("imdb_id", "text", False, ""),
        ("tmdb_id", "bigint", False, ""), ("trakt_id", "bigint", False, ""),
        ("parent_tconst", "text", False, ""), ("parent_title", "text", False, ""),
        ("title", "text", False, ""), ("season_number", "integer", False, ""),
        ("episode_number", "integer", False, ""), ("rating", "smallint", True, ""),
        ("rated_at", "timestamp without time zone", False, ""),
        ("source_origin", "text", True, ""), ("source_ref", "text", False, ""),
        ("created_at", "timestamp without time zone", True, ""),
        ("updated_at", "timestamp without time zone", True, ""),
        ("liked_notes", "text", False, ""), ("disliked_notes", "text", False, ""),
    ),
    "content_state": (
        ("tconst", "text", True, ""), ("interest_state", "text", True, ""),
        ("last_previewed_at", "timestamp without time zone", False, ""),
        ("last_watched_at", "timestamp without time zone", False, ""),
        ("updated_at", "timestamp without time zone", True, ""),
    ),
    "user_people": (
        ("person_key", "text", True, ""), ("nconst", "text", False, ""),
        ("name", "text", True, ""), ("known_for", "text", False, ""),
        ("birth_date", "text", False, ""), ("source_origin", "text", True, ""),
        ("source_ref", "text", False, ""), ("is_favorite", "boolean", True, "true"),
        ("created_at", "timestamp without time zone", True, ""),
        ("updated_at", "timestamp without time zone", True, ""),
        ("affinity_rating", "integer", False, ""),
    ),
    "user_title_role_signals": (
        ("signal_key", "text", True, ""), ("tconst", "text", True, ""),
        ("nconst", "text", False, ""), ("character_name", "text", False, ""),
        ("signal_type", "text", True, ""), ("polarity", "text", True, "'positive'::text"),
        ("strength", "integer", True, ""), ("notes", "text", False, ""),
        ("source_origin", "text", True, ""), ("source_ref", "text", False, ""),
        ("created_at", "timestamp without time zone", True, ""),
        ("updated_at", "timestamp without time zone", True, ""),
    ),
    "favorite_genres": (
        ("genre", "text", True, ""), ("weight", "double precision", True, "1.0"),
        ("preference_rank", "integer", False, ""), ("source_origin", "text", True, ""),
        ("source_ref", "text", False, ""), ("notes", "text", False, ""),
        ("is_active", "boolean", True, "true"),
        ("created_at", "timestamp without time zone", True, ""),
        ("updated_at", "timestamp without time zone", True, ""),
    ),
    "favorite_traits": (
        ("trait", "text", True, ""), ("weight", "double precision", True, "1.0"),
        ("preference_rank", "integer", False, ""), ("source_origin", "text", True, ""),
        ("source_ref", "text", False, ""), ("notes", "text", False, ""),
        ("is_active", "boolean", True, "true"),
        ("created_at", "timestamp without time zone", True, ""),
        ("updated_at", "timestamp without time zone", True, ""),
    ),
    "genre_scores": (
        ("id", "text", True, ""), ("genre", "text", True, ""),
        ("generated_at", "timestamp without time zone", True, ""),
        ("algorithm_version", "text", False, ""), ("score_scope", "text", False, ""),
        ("source_origin", "text", True, ""), ("source_ref", "text", False, ""),
        ("titles_considered", "integer", False, ""), ("watched_titles_considered", "integer", False, ""),
        ("rated_titles_considered", "integer", False, ""), ("contributing_titles_json", "text", False, ""),
        ("excluded_titles_json", "text", False, ""), ("favorite_genre_weight", "double precision", False, ""),
        ("preference_overlap_score", "double precision", False, ""),
        ("preference_alignment_score", "double precision", False, ""),
        ("affinity_score", "double precision", False, ""), ("rating_signal_score", "double precision", False, ""),
        ("watch_signal_score", "double precision", False, ""), ("recency_score", "double precision", False, ""),
        ("actor_affinity_score", "double precision", False, ""), ("frequency_score", "double precision", False, ""),
        ("consistency_score", "double precision", False, ""), ("novelty_score", "double precision", False, ""),
        ("confidence_score", "double precision", False, ""), ("manual_adjustment_score", "double precision", False, ""),
        ("final_score", "double precision", True, ""), ("normalized_score", "double precision", False, ""),
        ("rank_in_run", "integer", False, ""), ("metrics_json", "text", False, ""),
        ("explanation", "text", False, ""), ("created_at", "timestamp without time zone", True, ""),
    ),
    "search_recall": (
        ("id", "text", True, ""), ("entity_type", "text", True, ""),
        ("query_text", "text", True, ""), ("query_text_fold", "text", True, ""),
        ("query_key", "text", True, ""), ("target_id", "text", True, ""),
        ("target_label", "text", False, ""), ("target_title_type", "text", False, ""),
        ("matched_alias_title", "text", False, ""), ("fuzzy_score", "double precision", False, ""),
        ("first_searched_at", "timestamp without time zone", True, ""),
        ("last_searched_at", "timestamp without time zone", True, ""),
        ("hit_count", "integer", True, "1"),
    ),
}

EXPECTED_CONSTRAINTS = (
    ("imdb_file_manifest", "imdb_file_manifest_pkey", "p", "source_key", False, False, True),
    ("tmdb_title_map", "tmdb_title_map_pkey", "p", "tconst", False, False, True),
    ("tmdb_title_details", "tmdb_title_details_pkey", "p", "tconst,locale", False, False, True),
    ("tmdb_assets", "tmdb_assets_pkey", "p", "id", False, False, True),
    ("import_batches", "import_batches_pkey", "p", "id", False, False, True),
    ("import_rows", "import_rows_pkey", "p", "id", False, False, True),
    ("local_seed_meta", "local_seed_meta_pkey", "p", "seed_name", False, False, True),
    ("user_lists", "user_lists_pkey", "p", "id", False, False, True),
    ("user_lists", "user_lists_slug_key", "u", "slug", False, False, True),
    ("user_lists", "user_lists_ai_input_role_check", "c", "CHECK (ai_input_role = ANY (ARRAY['strong_positive'::text, 'interested_owned'::text, 'interested_planned'::text, 'in_progress'::text, 'negative'::text, 'external_suggestion'::text, 'ignore'::text]))", False, False, True),
    ("user_list_items", "user_list_items_pkey", "p", "id", False, False, True),
    ("user_list_items", "user_list_items_list_id_canonical_key_key", "u", "list_id,canonical_key", False, False, True),
    ("watch_events", "watch_events_pkey", "p", "id", False, False, True),
    ("watch_events", "watch_events_batch_import_row_key", "u", "batch_id,import_row_id", False, False, True),
    ("watch_events", "watch_events_event_scope_check", "c", "CHECK (event_scope = ANY (ARRAY['title'::text, 'episode'::text]))", False, False, True),
    ("watch_events", "watch_events_rating_check", "c", "CHECK (rating IS NULL OR rating >= 1 AND rating <= 10)", False, False, True),
    ("user_ratings", "user_ratings_pkey", "p", "canonical_key", False, False, True),
    ("user_ratings", "user_ratings_rating_check", "c", "CHECK (rating >= 1 AND rating <= 10)", False, False, True),
    ("content_state", "content_state_pkey", "p", "tconst", False, False, True),
    ("content_state", "content_state_interest_state_check", "c", "CHECK (interest_state = ANY (ARRAY['previewed'::text, 'in_progress'::text, 'watched'::text]))", False, False, True),
    ("user_people", "user_people_pkey", "p", "person_key", False, False, True),
    ("user_people", "user_people_affinity_rating_check", "c", "CHECK (affinity_rating IS NULL OR affinity_rating >= 0 AND affinity_rating <= 10)", False, False, True),
    ("user_title_role_signals", "user_title_role_signals_pkey", "p", "signal_key", False, False, True),
    ("user_title_role_signals", "user_title_role_signals_strength_check", "c", "CHECK (strength >= 0 AND strength <= 10)", False, False, True),
    ("user_title_role_signals", "user_title_role_signals_polarity_check", "c", "CHECK (polarity = ANY (ARRAY['positive'::text, 'negative'::text, 'mixed'::text]))", False, False, True),
    ("user_title_role_signals", "user_title_role_signals_signal_type_check", "c", "CHECK (signal_type = ANY (ARRAY['character'::text, 'dialogue'::text, 'behavior'::text, 'relationship_dynamic'::text, 'performance'::text, 'visual_appeal'::text, 'attraction'::text, 'other'::text]))", False, False, True),
    ("favorite_genres", "favorite_genres_pkey", "p", "genre", False, False, True),
    ("favorite_traits", "favorite_traits_pkey", "p", "trait", False, False, True),
    ("genre_scores", "genre_scores_pkey", "p", "id", False, False, True),
    ("search_recall", "search_recall_pkey", "p", "id", False, False, True),
)

EXPECTED_INDEXES = (
    # table, name, unique, primary, valid, ready, method, keys, opclasses,
    # collations (empty means non-collatable), per-key indoption bits.
    ("imdb_file_manifest", "imdb_file_manifest_pkey", True, True, True, True, "btree", "source_key", "text_ops", "default", "0"),
    ("tmdb_title_map", "tmdb_title_map_pkey", True, True, True, True, "btree", "tconst", "text_ops", "default", "0"),
    ("tmdb_title_details", "tmdb_title_details_pkey", True, True, True, True, "btree", "tconst,locale", "text_ops,text_ops", "default,default", "0,0"),
    ("tmdb_assets", "tmdb_assets_pkey", True, True, True, True, "btree", "id", "text_ops", "default", "0"),
    ("import_batches", "import_batches_pkey", True, True, True, True, "btree", "id", "text_ops", "default", "0"),
    ("import_rows", "import_rows_pkey", True, True, True, True, "btree", "id", "text_ops", "default", "0"),
    ("local_seed_meta", "local_seed_meta_pkey", True, True, True, True, "btree", "seed_name", "text_ops", "default", "0"),
    ("user_lists", "user_lists_pkey", True, True, True, True, "btree", "id", "text_ops", "default", "0"),
    ("user_lists", "user_lists_slug_key", True, False, True, True, "btree", "slug", "text_ops", "default", "0"),
    ("user_list_items", "user_list_items_pkey", True, True, True, True, "btree", "id", "text_ops", "default", "0"),
    ("user_list_items", "user_list_items_list_id_canonical_key_key", True, False, True, True, "btree", "list_id,canonical_key", "text_ops,text_ops", "default,default", "0,0"),
    ("user_list_items", "idx_user_list_items_list_active", False, False, True, True, "btree", "list_id,is_archived,rank", "text_ops,bool_ops,int4_ops", "default,,", "0,0,0"),
    ("user_list_items", "idx_user_list_items_tconst", False, False, True, True, "btree", "tconst", "text_ops", "default", "0"),
    ("watch_events", "watch_events_pkey", True, True, True, True, "btree", "id", "text_ops", "default", "0"),
    ("watch_events", "watch_events_batch_import_row_key", True, False, True, True, "btree", "batch_id,import_row_id", "text_ops,text_ops", "default,default", "0,0"),
    ("watch_events", "idx_watch_events_tconst_watched", False, False, True, True, "btree", "tconst,watched_on", "text_ops,date_ops", "default,", "0,3"),
    ("user_ratings", "user_ratings_pkey", True, True, True, True, "btree", "canonical_key", "text_ops", "default", "0"),
    ("user_ratings", "idx_user_ratings_tconst", False, False, True, True, "btree", "tconst", "text_ops", "default", "0"),
    ("content_state", "content_state_pkey", True, True, True, True, "btree", "tconst", "text_ops", "default", "0"),
    ("content_state", "idx_content_state_interest", False, False, True, True, "btree", "interest_state", "text_ops", "default", "0"),
    ("user_people", "user_people_pkey", True, True, True, True, "btree", "person_key", "text_ops", "default", "0"),
    ("user_people", "idx_user_people_nconst", False, False, True, True, "btree", "nconst", "text_ops", "default", "0"),
    ("user_people", "idx_user_people_favorite", False, False, True, True, "btree", "is_favorite", "bool_ops", "", "0"),
    ("user_title_role_signals", "user_title_role_signals_pkey", True, True, True, True, "btree", "signal_key", "text_ops", "default", "0"),
    ("user_title_role_signals", "idx_user_title_role_signals_tconst", False, False, True, True, "btree", "tconst", "text_ops", "default", "0"),
    ("user_title_role_signals", "idx_user_title_role_signals_nconst", False, False, True, True, "btree", "nconst", "text_ops", "default", "0"),
    ("user_title_role_signals", "idx_user_title_role_signals_polarity_strength", False, False, True, True, "btree", "polarity,strength", "text_ops,int4_ops", "default,", "0,3"),
    ("favorite_genres", "favorite_genres_pkey", True, True, True, True, "btree", "genre", "text_ops", "default", "0"),
    ("favorite_genres", "idx_favorite_genres_active_rank", False, False, True, True, "btree", "is_active,preference_rank", "bool_ops,int4_ops", ",", "0,0"),
    ("favorite_traits", "favorite_traits_pkey", True, True, True, True, "btree", "trait", "text_ops", "default", "0"),
    ("favorite_traits", "idx_favorite_traits_active_rank", False, False, True, True, "btree", "is_active,preference_rank", "bool_ops,int4_ops", ",", "0,0"),
    ("genre_scores", "genre_scores_pkey", True, True, True, True, "btree", "id", "text_ops", "default", "0"),
    ("genre_scores", "idx_genre_scores_genre_generated_at", False, False, True, True, "btree", "genre,generated_at", "text_ops,timestamp_ops", "default,", "0,0"),
    ("genre_scores", "idx_genre_scores_scope_generated_at", False, False, True, True, "btree", "score_scope,generated_at", "text_ops,timestamp_ops", "default,", "0,0"),
    ("search_recall", "search_recall_pkey", True, True, True, True, "btree", "id", "text_ops", "default", "0"),
    ("search_recall", "idx_search_recall_entity_query_key", False, False, True, True, "btree", "entity_type,query_key", "text_ops,text_ops", "default,default", "0,0"),
    ("search_recall", "idx_search_recall_last_searched_at", False, False, True, True, "btree", "last_searched_at", "timestamp_ops", "", "0"),
)


@dataclass(frozen=True)
class ConnectionConfig:
    """Jedno psql pripojeni bez vypisovani hesla do argumentu procesu."""

    psql: Path
    host: str
    port: str
    database: str
    user: str
    password: str

    def environment(self) -> dict[str, str]:
        """Vytvori minimalni prostredi bez zdedenych PG* promennych."""

        return {
            "PGPASSWORD": self.password,
            "PGCONNECT_TIMEOUT": "10",
            "LC_ALL": "C",
            "LANG": "C",
        }


@dataclass(frozen=True)
class SourceSnapshot:
    """Počty a konkrétní řádky zachycené ve stejné transakci jako CSV."""

    counts: dict[str, int]
    samples: dict[str, tuple[object, ...]]


def _load_values() -> dict[str, str | None]:
    _assert_secure_env_file(ENV_FILE)
    return dict(dotenv_values(ENV_FILE, interpolate=False))


def _config(prefix: str, values: dict[str, str | None]) -> ConnectionConfig:
    password = values.get(f"POSTGRES_{prefix}_PASSWORD") or ""
    if not password:
        raise RuntimeError(f"POSTGRES_{prefix}_PASSWORD v .env chybi nebo je prazdne")
    default_user = "postgres" if prefix == "ADMIN" else APP_ROLE
    default_database = "postgres" if prefix == "ADMIN" else TARGET_DATABASE
    return ConnectionConfig(
        psql=_resolve_psql(values.get("POSTGRES_PSQL_PATH")),
        host=values.get(f"POSTGRES_{prefix}_HOST") or "/private/tmp",
        port=values.get(f"POSTGRES_{prefix}_PORT") or "5432",
        database=values.get(f"POSTGRES_{prefix}_DATABASE") or default_database,
        user=values.get(f"POSTGRES_{prefix}_USER") or default_user,
        password=password,
    )


def _psql(
    config: ConnectionConfig,
    *,
    sql: str | None = None,
    file: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Spusti psql; SQL i hesla zustavaji mimo argv a vystup se zachyti."""

    command = [
        str(config.psql), "-X", "-v", "ON_ERROR_STOP=1", "-P", "pager=off",
        "-h", config.host, "-p", config.port, "-U", config.user,
        "-d", config.database,
    ]
    if file is not None:
        command.extend(("-f", str(file)))
    return subprocess.run(
        command,
        input=sql,
        env=config.environment(),
        check=check,
        capture_output=True,
        text=True,
    )


def _sql_literal(value: str) -> str:
    """Bezpecne uzavre interní hodnotu do PostgreSQL textoveho literalu."""

    return "'" + value.replace("'", "''") + "'"


def _write_app_config(password: str, admin: ConnectionConfig) -> None:
    """Doplni app pripojeni, zachova ostatni secrets a vynuti chmod 600."""

    desired = {
        "POSTGRES_APP_HOST": admin.host,
        "POSTGRES_APP_PORT": admin.port,
        "POSTGRES_APP_DATABASE": TARGET_DATABASE,
        "POSTGRES_APP_USER": APP_ROLE,
        "POSTGRES_APP_PASSWORD": password,
    }
    _assert_secure_env_file(ENV_FILE)
    original_lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    seen: set[str] = set()

    def env_line(key: str, value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")
        return f'{key}="{escaped}"'

    for line in original_lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0] if "=" in stripped and not stripped.startswith("#") else None
        if key in desired:
            output.append(env_line(key, desired[key]))
            seen.add(key)
        else:
            output.append(line)
    if output and output[-1] != "":
        output.append("")
    for key, value in desired.items():
        if key not in seen:
            output.append(env_line(key, value))

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=ENV_FILE.parent,
            prefix=".env.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write("\n".join(output) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary_path, ENV_FILE)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _validated_app_secret(admin: ConnectionConfig, values: dict[str, str | None]) -> str:
    """Zachová platný app secret; odmítne konfiguraci mířící jinam."""

    expected = {
        "POSTGRES_APP_HOST": admin.host,
        "POSTGRES_APP_PORT": admin.port,
        "POSTGRES_APP_DATABASE": TARGET_DATABASE,
        "POSTGRES_APP_USER": APP_ROLE,
    }
    for key, wanted in expected.items():
        configured = values.get(key)
        if configured is not None and configured != "" and configured != wanted:
            raise RuntimeError(f"{key} míří na {configured!r}, očekáváno {wanted!r}")
    return values.get("POSTGRES_APP_PASSWORD") or secrets.token_urlsafe(36)


def provision_role(
    admin: ConnectionConfig, values: dict[str, str | None], password: str
) -> ConnectionConfig:
    """Vytvori nebo zpresni omezenou roli a ulozi jeji nahodne heslo."""

    role_sql = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        CREATE ROLE {APP_ROLE} LOGIN;
    END IF;
END
$$;
ALTER ROLE {APP_ROLE} WITH LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS PASSWORD {_sql_literal(password)};
ALTER ROLE {APP_ROLE} IN DATABASE {TARGET_DATABASE} SET search_path = app, public;
    """
    _psql(admin, sql=role_sql)
    _write_app_config(password, admin)
    return _config("APP", _load_values())


def inspect_target_schema_before_apply(config: ConnectionConfig) -> str:
    """Povolí jen zcela prázdné ``app`` nebo už přesně ověřené runtime schéma."""

    result = _psql(
        config,
        sql="""
COPY (
    SELECT c.relname, c.relkind
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='app'
    ORDER BY c.relname, c.relkind
) TO STDOUT WITH (FORMAT csv);
""",
    )
    relations = [line for line in result.stdout.splitlines() if line.strip()]
    tables = {
        line.split(",", 1)[0].strip('"')
        for line in relations
        if line.rsplit(",", 1)[-1].strip('"') == "r"
    }
    expected = set(TABLE_COLUMNS)
    legacy_expected = set(LEGACY_RUNTIME_TABLES)
    allowed_catalog_tables = set(CATALOG_APP_TABLES)
    if not relations:
        return "fresh"
    views = {
        line.split(",", 1)[0].strip('"')
        for line in relations
        if line.rsplit(",", 1)[-1].strip('"') == "v"
    }
    if not views.issubset(set(OPTIONAL_RUNTIME_VIEWS)):
        extras = sorted(views - set(OPTIONAL_RUNTIME_VIEWS))
        raise RuntimeError(
            "Cílové app schéma je před 002 částečné nebo obsahuje cizí relace; "
            f"neočekávané views={extras}, relace={relations[:20]}"
        )
    if not (expected - tables) and not (tables - expected - allowed_catalog_tables):
        verify_schema_fingerprint(config)
        return "existing"
    if not (legacy_expected - tables) and not (tables - legacy_expected - allowed_catalog_tables):
        verify_schema_fingerprint(config, tables_override=LEGACY_RUNTIME_TABLES)
        return "legacy-runtime"
    missing = sorted(expected - tables)
    extra = sorted(tables - expected)
    raise RuntimeError(
        "Cílové app schéma je před 002 částečné nebo obsahuje cizí relace; "
        f"chybí tabulky={missing}, navíc tabulky={extra}, relace={relations[:20]}"
    )


def apply_schema(admin: ConnectionConfig) -> None:
    """Ověří vstup, vytvoří schema objekty, ověří je a až poté udělí práva."""

    target_admin = ConnectionConfig(
        psql=admin.psql,
        host=admin.host,
        port=admin.port,
        database=TARGET_DATABASE,
        user=admin.user,
        password=admin.password,
    )
    inspect_target_schema_before_apply(target_admin)
    _psql(target_admin, file=SCHEMA_MIGRATION)
    verify_schema_fingerprint(target_admin)
    _psql(target_admin, file=GRANTS_MIGRATION)


def verify_bootstrap_baseline(admin: ConnectionConfig) -> None:
    """Read-only ověří, že 000/001 už byly vědomě provedeny."""

    check_bootstrap(
        PostgreSQLConfig(
            psql=admin.psql,
            host=admin.host,
            port=admin.port,
            user=admin.user,
            admin_database=admin.database,
            password=admin.password,
        )
    )


def _values_sql(rows: list[tuple[object, ...]] | tuple[tuple[object, ...], ...]) -> str:
    """Sestaví VALUES pouze z interních konstant schématu."""

    return ",\n".join(
        "(" + ",".join(_postgres_literal(value) for value in row) + ")"
        for row in rows
    )


def _schema_verification_sql(tables_override: tuple[str, ...] | None = None) -> str:
    """Vrátí fail-closed dotaz na přesný PostgreSQL app-state fingerprint."""

    table_names = tables_override or tuple(TABLE_COLUMNS)
    columns = [
        (table, position, name, data_type, not_null, default)
        for table, definitions in EXPECTED_COLUMNS.items()
        if table in table_names
        for position, (name, data_type, not_null, default) in enumerate(definitions, 1)
    ]
    constraints = tuple(row for row in EXPECTED_CONSTRAINTS if row[0] in table_names)
    indexes = tuple(row for row in EXPECTED_INDEXES if row[0] in table_names)
    tables = [(table,) for table in table_names]
    return f"""
COPY (
WITH expected_tables(table_name) AS (VALUES {_values_sql(tables)}),
expected_columns(table_name,position,column_name,data_type,not_null,default_expr) AS (
    VALUES {_values_sql(columns)}
), expected_constraints(table_name,constraint_name,constraint_type,columns,
                         is_deferrable,is_deferred,is_validated) AS (
    VALUES {_values_sql(constraints)}
), expected_indexes(table_name,index_name,is_unique,is_primary,is_valid,is_ready,
                     access_method,key_columns,opclasses,collations,options) AS (
    VALUES {_values_sql(indexes)}
), actual_tables AS (
    SELECT c.relname::text AS table_name
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='app' AND c.relkind='r' AND c.relpersistence='p'
      AND c.relname IN ({', '.join(_sql_literal(table) for table in table_names)})
), expected_optional_views(view_name) AS (
    VALUES {_values_sql([(name,) for name in OPTIONAL_RUNTIME_VIEWS])}
), actual_views AS (
    SELECT c.relname::text AS view_name
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='app' AND c.relkind='v'
), actual_columns AS (
    SELECT c.relname::text, a.attnum::integer, a.attname::text,
           format_type(a.atttypid,a.atttypmod), a.attnotnull,
           COALESCE(pg_get_expr(d.adbin,d.adrelid),'')
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
    LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
    WHERE n.nspname='app' AND c.relkind='r'
      AND c.relname IN ({', '.join(_sql_literal(table) for table in table_names)})
), actual_constraints AS (
    SELECT c.relname::text, con.conname::text, con.contype::text,
           CASE
               WHEN con.contype = 'c' THEN pg_get_constraintdef(con.oid, true)
               ELSE string_agg(a.attname::text, ',' ORDER BY keys.ordinality)
           END,
           con.condeferrable, con.condeferred, con.convalidated
    FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN LATERAL unnest(con.conkey) WITH ORDINALITY keys(attnum,ordinality)
        ON con.contype <> 'c'
    LEFT JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=keys.attnum
    WHERE n.nspname='app' AND c.relkind='r'
      AND c.relname IN ({', '.join(_sql_literal(table) for table in table_names)})
    GROUP BY c.relname,con.conname,con.contype,con.condeferrable,
             con.condeferred,con.convalidated,con.oid
), actual_indexes AS (
    SELECT c.relname::text, ci.relname::text, i.indisunique, i.indisprimary,
           i.indisvalid, i.indisready, am.amname::text,
           string_agg(a.attname::text, ',' ORDER BY keys.ordinality),
           string_agg(opc.opcname::text, ',' ORDER BY keys.ordinality),
           string_agg(CASE WHEN coll.oid IS NULL THEN '' ELSE coll.collname::text END,
                      ',' ORDER BY keys.ordinality),
           string_agg(keys.option::text, ',' ORDER BY keys.ordinality)
    FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_class ci ON ci.oid=i.indexrelid
    JOIN pg_am am ON am.oid=ci.relam
    CROSS JOIN LATERAL unnest(i.indkey::smallint[], i.indclass::oid[],
                              i.indcollation::oid[], i.indoption::smallint[])
        WITH ORDINALITY keys(attnum,opclass,collation_oid,option,ordinality)
    JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=keys.attnum
    JOIN pg_opclass opc ON opc.oid=keys.opclass
    LEFT JOIN pg_collation coll ON coll.oid=keys.collation_oid
    WHERE n.nspname='app' AND c.relkind='r' AND i.indexprs IS NULL
      AND c.relname IN ({', '.join(_sql_literal(table) for table in table_names)})
      AND i.indpred IS NULL AND i.indnkeyatts=i.indnatts
      AND keys.ordinality<=i.indnkeyatts
    GROUP BY c.relname,ci.relname,i.indisunique,i.indisprimary,i.indisvalid,
             i.indisready,am.amname
), violations(detail) AS (
    SELECT 'table missing/extra: ' || table_name FROM (
        (SELECT * FROM expected_tables EXCEPT SELECT * FROM actual_tables)
        UNION ALL (SELECT * FROM actual_tables EXCEPT SELECT * FROM expected_tables)
    ) q
    UNION ALL
    SELECT 'unexpected view object: ' || view_name FROM (
        SELECT * FROM actual_views
        EXCEPT
        SELECT * FROM expected_optional_views
    ) q
    UNION ALL SELECT 'column missing/extra/drift: ' || row_to_json(q)::text FROM (
        (SELECT * FROM expected_columns EXCEPT SELECT * FROM actual_columns)
        UNION ALL (SELECT * FROM actual_columns EXCEPT SELECT * FROM expected_columns)
    ) q
    UNION ALL SELECT 'constraint missing/extra/drift: ' || row_to_json(q)::text FROM (
        (SELECT * FROM expected_constraints EXCEPT SELECT * FROM actual_constraints)
        UNION ALL (SELECT * FROM actual_constraints EXCEPT SELECT * FROM expected_constraints)
    ) q
    UNION ALL SELECT 'index missing/extra/drift: ' || row_to_json(q)::text FROM (
        (SELECT * FROM expected_indexes EXCEPT SELECT * FROM actual_indexes)
        UNION ALL (SELECT * FROM actual_indexes EXCEPT SELECT * FROM expected_indexes)
    ) q
    UNION ALL
    SELECT 'unexpected table owner: ' || c.relname || '=' || pg_get_userbyid(c.relowner)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='app' AND c.relkind='r' AND c.relowner<>current_user::regrole
    UNION ALL
    SELECT 'unexpected view owner: ' || c.relname || '=' || pg_get_userbyid(c.relowner)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='app' AND c.relkind='v'
      AND c.relname IN ({', '.join(_sql_literal(name) for name in OPTIONAL_RUNTIME_VIEWS)})
      AND c.relowner<>current_user::regrole
    UNION ALL
    SELECT 'table security/persistence drift: ' || c.relname
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='app' AND c.relkind='r'
      AND c.relname IN ({', '.join(_sql_literal(table) for table in table_names)})
      AND (c.relpersistence<>'p' OR c.relrowsecurity OR c.relforcerowsecurity)
    UNION ALL
    SELECT 'unexpected row security policy: ' || c.relname || '.' || p.polname
    FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='app'
    UNION ALL
    SELECT 'unexpected relation object: ' || c.relname || ' (' || c.relkind || ')'
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='app' AND (
        c.relkind NOT IN ('r','i','v')
        OR (c.relkind='v' AND c.relname NOT IN ({', '.join(_sql_literal(name) for name in OPTIONAL_RUNTIME_VIEWS)}))
    )
    UNION ALL
    SELECT 'unexpected trigger: ' || c.relname || '.' || t.tgname
    FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='app' AND NOT t.tgisinternal
      AND (c.relname || '.' || t.tgname) NOT IN ({', '.join(_sql_literal(name) for name in OPTIONAL_RUNTIME_TRIGGERS)})
    UNION ALL
    SELECT 'unsupported/extra constraint: ' || c.relname || '.' || con.conname
    FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='app' AND c.relname IN ({', '.join(_sql_literal(table) for table in table_names)})
      AND con.contype NOT IN ('p','u','c')
    UNION ALL
    SELECT 'expression/partial/include/non-permanent index is forbidden: ' || c.relname || '.' || ci.relname
    FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_class ci ON ci.oid=i.indexrelid
    WHERE n.nspname='app' AND c.relname IN ({', '.join(_sql_literal(table) for table in table_names)})
      AND (i.indexprs IS NOT NULL OR i.indpred IS NOT NULL
          OR i.indnkeyatts<>i.indnatts OR ci.relpersistence<>'p')
)
SELECT detail FROM violations ORDER BY detail
) TO STDOUT WITH (FORMAT csv);
"""


def verify_schema_fingerprint(
    config: ConnectionConfig,
    tables_override: tuple[str, ...] | None = None,
) -> None:
    """Zastaví migraci při jakékoli odchylce cílových app-state tabulek."""

    result = _psql(config, sql=_schema_verification_sql(tables_override))
    violations = [line for line in result.stdout.splitlines() if line.strip()]
    if violations:
        raise RuntimeError("Cílové runtime schéma nesedí: " + "; ".join(violations[:20]))


def _duckdb_csv_literal(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


DUCKDB_OPTIONAL_SOURCE_COLUMNS: dict[str, dict[str, str]] = {
    "user_lists": {
        "ai_input_role": (
            "CASE "
            "WHEN slug = 'kouknout-znou' THEN 'strong_positive' "
            "WHEN slug = 'mam' THEN 'interested_owned' "
            "WHEN slug IN ('watchlist', 'koukni-rychle', 'stahnout') THEN 'interested_planned' "
            "WHEN slug = 'rozkoukano' THEN 'in_progress' "
            "WHEN slug = 'nedokoukano' THEN 'negative' "
            "WHEN slug = 'ai-navrhy' THEN 'external_suggestion' "
            "ELSE 'ignore' END"
        ),
    },
    "user_ratings": {
        "liked_notes": "CAST(NULL AS VARCHAR)",
        "disliked_notes": "CAST(NULL AS VARCHAR)",
    }
}
DUCKDB_OPTIONAL_EMPTY_SOURCE_TABLES = frozenset({"user_title_role_signals"})


def _duckdb_table_exists(connection: duckdb.DuckDBPyConnection, table: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'app'
          AND table_name = ?
        LIMIT 1
        """,
        [table],
    ).fetchone()
    return row is not None


def _write_empty_csv(path: Path, columns: tuple[str, ...]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)


def _duckdb_table_columns(connection: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info('app.{table}')").fetchall()
    return {str(row[1]) for row in rows}


def _duckdb_select_list(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    columns: tuple[str, ...],
) -> str:
    available_columns = _duckdb_table_columns(connection, table)
    optional_columns = DUCKDB_OPTIONAL_SOURCE_COLUMNS.get(table, {})
    expressions: list[str] = []
    for column in columns:
        if column in available_columns:
            expressions.append(column)
        elif column in optional_columns:
            expressions.append(f"{optional_columns[column]} AS {column}")
        else:
            raise RuntimeError(f"DuckDB zdrojova tabulka app.{table} nema ocekavany sloupec {column}.")
    return ", ".join(expressions)


def export_source(directory: Path) -> SourceSnapshot:
    """Exportuje konzistentni read-only snapshot app-state tabulek do CSV."""

    counts: dict[str, int] = {}
    samples: dict[str, tuple[object, ...]] = {}
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as connection:
        connection.execute("BEGIN TRANSACTION")
        try:
            for table, columns in TABLE_COLUMNS.items():
                if not _duckdb_table_exists(connection, table):
                    if table not in DUCKDB_OPTIONAL_EMPTY_SOURCE_TABLES:
                        raise RuntimeError(f"DuckDB zdrojova tabulka app.{table} neexistuje.")
                    counts[table] = 0
                    _write_empty_csv(directory / f"{table}.csv", columns)
                    continue
                counts[table] = connection.execute(
                    f"SELECT count(*) FROM app.{table}"
                ).fetchone()[0]
                column_sql = _duckdb_select_list(connection, table, columns)
                path = directory / f"{table}.csv"
                connection.execute(
                    f"COPY (SELECT {column_sql} FROM app.{table}) "
                    f"TO {_duckdb_csv_literal(path)} "
                    "(FORMAT CSV, HEADER TRUE, NULL '\\N')"
                )
                sample_columns = SAMPLE_COLUMNS[table]
                sample_sql = _duckdb_select_list(connection, table, sample_columns)
                sample = connection.execute(
                    f"SELECT {sample_sql} FROM app.{table} "
                    f"ORDER BY {sample_columns[0]} LIMIT 1"
                ).fetchone()
                if sample is not None:
                    samples[table] = tuple(sample)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return SourceSnapshot(counts=counts, samples=samples)


def _psql_path_literal(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def import_snapshot(admin: ConnectionConfig, directory: Path) -> None:
    """Nacte staging CSV a atomicky nahradi cele app-state tabulky."""

    target_admin = ConnectionConfig(
        admin.psql, admin.host, admin.port, TARGET_DATABASE, admin.user, admin.password
    )
    # Druhá kontrola je záměrně těsně před sestavením destruktivní transakce.
    verify_schema_fingerprint(target_admin)
    pieces = ["BEGIN;", "SET LOCAL lock_timeout = '10s';"]
    for table, columns in TABLE_COLUMNS.items():
        column_sql = ", ".join(columns)
        pieces.extend(
            (
                f"CREATE TEMP TABLE stage_{table} "
                f"(LIKE app.{table} INCLUDING DEFAULTS) ON COMMIT DROP;",
                f"\\copy stage_{table} ({column_sql}) FROM "
                f"{_psql_path_literal(directory / f'{table}.csv')} "
                "WITH (FORMAT csv, HEADER true, NULL '\\N')",
            )
        )
    for table in reversed(tuple(TABLE_COLUMNS)):
        pieces.append(f"DELETE FROM app.{table};")
    for table, columns in TABLE_COLUMNS.items():
        column_sql = ", ".join(columns)
        pieces.append(
            f"INSERT INTO app.{table} ({column_sql}) SELECT {column_sql} FROM stage_{table};"
        )
    pieces.extend(
        (
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM app.user_list_items i "
            "LEFT JOIN app.user_lists l ON l.id=i.list_id WHERE l.id IS NULL) "
            "THEN RAISE EXCEPTION 'Orphan list_id after runtime import'; END IF; END $$;",
            "COMMIT;",
        )
    )
    _psql(target_admin, sql="\n".join(pieces) + "\n")


def _target_counts(config: ConnectionConfig) -> dict[str, int]:
    unions = " UNION ALL ".join(
        f"SELECT '{table}', count(*) FROM app.{table}" for table in TABLE_COLUMNS
    )
    result = _psql(config, sql=f"COPY ({unions}) TO STDOUT WITH (FORMAT csv);\n")
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if "," in line:
            table, count = line.split(",", 1)
            counts[table] = int(count)
    return counts


# Vzorek je celý řádek. První sloupec každé tabulky je její stabilní primární klíč.
SAMPLE_COLUMNS: dict[str, tuple[str, ...]] = dict(TABLE_COLUMNS)


def _postgres_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return _sql_literal(value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat())
    return _sql_literal(str(value))


def verify_exact_samples(
    config: ConnectionConfig, samples: dict[str, tuple[object, ...]]
) -> None:
    """Porovna jeden konkretni radek vcetne NULL a casu z kazde tabulky."""

    checks: list[str] = []
    labels: list[str] = []
    for table, row in samples.items():
        columns = SAMPLE_COLUMNS[table]
        predicates = " AND ".join(
            f"{column} IS NOT DISTINCT FROM {_postgres_literal(value)}"
            for column, value in zip(columns, row, strict=True)
        )
        checks.append(
            f"SELECT '{table}', EXISTS (SELECT 1 FROM app.{table} WHERE {predicates})"
        )
        labels.append(f"{table}:{row[0]}")
    if not checks:
        return
    result = _psql(
        config,
        sql="COPY (" + " UNION ALL ".join(checks) + ") TO STDOUT WITH (FORMAT csv);\n",
    )
    states = dict(line.split(",", 1) for line in result.stdout.splitlines() if "," in line)
    failed = [table for table in SAMPLE_COLUMNS if table in states and states[table] != "t"]
    if failed or len(states) != len(checks):
        raise RuntimeError(f"Nesedi konkretni vzorky tabulek: {failed or 'neuplny vysledek'}")
    print("Vzorky OK: " + ", ".join(labels))


def verify_data(admin: ConnectionConfig, source: SourceSnapshot) -> None:
    """Porovna pocty a overi list_id vazby bez katalogoveho FK."""

    target_admin = ConnectionConfig(
        admin.psql, admin.host, admin.port, TARGET_DATABASE, admin.user, admin.password
    )
    target_counts = _target_counts(target_admin)
    if target_counts != source.counts:
        raise RuntimeError(f"Nesedi pocty radku: DuckDB={source.counts}, PostgreSQL={target_counts}")
    orphan_check = _psql(
        target_admin,
        sql=(
            "COPY (SELECT count(*) FROM app.user_list_items i LEFT JOIN app.user_lists l "
            "ON l.id=i.list_id WHERE l.id IS NULL) TO STDOUT;\n"
        ),
    ).stdout.strip()
    if orphan_check != "0":
        raise RuntimeError(f"PostgreSQL obsahuje {orphan_check} osiřelých list_id")
    summary = ", ".join(f"{table}={count}" for table, count in source.counts.items())
    print(f"Data OK: {summary}; orphan list_id=0")
    verify_exact_samples(target_admin, source.samples)


def verify_role(admin: ConnectionConfig, app: ConnectionConfig) -> None:
    """Overi atributy, DML s rollbackem a zakaz CREATE i admin katalogu."""

    target_admin = ConnectionConfig(
        admin.psql, admin.host, admin.port, TARGET_DATABASE, admin.user, admin.password
    )
    relation_privileges = [
        ("app", table, privilege)
        for table in TABLE_COLUMNS
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
    ]
    relation_privileges.extend(
        ("app", relation, "SELECT")
        for relation in APP_READ_ONLY_RELATIONS
    )
    relation_privileges.extend(
        ("raw", relation, "SELECT")
        for relation in RAW_READ_ONLY_RELATIONS
    )
    relation_privileges.extend(
        ("old", table, privilege)
        for table in OLD_RUNTIME_TABLES
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
    )
    owner_privileges = [
        ("app", table, admin.user, privilege)
        for table in TABLE_COLUMNS
        for privilege in (
            "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"
        )
    ]
    owner_privileges.extend(
        ("app", relation, admin.user, privilege)
        for relation in APP_READ_ONLY_RELATIONS
        for privilege in (
            "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"
        )
    )
    owner_privileges.extend(
        ("raw", relation, admin.user, privilege)
        for relation in RAW_READ_ONLY_RELATIONS
        for privilege in (
            "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"
        )
    )
    owner_privileges.extend(
        ("old", table, admin.user, privilege)
        for table in OLD_RUNTIME_TABLES
        for privilege in (
            "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"
        )
    )
    exact_relation_acl = owner_privileges + [
        (schema_name, relation_name, APP_ROLE, privilege)
        for schema_name, relation_name, privilege in relation_privileges
    ]
    role_state = _psql(
        target_admin,
        sql=f"""
COPY (
WITH expected_relation_privileges(schema_name,relation_name,privilege_type) AS (
    VALUES {_values_sql(relation_privileges)}
), expected_relation_acl(schema_name,relation_name,grantee,privilege_type) AS (
    VALUES {_values_sql(exact_relation_acl)}
), actual_relation_acl AS (
    SELECT n.nspname::text,
           c.relname::text,
           CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee)::text END,
           acl.privilege_type::text
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace,
         LATERAL aclexplode(c.relacl) acl
    WHERE n.nspname IN ('app', 'raw', 'old') AND c.relkind IN ('r', 'v')
), violations(detail) AS (
    SELECT 'role missing or cluster flags/INHERIT not locked down'
    WHERE NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname='filmy_app' AND rolcanlogin
          AND NOT rolinherit AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
          AND NOT rolreplication AND NOT rolbypassrls AND rolconnlimit=-1
          AND rolvaliduntil IS NULL AND rolconfig IS NULL
    )
    UNION ALL
    SELECT 'role membership: ' || pg_get_userbyid(m.roleid) || ' -> ' || pg_get_userbyid(m.member)
    FROM pg_auth_members m
    WHERE m.roleid='filmy_app'::regrole OR m.member='filmy_app'::regrole
    UNION ALL
    SELECT 'database direct privilege drift: ' || d.datname || ':' || acl.privilege_type
    FROM pg_database d, LATERAL aclexplode(d.datacl) acl
    WHERE acl.grantee='filmy_app'::regrole
      AND NOT (d.datname='filmy' AND acl.privilege_type='CONNECT')
    UNION ALL
    SELECT 'database CONNECT missing' WHERE NOT has_database_privilege('filmy_app','filmy','CONNECT')
    UNION ALL
    SELECT 'database CREATE unexpectedly allowed' WHERE has_database_privilege('filmy_app','filmy','CREATE')
    UNION ALL
    SELECT 'schema privilege drift: ' || n.nspname || ':' || acl.privilege_type
    FROM pg_namespace n, LATERAL aclexplode(n.nspacl) acl
    WHERE acl.grantee='filmy_app'::regrole
      AND NOT (n.nspname IN ('app', 'raw', 'old') AND acl.privilege_type='USAGE')
    UNION ALL
    SELECT 'app USAGE missing' WHERE NOT has_schema_privilege('filmy_app','app','USAGE')
    UNION ALL
    SELECT 'raw USAGE missing' WHERE NOT has_schema_privilege('filmy_app','raw','USAGE')
    UNION ALL
    SELECT 'old USAGE missing' WHERE NOT has_schema_privilege('filmy_app','old','USAGE')
    UNION ALL
    SELECT 'schema CREATE unexpectedly allowed: ' || name
    FROM (VALUES ('app'),('raw'),('old'),('public')) schemas(name)
    WHERE has_schema_privilege('filmy_app',name,'CREATE')
    UNION ALL
    SELECT 'table ACL missing/extra: ' || schema_name || '.' || relation_name || ':' || grantee || ':' || privilege_type FROM (
        (SELECT * FROM expected_relation_acl EXCEPT SELECT * FROM actual_relation_acl)
        UNION ALL
        (SELECT * FROM actual_relation_acl EXCEPT SELECT * FROM expected_relation_acl)
    ) q
    UNION ALL
    SELECT 'required table privilege ineffective: ' || schema_name || '.' || relation_name || ':' || privilege_type
    FROM expected_relation_privileges
    WHERE NOT has_table_privilege('filmy_app', schema_name || '.' || relation_name, privilege_type)
    UNION ALL
    SELECT 'forbidden table privilege effective: ' || schema_name || '.' || relation_name || ':' || privilege_type
    FROM (VALUES ('TRUNCATE'),('REFERENCES'),('TRIGGER')) forbidden(privilege_type)
    CROSS JOIN (SELECT schema_name, relation_name FROM expected_relation_privileges GROUP BY schema_name, relation_name) relations
    WHERE has_table_privilege('filmy_app', schema_name || '.' || relation_name, privilege_type)
    UNION ALL
    SELECT 'default privilege for PUBLIC/filmy_app is forbidden: ' ||
           CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee)::text END
           || ':' || acl.privilege_type
    FROM pg_default_acl d, LATERAL aclexplode(d.defaclacl) acl
    WHERE acl.grantee IN (0, 'filmy_app'::regrole)
    UNION ALL
    SELECT 'unexpected role/database setting: ' || config
    FROM pg_db_role_setting s
    CROSS JOIN LATERAL unnest(s.setconfig) config
    WHERE s.setrole='filmy_app'::regrole
      AND NOT (s.setdatabase=(SELECT oid FROM pg_database WHERE datname='filmy')
               AND config='search_path=app, public')
    UNION ALL
    SELECT 'required search_path setting missing'
    WHERE NOT EXISTS (
        SELECT 1 FROM pg_db_role_setting s
        CROSS JOIN LATERAL unnest(s.setconfig) config
        WHERE s.setrole='filmy_app'::regrole
          AND s.setdatabase=(SELECT oid FROM pg_database WHERE datname='filmy')
          AND config='search_path=app, public'
    )
    UNION ALL
    SELECT 'unexpected sequence privilege: ' || n.nspname || '.' || c.relname || ':' || acl.privilege_type
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace,
         LATERAL aclexplode(c.relacl) acl
    WHERE c.relkind='S' AND acl.grantee='filmy_app'::regrole
    UNION ALL
    SELECT 'unexpected relation privilege: ' || n.nspname || '.' || c.relname || ':' || acl.privilege_type
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace,
         LATERAL aclexplode(c.relacl) acl
    WHERE c.relkind IN ('r','p','v','m','f') AND acl.grantee='filmy_app'::regrole
      AND NOT (
          n.nspname='app' AND c.relname IN ({', '.join(_sql_literal(table) for table in TABLE_COLUMNS)})
          AND acl.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')
      )
      AND NOT (
          n.nspname='app' AND c.relname IN ({', '.join(_sql_literal(name) for name in APP_READ_ONLY_RELATIONS)})
          AND acl.privilege_type = 'SELECT'
      )
      AND NOT (
          n.nspname='raw' AND c.relname IN ({', '.join(_sql_literal(name) for name in RAW_READ_ONLY_RELATIONS)})
          AND acl.privilege_type = 'SELECT'
      )
      AND NOT (
          n.nspname='old' AND c.relname IN ({', '.join(_sql_literal(name) for name in OLD_RUNTIME_TABLES)})
          AND acl.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')
      )
    UNION ALL
    SELECT 'unexpected function privilege: ' || n.nspname || '.' || p.proname || ':' || acl.privilege_type
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace,
         LATERAL aclexplode(p.proacl) acl
    WHERE acl.grantee='filmy_app'::regrole
      AND NOT (
          (n.nspname || '.' || p.proname) IN ({', '.join(_sql_literal(name) for name in ALLOWED_FUNCTION_EXECUTE_PRIVILEGES)})
          AND acl.privilege_type = 'EXECUTE'
      )
    UNION ALL
    SELECT 'unexpected type privilege: ' || n.nspname || '.' || t.typname || ':' || acl.privilege_type
    FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace,
         LATERAL aclexplode(t.typacl) acl
    WHERE acl.grantee='filmy_app'::regrole
    UNION ALL
    SELECT 'unexpected language privilege: ' || l.lanname || ':' || acl.privilege_type
    FROM pg_language l,
         LATERAL aclexplode(l.lanacl) acl
    WHERE acl.grantee='filmy_app'::regrole
    UNION ALL
    SELECT 'filmy_app unexpectedly owns database: ' || datname
    FROM pg_database WHERE datdba='filmy_app'::regrole
    UNION ALL
    SELECT 'filmy_app unexpectedly owns schema: ' || nspname
    FROM pg_namespace WHERE nspowner='filmy_app'::regrole
    UNION ALL
    SELECT 'filmy_app unexpectedly owns relation: ' || n.nspname || '.' || c.relname
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE c.relowner='filmy_app'::regrole
    UNION ALL
    SELECT 'filmy_app unexpectedly owns function: ' || n.nspname || '.' || p.proname
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE p.proowner='filmy_app'::regrole
)
SELECT detail FROM violations ORDER BY detail
) TO STDOUT WITH (FORMAT csv);
""",
    ).stdout
    violations = [line for line in role_state.splitlines() if line.strip()]
    if violations:
        raise RuntimeError("Neočekávaná oprávnění role filmy_app: " + "; ".join(violations))

    smoke_id = "runtime-migration-smoke-" + secrets.token_hex(8)
    dml_sql = f"""
BEGIN;
INSERT INTO app.user_lists
    (id, slug, name, list_kind, ai_input_role, source_origin, created_at, updated_at)
VALUES ({_sql_literal(smoke_id)}, {_sql_literal(smoke_id)}, 'smoke', 'custom',
        'ignore', 'migration-smoke', clock_timestamp(), clock_timestamp());
UPDATE app.user_lists SET description='updated' WHERE id={_sql_literal(smoke_id)};
DELETE FROM app.user_lists WHERE id={_sql_literal(smoke_id)};
ROLLBACK;
"""
    _psql(app, sql=dml_sql)

    forbidden = (
        "BEGIN; CREATE TABLE app.runtime_migration_forbidden(id integer); ROLLBACK;",
        "BEGIN; CREATE SCHEMA runtime_migration_forbidden; ROLLBACK;",
        "SELECT count(*) FROM pg_authid;",
    )
    for sql in forbidden:
        result = _psql(app, sql=sql, check=False)
        if result.returncode == 0:
            raise RuntimeError("filmy_app neocekavane uspel v zakazane operaci")

    residue = _psql(
        target_admin,
        sql=(
            f"COPY (SELECT count(*) FROM app.user_lists WHERE id={_sql_literal(smoke_id)}) TO STDOUT;\n"
            "COPY (SELECT count(*) FROM pg_namespace WHERE nspname='runtime_migration_forbidden') TO STDOUT;\n"
            "COPY (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='app' AND c.relname='runtime_migration_forbidden') TO STDOUT;\n"
        ),
    ).stdout.splitlines()
    if [line.strip() for line in residue if line.strip()] != ["0", "0", "0"]:
        raise RuntimeError("Po privilege smoke testu zustal testovaci objekt nebo radek")
    print("Role OK: DML s rollbackem funguje; CREATE schema/table a pg_authid jsou zakazane.")


def _sanitized_error(error: subprocess.CalledProcessError, secrets_to_hide: tuple[str, ...]) -> str:
    text = error.stderr or ""
    return _sanitize_text(text, secrets_to_hide)


def _sanitize_text(text: str, secrets_to_hide: tuple[str, ...]) -> str:
    """Odstraní admin i dosud neuložený app secret ze všech chybových cest."""

    variants = {
        variant
        for secret in secrets_to_hide if secret
        for variant in (secret, secret.replace("'", "''"), _sql_literal(secret))
    }
    for variant in sorted(variants, key=len, reverse=True):
        text = text.replace(variant, "[REDACTED]")
    return " | ".join(line.strip() for line in text.splitlines()[-8:] if line.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="Jen overi existujici PostgreSQL data a roli proti aktualni DuckDB.",
    )
    arguments = parser.parse_args()
    admin: ConnectionConfig | None = None
    app: ConnectionConfig | None = None
    pending_app_password = ""
    try:
        values = _load_values()
        admin = _config("ADMIN", values)
        # Runner 000/001 nikdy neaplikuje překvapivě: pouze ověří jejich baseline.
        verify_bootstrap_baseline(admin)
        if arguments.check:
            app = _config("APP", values)
            target_admin = ConnectionConfig(
                admin.psql, admin.host, admin.port, TARGET_DATABASE, admin.user, admin.password
            )
            verify_schema_fingerprint(target_admin)
            with tempfile.TemporaryDirectory(prefix="filmy-runtime-check-") as temporary:
                source = export_source(Path(temporary))
                verify_data(admin, source)
            verify_role(admin, app)
            return

        pending_app_password = _validated_app_secret(admin, values)
        app = provision_role(admin, values, pending_app_password)
        apply_schema(admin)
        # Destruktivní import nesmí začít bez kompletního login/DML preflightu.
        verify_role(admin, app)
        with tempfile.TemporaryDirectory(prefix="filmy-runtime-migration-") as temporary:
            directory = Path(temporary)
            source = export_source(directory)
            import_snapshot(admin, directory)
            verify_data(admin, source)
        target_admin = ConnectionConfig(
            admin.psql, admin.host, admin.port, TARGET_DATABASE, admin.user, admin.password
        )
        verify_schema_fingerprint(target_admin)
        verify_role(admin, app)
        print("Runtime migrace dokoncena. Aplikace nadale pouziva DuckDB.")
    except subprocess.CalledProcessError as error:
        secrets_to_hide = tuple(
            config.password for config in (admin, app) if config is not None
        ) + (pending_app_password,)
        detail = _sanitized_error(error, secrets_to_hide)
        message = f"PostgreSQL prikaz selhal (kod {error.returncode})."
        if detail:
            message += f" {detail}"
        print(message, file=sys.stderr)
        raise SystemExit(1) from None
    except (duckdb.Error, OSError, RuntimeError, subprocess.SubprocessError) as error:
        secrets_to_hide = tuple(
            config.password for config in (admin, app) if config is not None
        ) + (pending_app_password,)
        detail = _sanitize_text(str(error), secrets_to_hide)
        print(f"Runtime migrace selhala: {detail}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
