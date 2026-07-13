from __future__ import annotations

"""Bootstrap, seed and one-off migration helpers extracted from `filmy.db`.

This module groups logic that prepares the local app state from historical
imports or older schema variants. It is intentionally separate from the normal
runtime read/write layer so `filmy.db` can stay focused on the active
application model while still exposing a stable facade.
"""

import importlib
from typing import Any

import duckdb


def _db():
    return importlib.import_module("filmy.db")


def ensure_duckdb_database() -> None:
    """Prepare the legacy DuckDB backend when an explicit rollback selects it."""

    db = _db()
    with duckdb.connect(db.DB_PATH.as_posix()) as conn:
        db._create_base_schema(conn)
        catalog_needs_refresh, manifest_needs_update = db._get_catalog_refresh_state(conn)
        if catalog_needs_refresh:
            db.refresh_catalog(conn)
        elif manifest_needs_update:
            db._store_imdb_file_manifest(conn)
            db._store_catalog_refresh_meta(conn)
        for ensure_fn, label in (
            (db._ensure_title_alias_lookup, "title_alias_lookup"),
            (db._ensure_title_lookup, "title_lookup"),
            (db._ensure_person_lookup, "person_lookup"),
        ):
            try:
                ensure_fn(conn)
            except duckdb.IOException as exc:
                if not db._is_no_space_duckdb_error(exc):
                    raise
                db.logger.warning("Skipping %s rebuild because disk is full.", label)
        db._migrate_watched_alias_list(conn)


def refresh_duckdb_catalog() -> dict[str, int]:
    """Run the legacy DuckDB catalog rebuild outside the normal runtime module."""

    db = _db()
    with duckdb.connect(db.DB_PATH.as_posix()) as conn:
        return db.refresh_catalog(conn)


def archive_import_reference_tables(conn: duckdb.DuckDBPyConnection) -> None:
    db = _db()
    tables = [
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
    ]
    for table_name in tables:
        app_exists = conn.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = 'app' AND table_name = ?
            """,
            [table_name],
        ).fetchone()[0]
        old_exists = conn.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = 'old' AND table_name = ?
            """,
            [table_name],
        ).fetchone()[0]
        if not app_exists or not old_exists:
            continue
        old_count = conn.execute(f"SELECT COUNT(*) FROM old.{table_name}").fetchone()[0]
        if old_count == 0:
            conn.execute(f"INSERT INTO old.{table_name} SELECT * FROM app.{table_name}")
        conn.execute(f"DROP TABLE app.{table_name}")


