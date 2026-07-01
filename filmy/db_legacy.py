from __future__ import annotations

"""Historical import and legacy-source operations extracted from `filmy.db`.

This module groups public read/sync functions for Trakt, IMDb CSV exports and
Plex bootstrap sync. The implementation still delegates to shared helpers in
`filmy.db`, which keeps the refactor low-risk while reducing the size and
responsibility surface of the facade module.
"""

import importlib
import json
import uuid
from typing import Any


def _db():
    return importlib.import_module("filmy.db")


def inspect_trakt_export(export_dir: str = "trakt-export") -> dict[str, Any]:
    db = _db()
    export_path = db._resolve_export_path(export_dir)
    files = [db._describe_trakt_file(path) for path in sorted(export_path.glob("*.json"))]
    category_counts: dict[str, int] = {}
    importable_categories = {
        "watched_history",
        "ratings",
        "custom_lists",
        "watchlist",
        "collection",
        "list_metadata",
        "last_activities",
    }
    for item in files:
        category_counts[item["category"]] = category_counts.get(item["category"], 0) + 1

    return {
        "export_dir": export_path.as_posix(),
        "fingerprint": db._fingerprint_trakt_files(files),
        "file_count": len(files),
        "categories": category_counts,
        "supported_categories": sorted(importable_categories),
        "files": files,
    }