def migrate_legacy_watch_history(conn: duckdb.DuckDBPyConnection) -> None:
    db = _db()
    exists = conn.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = 'app' AND table_name = 'watch_history'
        """
    ).fetchone()[0]
    if exists == 0:
        return

    rows = conn.execute(
        """
        SELECT id, tconst, watched_on, source, rating, notes, created_at
        FROM app.watch_history
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            INSERT INTO app.watch_events (
                id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
            )
            SELECT ?, ?, 'title', ?, COALESCE(?, 'legacy_watch_history'), NULL, NULL, ?, ?, COALESCE(?, ?)
            WHERE NOT EXISTS (SELECT 1 FROM app.watch_events WHERE id = ?)
            """,
            [
                f"legacy-{row[0]}",
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                db._now_iso(),
                f"legacy-{row[0]}",
            ],
        )
    conn.execute("DROP TABLE app.watch_history")


def seed_local_library(conn: duckdb.DuckDBPyConnection) -> None:
    db = _db()
    seeded = (
        1
        if db.meta_backend_uses_postgres() and db.local_seed_exists("initial_import_unification")
        else conn.execute(
            "SELECT COUNT(*) FROM app.local_seed_meta WHERE seed_name = 'initial_import_unification'"
        ).fetchone()[0]
    )
    if seeded:
        return

    now = db._now_iso()
    watchlist_id = db._ensure_user_list(conn, "watchlist", "Watchlist", "watchlist", "seed_unified", "system:watchlist", now)

    for row in conn.execute(
        """
        SELECT tconst, position, title, original_title, created_at_src, description, your_rating, date_rated
        FROM old.imdb_watchlist_items
        WHERE is_active = TRUE
        """
    ).fetchall():
        db._upsert_user_list_item(
            conn,
            list_id=watchlist_id,
            canonical_key=db._canonical_media_key("title", row[0], row[0], None, None, None, None),
            tconst=row[0],
            media_type="title",
            imdb_id=row[0],
            tmdb_id=None,
            trakt_id=None,
            parent_tconst=None,
            parent_title=None,
            title=row[2] or row[3],
            season_number=None,
            episode_number=None,
            rank=row[1],
            added_at=row[4],
            notes=row[5],
            source_origin="seed_imdb_watchlist",
            source_ref=f"imdb_watchlist:{row[0]}",
            now=now,
        )
        if row[6] is not None:
            db._upsert_user_rating(
                conn,
                canonical_key=db._canonical_media_key("title", row[0], row[0], None, None, None, None),
                tconst=row[0],
                media_type="title",
                imdb_id=row[0],
                tmdb_id=None,
                trakt_id=None,
                parent_tconst=None,
                parent_title=None,
                title=row[2] or row[3],
                season_number=None,
                episode_number=None,
                rating=row[6],
                rated_at=row[7],
                source_origin="seed_imdb_watchlist",
                source_ref=f"imdb_watchlist_rating:{row[0]}",
                now=now,
            )

    for row in conn.execute(
        """
        SELECT source_key, media_type, trakt_id, imdb_id, tmdb_id, tconst, parent_title, title,
               season_number, episode_number, rank, listed_at, notes
        FROM old.trakt_list_items
        WHERE is_active = TRUE AND list_kind = 'watchlist'
        """
    ).fetchall():
        db._upsert_user_list_item(
            conn,
            list_id=watchlist_id,
            canonical_key=db._canonical_media_key(row[1], row[5], row[3], row[4], row[2], row[8], row[9]),
            tconst=row[5],
            media_type=row[1],
            imdb_id=row[3],
            tmdb_id=row[4],
            trakt_id=row[2],
            parent_tconst=None,
            parent_title=row[6],
            title=row[7],
            season_number=row[8],
            episode_number=row[9],
            rank=row[10],
            added_at=row[11],
            notes=row[12],
            source_origin="seed_trakt_watchlist",
            source_ref=row[0],
            now=now,
        )

    for row in conn.execute(
        """
        SELECT trakt_list_id, slug, name
        FROM old.trakt_lists
        WHERE is_active = TRUE
        """
    ).fetchall():
        list_id = db._ensure_user_list(
            conn,
            f"trakt-list-{row[0]}",
            row[2],
            "custom",
            "seed_trakt_list",
            str(row[0]),
            now,
            preferred_slug=row[1],
        )
        items = conn.execute(
            """
            SELECT source_key, media_type, trakt_id, imdb_id, tmdb_id, tconst, parent_title, title,
                   season_number, episode_number, rank, listed_at, notes
            FROM old.trakt_list_items
            WHERE is_active = TRUE AND list_kind = 'custom' AND trakt_list_id = ?
            """,
            [str(row[0])],
        ).fetchall()
        for item in items:
            db._upsert_user_list_item(
                conn,
                list_id=list_id,
                canonical_key=db._canonical_media_key(item[1], item[5], item[3], item[4], item[2], item[8], item[9]),
                tconst=item[5],
                media_type=item[1],
                imdb_id=item[3],
                tmdb_id=item[4],
                trakt_id=item[2],
                parent_tconst=None,
                parent_title=item[6],
                title=item[7],
                season_number=item[8],
                episode_number=item[9],
                rank=item[10],
                added_at=item[11],
                notes=item[12],
                source_origin="seed_trakt_list",
                source_ref=item[0],
                now=now,
            )

    for row in conn.execute(
        """
        SELECT source_key, media_type, trakt_id, imdb_id, tmdb_id, tconst, parent_title, title,
               season_number, episode_number, rating, rated_at
        FROM old.trakt_ratings
        WHERE is_active = TRUE
        """
    ).fetchall():
        db._upsert_user_rating(
            conn,
            canonical_key=db._canonical_media_key(row[1], row[5], row[3], row[4], row[2], row[8], row[9]),
            tconst=row[5],
            media_type=row[1],
            imdb_id=row[3],
            tmdb_id=row[4],
            trakt_id=row[2],
            parent_tconst=None,
            parent_title=row[6],
            title=row[7],
            season_number=row[8],
            episode_number=row[9],
            rating=row[10],
            rated_at=row[11],
            source_origin="seed_trakt_rating",
            source_ref=row[0],
            now=now,
        )

    for row in conn.execute(
        """
        SELECT nconst, name, known_for, birth_date
        FROM old.imdb_favorite_people
        WHERE is_active = TRUE
        """
    ).fetchall():
        conn.execute(
            """
            INSERT INTO app.user_people (
                person_key, nconst, name, known_for, birth_date, source_origin, source_ref, is_favorite, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'seed_imdb_favorite_person', ?, TRUE, ?, ?)
            ON CONFLICT (person_key) DO NOTHING
            """,
            [f"nconst:{row[0]}", row[0], row[1], row[2], row[3], row[0], now, now],
        )

    if db.meta_backend_uses_postgres():
        db.record_local_seed_meta(
            seed_name="initial_import_unification",
            seeded_at=now,
            note="Unified imported IMDb/Trakt records into local user_* tables.",
        )
    else:
        conn.execute(
            """
            INSERT INTO app.local_seed_meta (seed_name, seeded_at, note)
            VALUES ('initial_import_unification', ?, ?)
            """,
            [now, "Unified imported IMDb/Trakt records into local user_* tables."],
        )


def migrate_watched_alias_list(conn: duckdb.DuckDBPyConnection) -> None:
    watched_list_ids = [
        row[0]
        for row in conn.execute(
            """
            SELECT id
            FROM app.user_lists
            WHERE source_ref = 'system:watched-alias'
               OR slug = 'videl-jsem'
               OR name = 'Viděl jsem'
            """
        ).fetchall()
    ]
    if not watched_list_ids:
        return

    for list_id in watched_list_ids:
        conn.execute("DELETE FROM app.user_list_items WHERE list_id = ?", [list_id])
        conn.execute("DELETE FROM app.user_lists WHERE id = ?", [list_id])