def sync_trakt_export(export_dir: str = "trakt-export") -> dict[str, Any]:
    db = _db()
    inspection = inspect_trakt_export(export_dir)
    if inspection["file_count"] == 0:
        raise ValueError("Adresář trakt exportu je prázdný.")

    with db.duckdb.connect(db.DB_PATH.as_posix()) as conn:
        db._create_base_schema(conn)
        latest = conn.execute(
            """
            SELECT id
            FROM old.trakt_sync_runs
            WHERE export_fingerprint = ? AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [inspection["fingerprint"]],
        ).fetchone()
        if latest is not None:
            db._backfill_trakt_snapshots_for_run(conn, latest[0])
            return {
                "status": "unchanged",
                "sync_run_id": latest[0],
                "export_dir": inspection["export_dir"],
                "fingerprint": inspection["fingerprint"],
            }

        sync_run_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO old.trakt_sync_runs (id, export_path, export_fingerprint, status, summary_json, created_at)
            VALUES (?, ?, ?, 'running', ?, ?)
            """,
            [sync_run_id, inspection["export_dir"], inspection["fingerprint"], json.dumps(inspection), db._now_iso()],
        )
        for item in inspection["files"]:
            conn.execute(
                """
                INSERT INTO old.trakt_sync_files (
                    sync_run_id, relative_path, file_size, file_mtime, file_sha256, category, item_count, imported
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    sync_run_id,
                    item["relative_path"],
                    item["size"],
                    item["mtime"],
                    item["sha256"],
                    item["category"],
                    item["item_count"],
                    item["category"] in {"watched_history", "ratings", "custom_lists", "watchlist", "collection", "list_metadata", "last_activities"},
                ],
            )

        files_by_category: dict[str, list[dict[str, Any]]] = {}
        for item in inspection["files"]:
            files_by_category.setdefault(item["category"], []).append(item)

        summary = {
            "history_events": db._sync_trakt_history(conn, sync_run_id, files_by_category.get("watched_history", [])),
            "ratings": db._sync_trakt_ratings(conn, sync_run_id, files_by_category.get("ratings", [])),
            "lists": db._sync_trakt_lists(
                conn,
                sync_run_id,
                files_by_category.get("list_metadata", []),
                files_by_category.get("custom_lists", []),
                files_by_category.get("watchlist", []),
            ),
            "collection": db._sync_trakt_collection(conn, sync_run_id, files_by_category.get("collection", [])),
            "last_activities": db._read_last_activities(files_by_category.get("last_activities", [])),
        }
        result = {
            "status": "completed",
            "sync_run_id": sync_run_id,
            "export_dir": inspection["export_dir"],
            "fingerprint": inspection["fingerprint"],
            "summary": summary,
        }
        conn.execute(
            "UPDATE old.trakt_sync_runs SET status = 'completed', summary_json = ? WHERE id = ?",
            [json.dumps(result, ensure_ascii=False), sync_run_id],
        )
        return result


def get_trakt_sync_runs(limit: int = 20) -> list[dict[str, Any]]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT id, export_path, export_fingerprint, status, summary_json, created_at
            FROM old.trakt_sync_runs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
    return [
        {
            "id": row[0],
            "export_path": row[1],
            "export_fingerprint": row[2],
            "status": row[3],
            "summary": db._loads_json_or_none(row[4]),
            "created_at": row[5],
        }
        for row in rows
    ]


def get_trakt_sync_run(sync_run_id: str) -> dict[str, Any] | None:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        run = conn.execute(
            """
            SELECT id, export_path, export_fingerprint, status, summary_json, created_at
            FROM old.trakt_sync_runs
            WHERE id = ?
            """,
            [sync_run_id],
        ).fetchone()
        if run is None:
            return None
        files = conn.execute(
            """
            SELECT relative_path, file_size, file_mtime, file_sha256, category, item_count, imported
            FROM old.trakt_sync_files
            WHERE sync_run_id = ?
            ORDER BY relative_path
            """,
            [sync_run_id],
        ).fetchall()
    return {
        "id": run[0],
        "export_path": run[1],
        "export_fingerprint": run[2],
        "status": run[3],
        "summary": db._loads_json_or_none(run[4]),
        "created_at": run[5],
        "files": [
            {
                "relative_path": row[0],
                "file_size": row[1],
                "file_mtime": row[2],
                "file_sha256": row[3],
                "category": row[4],
                "item_count": row[5],
                "imported": row[6],
            }
            for row in files
        ],
    }


def get_trakt_sync_changes(
    sync_run_id: str | None = None,
    previous_sync_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        if sync_run_id is None:
            current = conn.execute(
                """
                SELECT id, created_at
                FROM old.trakt_sync_runs
                WHERE status = 'completed'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        else:
            current = conn.execute(
                """
                SELECT id, created_at
                FROM old.trakt_sync_runs
                WHERE id = ? AND status = 'completed'
                """,
                [sync_run_id],
            ).fetchone()
        if current is None:
            return {"current_sync_id": sync_run_id, "previous_sync_id": None, "changes": {}}

        if previous_sync_id is not None:
            previous = conn.execute(
                """
                SELECT id, created_at
                FROM old.trakt_sync_runs
                WHERE id = ? AND status = 'completed'
                """,
                [previous_sync_id],
            ).fetchone()
        else:
            previous = conn.execute(
                """
                SELECT id, created_at
                FROM old.trakt_sync_runs
                WHERE status = 'completed' AND created_at < ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [current[1]],
            ).fetchone()
        previous_id = previous[0] if previous else None
        if previous_id and db._has_trakt_snapshot(conn, "old.trakt_history_snapshot", current[0]):
            changes = {
                "history_added": db._snapshot_change_rows(
                    conn,
                    "old.trakt_history_snapshot",
                    "history_id",
                    "old.trakt_history_events",
                    "history_id",
                    "media_type",
                    "parent_title",
                    "title",
                    "watched_at",
                    current[0],
                    previous_id,
                    limit,
                ),
                "history_removed": db._snapshot_change_rows(
                    conn,
                    "old.trakt_history_snapshot",
                    "history_id",
                    "old.trakt_history_events",
                    "history_id",
                    "media_type",
                    "parent_title",
                    "title",
                    "watched_at",
                    previous_id,
                    current[0],
                    limit,
                ),
                "ratings_changed": db._snapshot_change_rows(
                    conn,
                    "old.trakt_ratings_snapshot",
                    "source_key",
                    "old.trakt_ratings",
                    "source_key",
                    "media_type",
                    "parent_title",
                    "title",
                    "rated_at",
                    current[0],
                    previous_id,
                    limit,
                ),
                "list_items_changed": db._snapshot_change_rows(
                    conn,
                    "old.trakt_list_items_snapshot",
                    "source_key",
                    "old.trakt_list_items",
                    "source_key",
                    "media_type",
                    "list_name",
                    "title",
                    "listed_at",
                    current[0],
                    previous_id,
                    limit,
                ),
                "collection_changed": db._snapshot_change_rows(
                    conn,
                    "old.trakt_collection_snapshot",
                    "source_key",
                    "old.trakt_collection_items",
                    "source_key",
                    "media_type",
                    "parent_title",
                    "title",
                    "updated_at",
                    current[0],
                    previous_id,
                    limit,
                ),
            }
            counts = {
                "history_added": db._snapshot_change_count(conn, "old.trakt_history_snapshot", "history_id", current[0], previous_id),
                "history_removed": db._snapshot_change_count(conn, "old.trakt_history_snapshot", "history_id", previous_id, current[0]),
                "ratings_changed": db._snapshot_change_count(conn, "old.trakt_ratings_snapshot", "source_key", current[0], previous_id),
                "list_items_changed": db._snapshot_change_count(conn, "old.trakt_list_items_snapshot", "source_key", current[0], previous_id),
                "collection_changed": db._snapshot_change_count(conn, "old.trakt_collection_snapshot", "source_key", current[0], previous_id),
            }
        else:
            changes = {
                "history_added": db._fetch_change_rows(
                    conn,
                    """
                    SELECT history_id AS entity_id, media_type, parent_title, title, watched_at AS changed_at
                    FROM old.trakt_history_events
                    WHERE is_active = TRUE AND last_seen_sync_id = ?
                    ORDER BY watched_at DESC
                    LIMIT ?
                    """,
                    [current[0], limit],
                ),
                "history_removed": [],
                "ratings_changed": db._fetch_change_rows(
                    conn,
                    """
                    SELECT source_key AS entity_id, media_type, parent_title, title, rated_at AS changed_at
                    FROM old.trakt_ratings
                    WHERE is_active = TRUE AND last_seen_sync_id = ?
                    ORDER BY rated_at DESC
                    LIMIT ?
                    """,
                    [current[0], limit],
                ),
                "list_items_changed": db._fetch_change_rows(
                    conn,
                    """
                    SELECT source_key AS entity_id, media_type, list_name AS parent_title, title, listed_at AS changed_at
                    FROM old.trakt_list_items
                    WHERE is_active = TRUE AND last_seen_sync_id = ?
                    ORDER BY listed_at DESC NULLS LAST, rank NULLS LAST
                    LIMIT ?
                    """,
                    [current[0], limit],
                ),
                "collection_changed": db._fetch_change_rows(
                    conn,
                    """
                    SELECT source_key AS entity_id, media_type, parent_title, title, updated_at AS changed_at
                    FROM old.trakt_collection_items
                    WHERE is_active = TRUE AND last_seen_sync_id = ?
                    ORDER BY updated_at DESC NULLS LAST, collected_at DESC NULLS LAST
                    LIMIT ?
                    """,
                    [current[0], limit],
                ),
            }
            counts = {
                "history_added": conn.execute(
                    "SELECT COUNT(*) FROM old.trakt_history_events WHERE is_active = TRUE AND last_seen_sync_id = ?",
                    [current[0]],
                ).fetchone()[0],
                "history_removed": 0,
                "ratings_changed": conn.execute(
                    "SELECT COUNT(*) FROM old.trakt_ratings WHERE is_active = TRUE AND last_seen_sync_id = ?",
                    [current[0]],
                ).fetchone()[0],
                "list_items_changed": conn.execute(
                    "SELECT COUNT(*) FROM old.trakt_list_items WHERE is_active = TRUE AND last_seen_sync_id = ?",
                    [current[0]],
                ).fetchone()[0],
                "collection_changed": conn.execute(
                    "SELECT COUNT(*) FROM old.trakt_collection_items WHERE is_active = TRUE AND last_seen_sync_id = ?",
                    [current[0]],
                ).fetchone()[0],
            }
    return {
        "current_sync_id": current[0],
        "previous_sync_id": previous_id,
        "counts": counts,
        "changes": changes,
    }


def get_trakt_ratings(limit: int = 100, active_only: bool = True) -> list[dict[str, Any]]:
    db = _db()
    sql = """
        SELECT source_key, media_type, trakt_id, imdb_id, tmdb_id, tconst, parent_title, title,
               season_number, episode_number, rating, rated_at, is_active, last_seen_sync_id
        FROM old.trakt_ratings
        WHERE (? = FALSE OR is_active = TRUE)
        ORDER BY rated_at DESC
        LIMIT ?
    """
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(sql, [active_only, limit]).fetchall()
    return [
        {
            "source_key": row[0],
            "media_type": row[1],
            "trakt_id": row[2],
            "imdb_id": row[3],
            "tmdb_id": row[4],
            "tconst": row[5],
            "parent_title": row[6],
            "title": row[7],
            "season_number": row[8],
            "episode_number": row[9],
            "rating": row[10],
            "rated_at": row[11],
            "is_active": row[12],
            "last_seen_sync_id": row[13],
        }
        for row in rows
    ]


def get_trakt_list_overview(include_items: bool = False, active_only: bool = True) -> dict[str, Any]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        lists = conn.execute(
            """
            SELECT trakt_list_id, slug, name, description, privacy, list_type, item_count, updated_at, is_active, last_seen_sync_id
            FROM old.trakt_lists
            WHERE (? = FALSE OR is_active = TRUE)
            ORDER BY name
            """,
            [active_only],
        ).fetchall()
        watchlist_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM old.trakt_list_items
            WHERE list_kind = 'watchlist' AND (? = FALSE OR is_active = TRUE)
            """,
            [active_only],
        ).fetchone()[0]

        result_lists = []
        for row in lists:
            item = {
                "trakt_list_id": row[0],
                "slug": row[1],
                "name": row[2],
                "description": row[3],
                "privacy": row[4],
                "list_type": row[5],
                "item_count": row[6],
                "updated_at": row[7],
                "is_active": row[8],
                "last_seen_sync_id": row[9],
            }
            if include_items:
                items = conn.execute(
                    """
                    SELECT source_key, media_type, imdb_id, tmdb_id, tconst, parent_title, title,
                           season_number, episode_number, rank, listed_at, notes, my_rating, is_active
                    FROM old.trakt_list_items
                    WHERE trakt_list_id = ? AND (? = FALSE OR is_active = TRUE)
                    ORDER BY rank NULLS LAST, listed_at DESC NULLS LAST
                    """,
                    [str(row[0]), active_only],
                ).fetchall()
                item["items"] = [db._trakt_list_item_row_to_dict(r) for r in items]
            result_lists.append(item)

        watchlist_items = []
        if include_items:
            watchlist_rows = conn.execute(
                """
                SELECT source_key, media_type, imdb_id, tmdb_id, tconst, parent_title, title,
                       season_number, episode_number, rank, listed_at, notes, my_rating, is_active
                FROM old.trakt_list_items
                WHERE list_kind = 'watchlist' AND (? = FALSE OR is_active = TRUE)
                ORDER BY rank NULLS LAST, listed_at DESC NULLS LAST
                """,
                [active_only],
            ).fetchall()
            watchlist_items = [db._trakt_list_item_row_to_dict(r) for r in watchlist_rows]

    return {
        "lists": result_lists,
        "watchlist": {
            "item_count": watchlist_count,
            "items": watchlist_items if include_items else None,
        },
    }


def get_trakt_collection(limit: int = 100, active_only: bool = True) -> list[dict[str, Any]]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT source_key, media_type, trakt_id, imdb_id, tmdb_id, tconst, parent_title, title,
                   season_number, episode_number, collected_at, updated_at, is_active, last_seen_sync_id
            FROM old.trakt_collection_items
            WHERE (? = FALSE OR is_active = TRUE)
            ORDER BY updated_at DESC NULLS LAST, collected_at DESC NULLS LAST
            LIMIT ?
            """,
            [active_only, limit],
        ).fetchall()
    return [
        {
            "source_key": row[0],
            "media_type": row[1],
            "trakt_id": row[2],
            "imdb_id": row[3],
            "tmdb_id": row[4],
            "tconst": row[5],
            "parent_title": row[6],
            "title": row[7],
            "season_number": row[8],
            "episode_number": row[9],
            "collected_at": row[10],
            "updated_at": row[11],
            "is_active": row[12],
            "last_seen_sync_id": row[13],
        }
        for row in rows
    ]


def get_trakt_status() -> dict[str, Any]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        latest = conn.execute(
            """
            SELECT id, export_path, export_fingerprint, status, summary_json, created_at
            FROM old.trakt_sync_runs
            WHERE status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        if latest is None:
            return {
                "latest_sync": None,
                "counts": {
                    "history_events_active": 0,
                    "ratings_active": 0,
                    "lists_active": 0,
                    "watchlist_active": 0,
                    "collection_active": 0,
                },
                "last_activities": None,
            }

        summary = db._loads_json_or_none(latest[4]) or {}
        nested_summary = summary.get("summary") or {}
        counts = {
            "history_events_active": conn.execute(
                "SELECT COUNT(*) FROM old.trakt_history_events WHERE is_active = TRUE"
            ).fetchone()[0],
            "ratings_active": conn.execute(
                "SELECT COUNT(*) FROM old.trakt_ratings WHERE is_active = TRUE"
            ).fetchone()[0],
            "lists_active": conn.execute(
                "SELECT COUNT(*) FROM old.trakt_lists WHERE is_active = TRUE"
            ).fetchone()[0],
            "watchlist_active": conn.execute(
                "SELECT COUNT(*) FROM old.trakt_list_items WHERE is_active = TRUE AND list_kind = 'watchlist'"
            ).fetchone()[0],
            "collection_active": conn.execute(
                "SELECT COUNT(*) FROM old.trakt_collection_items WHERE is_active = TRUE"
            ).fetchone()[0],
        }
    return {
        "latest_sync": {
            "id": latest[0],
            "export_path": latest[1],
            "export_fingerprint": latest[2],
            "status": latest[3],
            "created_at": latest[5],
            "summary": nested_summary,
        },
        "counts": counts,
        "last_activities": nested_summary.get("last_activities"),
    }


def inspect_imdb_lists(export_dir: str = "imdb_lists") -> dict[str, Any]:
    db = _db()
    export_path = db._resolve_export_path(export_dir)
    files: list[dict[str, Any]] = []
    for path in sorted(export_path.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        item_count = db._count_csv_rows(path)
        files.append(
            {
                "name": path.name,
                "path": path.as_posix(),
                "relative_path": path.name,
                "category": db._categorize_imdb_list_file(path.name),
                "item_count": item_count,
                "size": path.stat().st_size,
                "mtime": int(path.stat().st_mtime),
                "sha256": db._file_sha256(path),
            }
        )
    return {
        "export_dir": export_path.as_posix(),
        "fingerprint": db._fingerprint_trakt_files(files),
        "file_count": len(files),
        "files": files,
    }


def sync_imdb_lists(export_dir: str = "imdb_lists") -> dict[str, Any]:
    db = _db()
    inspection = inspect_imdb_lists(export_dir)
    if inspection["file_count"] == 0:
        raise ValueError("Adresář imdb_lists je prázdný.")

    with db.duckdb.connect(db.DB_PATH.as_posix()) as conn:
        db._create_base_schema(conn)
        latest = conn.execute(
            """
            SELECT id
            FROM old.imdb_list_sync_runs
            WHERE export_fingerprint = ? AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [inspection["fingerprint"]],
        ).fetchone()
        if latest is not None:
            return {
                "status": "unchanged",
                "sync_run_id": latest[0],
                "export_dir": inspection["export_dir"],
                "fingerprint": inspection["fingerprint"],
            }

        sync_run_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO old.imdb_list_sync_runs (id, export_path, export_fingerprint, status, summary_json, created_at)
            VALUES (?, ?, ?, 'running', ?, ?)
            """,
            [sync_run_id, inspection["export_dir"], inspection["fingerprint"], json.dumps(inspection), db._now_iso()],
        )

        files_by_category = {item["category"]: item for item in inspection["files"]}
        summary = {
            "watchlist": db._sync_imdb_watchlist(conn, sync_run_id, files_by_category.get("watchlist")),
            "favorite_people": db._sync_imdb_favorite_people(conn, sync_run_id, files_by_category.get("favorite_people")),
        }
        result = {
            "status": "completed",
            "sync_run_id": sync_run_id,
            "export_dir": inspection["export_dir"],
            "fingerprint": inspection["fingerprint"],
            "summary": summary,
        }
        conn.execute(
            "UPDATE old.imdb_list_sync_runs SET status = 'completed', summary_json = ? WHERE id = ?",
            [json.dumps(result, ensure_ascii=False), sync_run_id],
        )
        return result


def get_imdb_lists_status() -> dict[str, Any]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        latest = conn.execute(
            """
            SELECT id, export_path, export_fingerprint, status, summary_json, created_at
            FROM old.imdb_list_sync_runs
            WHERE status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        counts = {
            "watchlist_active": conn.execute(
                "SELECT COUNT(*) FROM old.imdb_watchlist_items WHERE is_active = TRUE"
            ).fetchone()[0],
            "favorite_people_active": conn.execute(
                "SELECT COUNT(*) FROM old.imdb_favorite_people WHERE is_active = TRUE"
            ).fetchone()[0],
        }
    return {
        "latest_sync": (
            {
                "id": latest[0],
                "export_path": latest[1],
                "export_fingerprint": latest[2],
                "status": latest[3],
                "summary": (db._loads_json_or_none(latest[4]) or {}).get("summary"),
                "created_at": latest[5],
            }
            if latest
            else None
        ),
        "counts": counts,
    }


def get_imdb_watchlist(limit: int = 100, active_only: bool = True) -> list[dict[str, Any]]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT tconst, position, title, original_title, title_type, imdb_rating, runtime_minutes, year,
                   genres, num_votes, release_date, directors, your_rating, date_rated, is_active, last_seen_sync_id
            FROM old.imdb_watchlist_items
            WHERE (? = FALSE OR is_active = TRUE)
            ORDER BY position ASC NULLS LAST, title
            LIMIT ?
            """,
            [active_only, limit],
        ).fetchall()
    return [
        {
            "tconst": row[0],
            "position": row[1],
            "title": row[2],
            "original_title": row[3],
            "title_type": row[4],
            "imdb_rating": row[5],
            "runtime_minutes": row[6],
            "year": row[7],
            "genres": row[8],
            "num_votes": row[9],
            "release_date": row[10],
            "directors": row[11],
            "your_rating": row[12],
            "date_rated": row[13],
            "is_active": row[14],
            "last_seen_sync_id": row[15],
        }
        for row in rows
    ]


def get_imdb_favorite_people(limit: int = 100, active_only: bool = True) -> list[dict[str, Any]]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT nconst, position, name, known_for, birth_date, is_active, last_seen_sync_id
            FROM old.imdb_favorite_people
            WHERE (? = FALSE OR is_active = TRUE)
            ORDER BY position ASC NULLS LAST, name
            LIMIT ?
            """,
            [active_only, limit],
        ).fetchall()
    return [
        {
            "nconst": row[0],
            "position": row[1],
            "name": row[2],
            "known_for": row[3],
            "birth_date": row[4],
            "is_active": row[5],
            "last_seen_sync_id": row[6],
        }
        for row in rows
    ]


def inspect_plex_source() -> dict[str, Any]:
    db = _db()
    server = db.get_primary_server()
    if server is None:
        return {"server": None, "sections": [], "fingerprint": None}

    sections = [
        section
        for section in db.get_library_sections(server)
        if section.get("type") in {"movie", "show"} and section.get("hidden") != "1"
    ]
    fingerprint = db._plex_fingerprint(server.client_identifier, sections)
    return {
        "server": {
            "name": server.name,
            "client_identifier": server.client_identifier,
        },
        "sections": sections,
        "fingerprint": fingerprint,
    }


def sync_plex_source(section_limit: int | None = None, item_limit_per_section: int | None = None) -> dict[str, Any]:
    db = _db()
    plex_server = db.get_primary_server()
    inspection = inspect_plex_source()
    server = inspection["server"]
    if server is None or plex_server is None:
        raise ValueError("Nebyl nalezen žádný dostupný Plex Media Server.")

    sections = inspection["sections"]
    if section_limit is not None:
        sections = sections[:section_limit]

    with db.duckdb.connect(db.DB_PATH.as_posix()) as conn:
        db._create_base_schema(conn)
        latest = conn.execute(
            """
            SELECT id
            FROM old.plex_sync_runs
            WHERE source_fingerprint = ? AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [inspection["fingerprint"]],
        ).fetchone()
        if latest is not None:
            return {
                "status": "unchanged",
                "sync_run_id": latest[0],
                "summary": db._loads_json_or_none(
                    conn.execute("SELECT summary_json FROM old.plex_sync_runs WHERE id = ?", [latest[0]]).fetchone()[0]
                ),
            }

        sync_run_id = str(uuid.uuid4())
        now = db._now_iso()
        conn.execute(
            """
            INSERT INTO old.plex_sync_runs (
                id, server_name, server_client_identifier, source_fingerprint, status, summary_json, created_at
            )
            VALUES (?, ?, ?, ?, 'running', '{}', ?)
            """,
            [sync_run_id, server["name"], server["client_identifier"], inspection["fingerprint"], now],
        )
        plex_list_id = db._ensure_user_list(
            conn,
            "plex-library",
            "Plex Library",
            "custom",
            "seed_plex_library",
            f"plex:{server['client_identifier']}",
            now,
            preferred_slug="plex-library",
        )

        summary = {
            "server_name": server["name"],
            "server_client_identifier": server["client_identifier"],
            "sections_processed": 0,
            "items_imported": 0,
            "library_items_upserted": 0,
            "watch_events_upserted": 0,
            "content_state_updates": 0,
        }

        for section in sections:
            summary["sections_processed"] += 1
            items = db.iter_section_items(section["key"], resource=plex_server, limit=item_limit_per_section)
            for item in items:
                rating_key = item.get("rating_key")
                if not rating_key:
                    continue
                snapshot = db.get_metadata_snapshot(rating_key, resource=plex_server)
                if snapshot is None:
                    continue
                db._upsert_plex_library_item(conn, sync_run_id, section, snapshot)
                summary["items_imported"] += 1

                if db._sync_plex_item_to_local_library(conn, plex_list_id, sync_run_id, snapshot, now):
                    summary["library_items_upserted"] += 1
                if db._sync_plex_watch_state(conn, snapshot):
                    summary["watch_events_upserted"] += 1
                if db._sync_plex_content_state(conn, snapshot, now):
                    summary["content_state_updates"] += 1

        conn.execute("UPDATE old.plex_library_items SET is_active = FALSE WHERE last_seen_sync_id <> ?", [sync_run_id])
        conn.execute(
            """
            UPDATE app.user_list_items
            SET is_archived = TRUE, updated_at = ?
            WHERE list_id = ? AND source_origin = 'seed_plex_library' AND source_ref NOT IN (
                SELECT source_key FROM old.plex_library_items WHERE last_seen_sync_id = ? AND is_active = TRUE
            )
            """,
            [now, plex_list_id, sync_run_id],
        )
        conn.execute(
            "UPDATE old.plex_sync_runs SET status = 'completed', summary_json = ? WHERE id = ?",
            [json.dumps(summary, ensure_ascii=False), sync_run_id],
        )

    return {"status": "completed", "sync_run_id": sync_run_id, "summary": summary}


def get_plex_status() -> dict[str, Any]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        latest = conn.execute(
            """
            SELECT id, server_name, server_client_identifier, source_fingerprint, status, summary_json, created_at
            FROM old.plex_sync_runs
            WHERE status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        if latest is None:
            return {
                "latest_sync": None,
                "counts": {
                    "active_library_items": 0,
                    "mapped_imdb_items": 0,
                    "mapped_tmdb_items": 0,
                    "watched_items": 0,
                },
            }

        counts = {
            "active_library_items": conn.execute(
                "SELECT COUNT(*) FROM old.plex_library_items WHERE is_active = TRUE"
            ).fetchone()[0],
            "mapped_imdb_items": conn.execute(
                "SELECT COUNT(*) FROM old.plex_library_items WHERE is_active = TRUE AND imdb_id IS NOT NULL"
            ).fetchone()[0],
            "mapped_tmdb_items": conn.execute(
                "SELECT COUNT(*) FROM old.plex_library_items WHERE is_active = TRUE AND tmdb_id IS NOT NULL"
            ).fetchone()[0],
            "watched_items": conn.execute(
                """
                SELECT COUNT(*)
                FROM old.plex_library_items
                WHERE is_active = TRUE
                  AND (
                    COALESCE(view_count, 0) > 0
                    OR (
                        COALESCE(viewed_leaf_count, 0) > 0
                        AND viewed_leaf_count = leaf_count
                    )
                  )
                """
            ).fetchone()[0],
        }
    return {
        "latest_sync": {
            "id": latest[0],
            "server_name": latest[1],
            "server_client_identifier": latest[2],
            "source_fingerprint": latest[3],
            "status": latest[4],
            "summary": db._loads_json_or_none(latest[5]),
            "created_at": latest[6],
        },
        "counts": counts,
    }


def _upsert_plex_library_item(
    conn,
    sync_run_id: str,
    section: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    db = _db()
    ids = snapshot.get("ids") or {}
    imdb_id = ids.get("imdb")
    tconst = None
    if imdb_id:
        found = conn.execute("SELECT tconst FROM app.catalog_titles WHERE tconst = ?", [imdb_id]).fetchone()
        if found:
            tconst = found[0]

    source_key = db._plex_source_key(snapshot["rating_key"])
    conn.execute(
        """
        INSERT INTO old.plex_library_items (
            source_key,
            plex_rating_key,
            plex_guid,
            section_key,
            section_title,
            library_type,
            title,
            year,
            imdb_id,
            tmdb_id,
            tvdb_id,
            tconst,
            view_count,
            viewed_leaf_count,
            leaf_count,
            last_viewed_at,
            added_at_src,
            updated_at_src,
            originally_available_at,
            directors_json,
            roles_json,
            genres_json,
            countries_json,
            is_active,
            last_seen_sync_id,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
        ON CONFLICT (source_key) DO UPDATE SET
            plex_guid = excluded.plex_guid,
            section_key = excluded.section_key,
            section_title = excluded.section_title,
            library_type = excluded.library_type,
            title = excluded.title,
            year = excluded.year,
            imdb_id = excluded.imdb_id,
            tmdb_id = excluded.tmdb_id,
            tvdb_id = excluded.tvdb_id,
            tconst = excluded.tconst,
            view_count = excluded.view_count,
            viewed_leaf_count = excluded.viewed_leaf_count,
            leaf_count = excluded.leaf_count,
            last_viewed_at = excluded.last_viewed_at,
            added_at_src = excluded.added_at_src,
            updated_at_src = excluded.updated_at_src,
            originally_available_at = excluded.originally_available_at,
            directors_json = excluded.directors_json,
            roles_json = excluded.roles_json,
            genres_json = excluded.genres_json,
            countries_json = excluded.countries_json,
            is_active = TRUE,
            last_seen_sync_id = excluded.last_seen_sync_id,
            raw_json = excluded.raw_json
        """,
        [
            source_key,
            snapshot["rating_key"],
            snapshot.get("guid"),
            section.get("key"),
            section.get("title"),
            snapshot.get("type") or section.get("type"),
            snapshot.get("title"),
            db._safe_int(snapshot.get("year")),
            imdb_id,
            db._safe_int(ids.get("tmdb")),
            db._safe_int(ids.get("tvdb")),
            tconst,
            db._safe_int(snapshot.get("view_count")),
            db._safe_int(snapshot.get("viewed_leaf_count")),
            db._safe_int(snapshot.get("leaf_count")),
            db._parse_unix_timestamp(snapshot.get("last_viewed_at")),
            db._parse_unix_timestamp(snapshot.get("added_at")),
            db._parse_unix_timestamp(snapshot.get("updated_at")),
            snapshot.get("originally_available_at"),
            json.dumps(snapshot.get("directors") or [], ensure_ascii=False),
            json.dumps(snapshot.get("roles") or [], ensure_ascii=False),
            json.dumps(snapshot.get("genres") or [], ensure_ascii=False),
            json.dumps(snapshot.get("countries") or [], ensure_ascii=False),
            sync_run_id,
            json.dumps(snapshot, ensure_ascii=False),
        ],
    )


def _sync_plex_item_to_local_library(
    conn,
    list_id: str,
    sync_run_id: str,
    snapshot: dict[str, Any],
    now: str,
) -> bool:
    db = _db()
    ids = snapshot.get("ids") or {}
    imdb_id = ids.get("imdb")
    tmdb_id = db._safe_int(ids.get("tmdb"))
    tconst = imdb_id if imdb_id and conn.execute("SELECT COUNT(*) FROM app.catalog_titles WHERE tconst = ?", [imdb_id]).fetchone()[0] else None
    if tconst is None and imdb_id is None and tmdb_id is None:
        return False
    canonical_key = db._canonical_media_key("title", tconst, imdb_id, tmdb_id, None, None, None)

    db._upsert_user_list_item(
        conn,
        list_id=list_id,
        canonical_key=canonical_key,
        tconst=tconst,
        media_type="title",
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        trakt_id=None,
        parent_tconst=None,
        parent_title=None,
        title=snapshot.get("title"),
        season_number=None,
        episode_number=None,
        rank=None,
        added_at=db._parse_unix_timestamp(snapshot.get("added_at")) or now,
        notes=f"plex_sync:{sync_run_id}",
        source_origin="seed_plex_library",
        source_ref=db._plex_source_key(snapshot["rating_key"]),
        now=now,
    )
    return True


def _sync_plex_watch_state(conn, snapshot: dict[str, Any]) -> bool:
    db = _db()
    ids = snapshot.get("ids") or {}
    imdb_id = ids.get("imdb")
    if not imdb_id:
        return False
    found = conn.execute("SELECT COUNT(*) FROM app.catalog_titles WHERE tconst = ?", [imdb_id]).fetchone()[0]
    if not found or not db._plex_item_is_watched(snapshot):
        return False

    watched_at = db._parse_unix_timestamp(snapshot.get("last_viewed_at")) or db._now_iso()
    watched_on = watched_at[:10]
    event_id = f"plex-watch-{snapshot['rating_key']}"
    conn.execute(
        """
        INSERT INTO app.watch_events (
            id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
        )
        VALUES (?, ?, 'title', ?, 'plex', NULL, NULL, NULL, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            tconst = excluded.tconst,
            watched_on = excluded.watched_on,
            notes = excluded.notes,
            created_at = excluded.created_at
        """,
        [event_id, imdb_id, watched_on, f"plex_rating_key:{snapshot['rating_key']}", watched_at],
    )
    return True


def _sync_plex_content_state(conn, snapshot: dict[str, Any], now: str) -> bool:
    db = _db()
    ids = snapshot.get("ids") or {}
    imdb_id = ids.get("imdb")
    if not imdb_id:
        return False
    found = conn.execute("SELECT COUNT(*) FROM app.catalog_titles WHERE tconst = ?", [imdb_id]).fetchone()[0]
    if not found:
        return False

    interest_state = None
    last_watched_at = None
    if db._plex_item_is_watched(snapshot):
        interest_state = "watched"
        last_watched_at = db._parse_unix_timestamp(snapshot.get("last_viewed_at")) or now
    elif db._plex_item_is_in_progress(snapshot):
        interest_state = "in_progress"
    else:
        return False

    conn.execute(
        """
        INSERT INTO app.content_state (tconst, interest_state, last_previewed_at, last_watched_at, updated_at)
        VALUES (?, ?, NULL, ?, ?)
        ON CONFLICT (tconst) DO UPDATE SET
            interest_state = excluded.interest_state,
            last_watched_at = COALESCE(excluded.last_watched_at, app.content_state.last_watched_at),
            updated_at = excluded.updated_at
        """,
        [imdb_id, interest_state, last_watched_at, now],
    )
    return True


def _sync_trakt_history(conn, sync_run_id: str, files: list[dict[str, Any]]) -> dict[str, int]:
    db = _db()
    imported = 0
    watch_events_synced = 0
    for file_info in files:
        for item in db._load_json_file(db.Path(file_info["path"])):
            history_id = db._safe_int(item.get("id"))
            watched_at = item.get("watched_at")
            if history_id is None or not watched_at:
                continue
            media = db._extract_trakt_media(item)
            conn.execute(
                """
                INSERT INTO old.trakt_history_events (
                    history_id, tconst, media_type, trakt_id, imdb_id, tmdb_id, parent_trakt_id, parent_title,
                    title, season_number, episode_number, watched_at, watched_on, action, is_active,
                    last_seen_sync_id, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
                ON CONFLICT (history_id) DO UPDATE SET
                    tconst = excluded.tconst,
                    media_type = excluded.media_type,
                    trakt_id = excluded.trakt_id,
                    imdb_id = excluded.imdb_id,
                    tmdb_id = excluded.tmdb_id,
                    parent_trakt_id = excluded.parent_trakt_id,
                    parent_title = excluded.parent_title,
                    title = excluded.title,
                    season_number = excluded.season_number,
                    episode_number = excluded.episode_number,
                    watched_at = excluded.watched_at,
                    watched_on = excluded.watched_on,
                    action = excluded.action,
                    is_active = TRUE,
                    last_seen_sync_id = excluded.last_seen_sync_id,
                    raw_json = excluded.raw_json
                """,
                [
                    history_id,
                    media["tconst"],
                    media["media_type"],
                    media["trakt_id"],
                    media["imdb_id"],
                    media["tmdb_id"],
                    media["parent_trakt_id"],
                    media["parent_title"],
                    media["title"],
                    media["season_number"],
                    media["episode_number"],
                    watched_at,
                    watched_at[:10],
                    item.get("action"),
                    sync_run_id,
                    json.dumps(item, ensure_ascii=False),
                ],
            )
            conn.execute("DELETE FROM app.watch_events WHERE id = ?", [f"trakt-history-{history_id}"])
            conn.execute(
                """
                INSERT INTO app.watch_events (
                    id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
                )
                VALUES (?, ?, ?, ?, 'trakt_export', ?, NULL, NULL, NULL, ?)
                """,
                [
                    f"trakt-history-{history_id}",
                    media["tconst"] or media["imdb_id"] or f"unresolved:{history_id}",
                    "episode" if media["media_type"] == "episode" else "title",
                    watched_at[:10],
                    sync_run_id,
                    watched_at,
                ],
            )
            conn.execute(
                "INSERT INTO old.trakt_history_snapshot (sync_run_id, history_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                [sync_run_id, history_id],
            )
            imported += 1
            watch_events_synced += 1

    conn.execute("UPDATE old.trakt_history_events SET is_active = FALSE WHERE last_seen_sync_id <> ?", [sync_run_id])
    return {"imported": imported, "watch_events_synced": watch_events_synced}


def _sync_trakt_ratings(conn, sync_run_id: str, files: list[dict[str, Any]]) -> dict[str, int]:
    db = _db()
    imported = 0
    for file_info in files:
        for item in db._load_json_file(db.Path(file_info["path"])):
            media = db._extract_trakt_media(item)
            source_key = db._build_trakt_media_key(media)
            rated_at = item.get("rated_at")
            rating = db._safe_int(item.get("rating"))
            if not source_key or rated_at is None or rating is None:
                continue
            conn.execute(
                """
                INSERT INTO old.trakt_ratings (
                    source_key, media_type, trakt_id, imdb_id, tmdb_id, tconst, parent_trakt_id, parent_title,
                    title, season_number, episode_number, rating, rated_at, is_active, last_seen_sync_id, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
                ON CONFLICT (source_key) DO UPDATE SET
                    media_type = excluded.media_type,
                    trakt_id = excluded.trakt_id,
                    imdb_id = excluded.imdb_id,
                    tmdb_id = excluded.tmdb_id,
                    tconst = excluded.tconst,
                    parent_trakt_id = excluded.parent_trakt_id,
                    parent_title = excluded.parent_title,
                    title = excluded.title,
                    season_number = excluded.season_number,
                    episode_number = excluded.episode_number,
                    rating = excluded.rating,
                    rated_at = excluded.rated_at,
                    is_active = TRUE,
                    last_seen_sync_id = excluded.last_seen_sync_id,
                    raw_json = excluded.raw_json
                """,
                [
                    source_key,
                    media["media_type"],
                    media["trakt_id"],
                    media["imdb_id"],
                    media["tmdb_id"],
                    media["tconst"],
                    media["parent_trakt_id"],
                    media["parent_title"],
                    media["title"],
                    media["season_number"],
                    media["episode_number"],
                    rating,
                    rated_at,
                    sync_run_id,
                    json.dumps(item, ensure_ascii=False),
                ],
            )
            conn.execute(
                "INSERT INTO old.trakt_ratings_snapshot (sync_run_id, source_key) VALUES (?, ?) ON CONFLICT DO NOTHING",
                [sync_run_id, source_key],
            )
            imported += 1
    conn.execute("UPDATE old.trakt_ratings SET is_active = FALSE WHERE last_seen_sync_id <> ?", [sync_run_id])
    return {"imported": imported}


def _sync_trakt_lists(
    conn,
    sync_run_id: str,
    metadata_files: list[dict[str, Any]],
    custom_list_files: list[dict[str, Any]],
    watchlist_files: list[dict[str, Any]],
) -> dict[str, int]:
    db = _db()
    imported_lists = 0
    imported_items = 0
    list_name_map: dict[str, str] = {}

    for file_info in metadata_files:
        for item in db._load_json_file(db.Path(file_info["path"])):
            trakt_list_id = db._safe_int(((item.get("ids") or {}).get("trakt")))
            if trakt_list_id is None:
                continue
            conn.execute(
                """
                INSERT INTO old.trakt_lists (
                    trakt_list_id, slug, name, description, privacy, list_type, item_count, updated_at,
                    is_active, last_seen_sync_id, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
                ON CONFLICT (trakt_list_id) DO UPDATE SET
                    slug = excluded.slug,
                    name = excluded.name,
                    description = excluded.description,
                    privacy = excluded.privacy,
                    list_type = excluded.list_type,
                    item_count = excluded.item_count,
                    updated_at = excluded.updated_at,
                    is_active = TRUE,
                    last_seen_sync_id = excluded.last_seen_sync_id,
                    raw_json = excluded.raw_json
                """,
                [
                    trakt_list_id,
                    (item.get("ids") or {}).get("slug"),
                    item.get("name"),
                    item.get("description"),
                    item.get("privacy"),
                    item.get("type"),
                    db._safe_int(item.get("item_count")),
                    item.get("updated_at"),
                    sync_run_id,
                    json.dumps(item, ensure_ascii=False),
                ],
            )
            list_name_map[str(trakt_list_id)] = item.get("name") or ""
            imported_lists += 1
    conn.execute("UPDATE old.trakt_lists SET is_active = FALSE WHERE last_seen_sync_id <> ?", [sync_run_id])

    for file_info in custom_list_files:
        list_id = db._parse_trakt_list_id_from_filename(file_info["name"])
        list_name = list_name_map.get(list_id)
        for item in db._load_json_file(db.Path(file_info["path"])):
            media = db._extract_trakt_media(item)
            item_id = db._safe_int(item.get("id"))
            if item_id is None:
                continue
            conn.execute(
                """
                INSERT INTO old.trakt_list_items (
                    source_key, trakt_list_id, list_kind, list_name, item_id, media_type, trakt_id, imdb_id, tmdb_id,
                    tconst, parent_trakt_id, parent_title, title, season_number, episode_number, rank, listed_at,
                    notes, my_rating, is_active, last_seen_sync_id, raw_json
                )
                VALUES (?, ?, 'custom', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
                ON CONFLICT (source_key) DO UPDATE SET
                    list_name = excluded.list_name,
                    item_id = excluded.item_id,
                    media_type = excluded.media_type,
                    trakt_id = excluded.trakt_id,
                    imdb_id = excluded.imdb_id,
                    tmdb_id = excluded.tmdb_id,
                    tconst = excluded.tconst,
                    parent_trakt_id = excluded.parent_trakt_id,
                    parent_title = excluded.parent_title,
                    title = excluded.title,
                    season_number = excluded.season_number,
                    episode_number = excluded.episode_number,
                    rank = excluded.rank,
                    listed_at = excluded.listed_at,
                    notes = excluded.notes,
                    my_rating = excluded.my_rating,
                    is_active = TRUE,
                    last_seen_sync_id = excluded.last_seen_sync_id,
                    raw_json = excluded.raw_json
                """,
                [
                    f"{list_id}:{item_id}",
                    list_id,
                    list_name,
                    item_id,
                    media["media_type"],
                    media["trakt_id"],
                    media["imdb_id"],
                    media["tmdb_id"],
                    media["tconst"],
                    media["parent_trakt_id"],
                    media["parent_title"],
                    media["title"],
                    media["season_number"],
                    media["episode_number"],
                    db._safe_int(item.get("rank")),
                    item.get("listed_at"),
                    item.get("notes"),
                    db._safe_int(item.get("my_rating")),
                    sync_run_id,
                    json.dumps(item, ensure_ascii=False),
                ],
            )
            conn.execute(
                "INSERT INTO old.trakt_list_items_snapshot (sync_run_id, source_key) VALUES (?, ?) ON CONFLICT DO NOTHING",
                [sync_run_id, f"{list_id}:{item_id}"],
            )
            imported_items += 1

    for file_info in watchlist_files:
        for item in db._load_json_file(db.Path(file_info["path"])):
            media = db._extract_trakt_media(item)
            item_id = db._safe_int(item.get("id"))
            if item_id is None:
                continue
            conn.execute(
                """
                INSERT INTO old.trakt_list_items (
                    source_key, trakt_list_id, list_kind, list_name, item_id, media_type, trakt_id, imdb_id, tmdb_id,
                    tconst, parent_trakt_id, parent_title, title, season_number, episode_number, rank, listed_at,
                    notes, my_rating, is_active, last_seen_sync_id, raw_json
                )
                VALUES (?, 'watchlist', 'watchlist', 'Watchlist', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
                ON CONFLICT (source_key) DO UPDATE SET
                    item_id = excluded.item_id,
                    media_type = excluded.media_type,
                    trakt_id = excluded.trakt_id,
                    imdb_id = excluded.imdb_id,
                    tmdb_id = excluded.tmdb_id,
                    tconst = excluded.tconst,
                    parent_trakt_id = excluded.parent_trakt_id,
                    parent_title = excluded.parent_title,
                    title = excluded.title,
                    season_number = excluded.season_number,
                    episode_number = excluded.episode_number,
                    rank = excluded.rank,
                    listed_at = excluded.listed_at,
                    notes = excluded.notes,
                    my_rating = excluded.my_rating,
                    is_active = TRUE,
                    last_seen_sync_id = excluded.last_seen_sync_id,
                    raw_json = excluded.raw_json
                """,
                [
                    f"watchlist:{item_id}",
                    item_id,
                    media["media_type"],
                    media["trakt_id"],
                    media["imdb_id"],
                    media["tmdb_id"],
                    media["tconst"],
                    media["parent_trakt_id"],
                    media["parent_title"],
                    media["title"],
                    media["season_number"],
                    media["episode_number"],
                    db._safe_int(item.get("rank")),
                    item.get("listed_at"),
                    item.get("notes"),
                    db._safe_int(item.get("my_rating")),
                    sync_run_id,
                    json.dumps(item, ensure_ascii=False),
                ],
            )
            conn.execute(
                "INSERT INTO old.trakt_list_items_snapshot (sync_run_id, source_key) VALUES (?, ?) ON CONFLICT DO NOTHING",
                [sync_run_id, f"watchlist:{item_id}"],
            )
            imported_items += 1

    conn.execute(
        """
        UPDATE old.trakt_list_items
        SET is_active = FALSE
        WHERE list_kind = 'custom' AND last_seen_sync_id <> ?
        """,
        [sync_run_id],
    )
    conn.execute(
        """
        UPDATE old.trakt_list_items
        SET is_active = FALSE
        WHERE list_kind = 'watchlist' AND last_seen_sync_id <> ?
        """,
        [sync_run_id],
    )
    return {"lists_imported": imported_lists, "items_imported": imported_items}


def _sync_trakt_collection(conn, sync_run_id: str, files: list[dict[str, Any]]) -> dict[str, int]:
    db = _db()
    imported = 0
    for file_info in files:
        for item in db._load_json_file(db.Path(file_info["path"])):
            media = db._extract_trakt_media(item)
            source_key = db._build_trakt_media_key(media)
            if not source_key:
                continue
            conn.execute(
                """
                INSERT INTO old.trakt_collection_items (
                    source_key, media_type, trakt_id, imdb_id, tmdb_id, tconst, parent_trakt_id, parent_title,
                    title, season_number, episode_number, collected_at, updated_at, is_active, last_seen_sync_id, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
                ON CONFLICT (source_key) DO UPDATE SET
                    media_type = excluded.media_type,
                    trakt_id = excluded.trakt_id,
                    imdb_id = excluded.imdb_id,
                    tmdb_id = excluded.tmdb_id,
                    tconst = excluded.tconst,
                    parent_trakt_id = excluded.parent_trakt_id,
                    parent_title = excluded.parent_title,
                    title = excluded.title,
                    season_number = excluded.season_number,
                    episode_number = excluded.episode_number,
                    collected_at = excluded.collected_at,
                    updated_at = excluded.updated_at,
                    is_active = TRUE,
                    last_seen_sync_id = excluded.last_seen_sync_id,
                    raw_json = excluded.raw_json
                """,
                [
                    source_key,
                    media["media_type"],
                    media["trakt_id"],
                    media["imdb_id"],
                    media["tmdb_id"],
                    media["tconst"],
                    media["parent_trakt_id"],
                    media["parent_title"],
                    media["title"],
                    media["season_number"],
                    media["episode_number"],
                    item.get("collected_at"),
                    item.get("updated_at"),
                    sync_run_id,
                    json.dumps(item, ensure_ascii=False),
                ],
            )
            conn.execute(
                "INSERT INTO old.trakt_collection_snapshot (sync_run_id, source_key) VALUES (?, ?) ON CONFLICT DO NOTHING",
                [sync_run_id, source_key],
            )
            imported += 1
    conn.execute("UPDATE old.trakt_collection_items SET is_active = FALSE WHERE last_seen_sync_id <> ?", [sync_run_id])
    return {"imported": imported}


def _read_last_activities(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    db = _db()
    if not files:
        return None
    return db._load_json_file(db.Path(files[0]["path"]))


def _sync_imdb_watchlist(conn, sync_run_id: str, file_info: dict[str, Any] | None) -> dict[str, int]:
    db = _db()
    if file_info is None:
        return {"imported": 0}
    imported = 0
    for row in db._read_csv_rows(db.Path(file_info["path"])):
        tconst = (row.get("Const") or "").strip()
        if not tconst:
            continue
        conn.execute(
            """
            INSERT INTO old.imdb_watchlist_items (
                tconst, position, created_at_src, modified_at_src, description, title, original_title, url,
                title_type, imdb_rating, runtime_minutes, year, genres, num_votes, release_date, directors,
                your_rating, date_rated, is_active, last_seen_sync_id, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
            ON CONFLICT (tconst) DO UPDATE SET
                position = excluded.position,
                created_at_src = excluded.created_at_src,
                modified_at_src = excluded.modified_at_src,
                description = excluded.description,
                title = excluded.title,
                original_title = excluded.original_title,
                url = excluded.url,
                title_type = excluded.title_type,
                imdb_rating = excluded.imdb_rating,
                runtime_minutes = excluded.runtime_minutes,
                year = excluded.year,
                genres = excluded.genres,
                num_votes = excluded.num_votes,
                release_date = excluded.release_date,
                directors = excluded.directors,
                your_rating = excluded.your_rating,
                date_rated = excluded.date_rated,
                is_active = TRUE,
                last_seen_sync_id = excluded.last_seen_sync_id,
                raw_json = excluded.raw_json
            """,
            [
                tconst,
                db._safe_int(row.get("Position")),
                row.get("Created") or None,
                row.get("Modified") or None,
                row.get("Description") or None,
                row.get("Title") or None,
                row.get("Original Title") or None,
                row.get("URL") or None,
                row.get("Title Type") or None,
                db._safe_float(row.get("IMDb Rating")),
                db._safe_int(row.get("Runtime (mins)")),
                db._safe_int(row.get("Year")),
                row.get("Genres") or None,
                db._safe_int(row.get("Num Votes")),
                db._parse_iso_date(row.get("Release Date")),
                row.get("Directors") or None,
                db._safe_int(row.get("Your Rating")),
                db._parse_iso_date(row.get("Date Rated")),
                sync_run_id,
                json.dumps(row, ensure_ascii=False),
            ],
        )
        imported += 1
    conn.execute("UPDATE old.imdb_watchlist_items SET is_active = FALSE WHERE last_seen_sync_id <> ?", [sync_run_id])
    return {"imported": imported}


def _sync_imdb_favorite_people(conn, sync_run_id: str, file_info: dict[str, Any] | None) -> dict[str, int]:
    db = _db()
    if file_info is None:
        return {"imported": 0}
    imported = 0
    for row in db._read_csv_rows(db.Path(file_info["path"])):
        nconst = (row.get("Const") or "").strip()
        if not nconst:
            continue
        conn.execute(
            """
            INSERT INTO old.imdb_favorite_people (
                nconst, position, created_at_src, modified_at_src, description, name, known_for, birth_date,
                is_active, last_seen_sync_id, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)
            ON CONFLICT (nconst) DO UPDATE SET
                position = excluded.position,
                created_at_src = excluded.created_at_src,
                modified_at_src = excluded.modified_at_src,
                description = excluded.description,
                name = excluded.name,
                known_for = excluded.known_for,
                birth_date = excluded.birth_date,
                is_active = TRUE,
                last_seen_sync_id = excluded.last_seen_sync_id,
                raw_json = excluded.raw_json
            """,
            [
                nconst,
                db._safe_int(row.get("Position")),
                row.get("Created") or None,
                row.get("Modified") or None,
                row.get("Description") or None,
                row.get("Name") or None,
                row.get("Known For") or None,
                row.get("Birth Date") or None,
                sync_run_id,
                json.dumps(row, ensure_ascii=False),
            ],
        )
        imported += 1
    conn.execute("UPDATE old.imdb_favorite_people SET is_active = FALSE WHERE last_seen_sync_id <> ?", [sync_run_id])
    return {"imported": imported}
