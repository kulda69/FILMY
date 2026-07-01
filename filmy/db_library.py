from __future__ import annotations

"""Local-library DB operations extracted from the `filmy.db` facade.

The goal is to separate mutations and list-oriented read models from the very
large legacy module while preserving the existing public API. Runtime imports
back to `filmy.db` are deliberate here: they let us reuse stable internal
helpers first and only later decide which helpers deserve their own dedicated
module.
"""

import importlib
from typing import Any


def _db():
    return importlib.import_module("filmy.db")


def update_content_state(tconst: str, interest_state: str) -> dict[str, Any]:
    db = _db()
    now = db._now_iso()

    def write(conn):
        conn.execute(
            """
            INSERT INTO app.content_state (tconst, interest_state, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (tconst) DO UPDATE SET
                interest_state = excluded.interest_state,
                updated_at = excluded.updated_at,
                last_previewed_at = CASE
                    WHEN excluded.interest_state = 'previewed' THEN excluded.updated_at
                    ELSE app.content_state.last_previewed_at
                END,
                last_watched_at = CASE
                    WHEN excluded.interest_state = 'watched' THEN excluded.updated_at
                    ELSE app.content_state.last_watched_at
                END
            """,
            [tconst, interest_state, now],
        )

    db._run_duckdb_write(write)
    db.clear_title_presentation_cache()
    return {"tconst": tconst, "interest_state": interest_state, "updated_at": now}


def set_watchlist_state(tconst: str, *, in_watchlist: bool, notes: str | None = None) -> dict[str, Any]:
    db = _db()
    detail = db.get_content_detail(tconst)
    if detail is None:
        raise ValueError("Titul nebyl nalezen.")

    now = db._now_iso()
    media = db._build_local_media_identity(detail)
    canonical_key = db._canonical_media_key(
        media["media_type"],
        media["tconst"],
        media["imdb_id"],
        media["tmdb_id"],
        None,
        media["season_number"],
        media["episode_number"],
    )

    def write(conn):
        db._ensure_user_list(conn, "watchlist", "Watchlist", "watchlist", "local_app", "system:watchlist", now)
        if in_watchlist:
            db._upsert_user_list_item(
                conn,
                list_id="watchlist",
                canonical_key=canonical_key,
                tconst=media["tconst"],
                media_type=media["media_type"],
                imdb_id=media["imdb_id"],
                tmdb_id=media["tmdb_id"],
                trakt_id=None,
                parent_tconst=media["parent_tconst"],
                parent_title=media["parent_title"],
                title=media["title"],
                season_number=media["season_number"],
                episode_number=media["episode_number"],
                rank=None,
                added_at=now,
                notes=notes,
                source_origin="local_app",
                source_ref=f"manual_watchlist:{tconst}",
                now=now,
            )
        else:
            conn.execute(
                """
                UPDATE app.user_list_items
                SET is_archived = TRUE, updated_at = ?
                WHERE list_id = 'watchlist' AND canonical_key = ?
                """,
                [now, canonical_key],
            )

    db._run_duckdb_write(write)
    db.clear_title_presentation_cache()
    return {
        "tconst": tconst,
        "in_watchlist": in_watchlist,
        "updated_at": now,
        "library": db._get_library_summary_for_tconst(tconst),
    }


def add_title_to_user_list(tconst: str, list_id: str, *, notes: str | None = None) -> dict[str, Any]:
    db = _db()
    detail = db.get_content_detail(tconst)
    if detail is None:
        raise ValueError("Titul nebyl nalezen.")

    now = db._now_iso()
    media = db._build_local_media_identity(detail)
    canonical_key = db._canonical_media_key(
        media["media_type"],
        media["tconst"],
        media["imdb_id"],
        media["tmdb_id"],
        None,
        media["season_number"],
        media["episode_number"],
    )

    def write(conn):
        target_list = conn.execute(
            "SELECT id, list_kind FROM app.user_lists WHERE id = ?",
            [list_id],
        ).fetchone()
        if target_list is None:
            raise ValueError("Cílový seznam nebyl nalezen.")

        db._upsert_user_list_item(
            conn,
            list_id=list_id,
            canonical_key=canonical_key,
            tconst=media["tconst"],
            media_type=media["media_type"],
            imdb_id=media["imdb_id"],
            tmdb_id=media["tmdb_id"],
            trakt_id=None,
            parent_tconst=media["parent_tconst"],
            parent_title=media["parent_title"],
            title=media["title"],
            season_number=media["season_number"],
            episode_number=media["episode_number"],
            rank=None,
            added_at=now,
            notes=notes,
            source_origin="local_app",
            source_ref=f"manual_add_to_list:{tconst}",
            now=now,
        )

    db._run_duckdb_write(write)
    db.clear_title_presentation_cache()
    return {
        "tconst": tconst,
        "list_id": list_id,
        "updated_at": now,
        "library": db._get_library_summary_for_tconst(tconst),
    }


def set_user_rating(tconst: str, rating: int) -> dict[str, Any]:
    db = _db()
    if rating < 1 or rating > 10:
        raise ValueError("Rating musí být mezi 1 a 10.")
    detail = db.get_content_detail(tconst)
    if detail is None:
        raise ValueError("Titul nebyl nalezen.")

    now = db._now_iso()
    media = db._build_local_media_identity(detail)
    canonical_key = db._canonical_media_key(
        media["media_type"],
        media["tconst"],
        media["imdb_id"],
        media["tmdb_id"],
        None,
        media["season_number"],
        media["episode_number"],
    )

    def write(conn):
        db._upsert_user_rating(
            conn,
            canonical_key=canonical_key,
            tconst=media["tconst"],
            media_type=media["media_type"],
            imdb_id=media["imdb_id"],
            tmdb_id=media["tmdb_id"],
            trakt_id=None,
            parent_tconst=media["parent_tconst"],
            parent_title=media["parent_title"],
            title=media["title"],
            season_number=media["season_number"],
            episode_number=media["episode_number"],
            rating=rating,
            rated_at=now,
            source_origin="local_app",
            source_ref=f"manual_rating:{tconst}",
            now=now,
        )

    db._run_duckdb_write(write)
    db.clear_title_presentation_cache()
    return {"tconst": tconst, "rating": rating, "rated_at": now, "library": db._get_library_summary_for_tconst(tconst)}


def set_person_affinity_rating(nconst: str, rating: int) -> dict[str, Any]:
    db = _db()
    if rating < 0 or rating > 10:
        raise ValueError("Rating musí být mezi 0 a 10.")

    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        row = conn.execute(
            """
            SELECT nconst, primary_name, known_for_titles, birth_year
            FROM app.catalog_people
            WHERE nconst = ?
            """,
            [nconst],
        ).fetchone()
    if row is None:
        raise ValueError("Osoba nebyla nalezena.")

    now = db._now_iso()
    person_key = f"nconst:{nconst}"

    def write(conn):
        existing = conn.execute(
            "SELECT is_favorite, created_at FROM app.user_people WHERE person_key = ?",
            [person_key],
        ).fetchone()
        conn.execute(
            """
            INSERT INTO app.user_people (
                person_key, nconst, name, known_for, birth_date, source_origin, source_ref,
                is_favorite, affinity_rating, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'local_app', ?, ?, ?, ?, ?)
            ON CONFLICT (person_key) DO UPDATE SET
                nconst = excluded.nconst,
                name = excluded.name,
                known_for = COALESCE(app.user_people.known_for, excluded.known_for),
                birth_date = COALESCE(app.user_people.birth_date, excluded.birth_date),
                affinity_rating = excluded.affinity_rating,
                updated_at = excluded.updated_at
            """,
            [
                person_key,
                row[0],
                row[1],
                row[2],
                str(row[3]) if row[3] is not None else None,
                f"manual_person_rating:{nconst}",
                existing[0] if existing is not None else False,
                rating,
                existing[1] if existing is not None else now,
                now,
            ],
        )

    db._run_duckdb_write(write)
    db.get_person_presentation(nconst)
    return {"nconst": nconst, "rating": rating, "updated_at": now}


def clear_user_rating(tconst: str) -> dict[str, Any]:
    db = _db()
    detail = db.get_content_detail(tconst)
    if detail is None:
        raise ValueError("Titul nebyl nalezen.")
    media = db._build_local_media_identity(detail)
    canonical_key = db._canonical_media_key(
        media["media_type"],
        media["tconst"],
        media["imdb_id"],
        media["tmdb_id"],
        None,
        media["season_number"],
        media["episode_number"],
    )
    now = db._now_iso()

    def write(conn):
        conn.execute("DELETE FROM app.user_ratings WHERE canonical_key = ?", [canonical_key])

    db._run_duckdb_write(write)
    db.clear_title_presentation_cache()
    return {"tconst": tconst, "rating": None, "updated_at": now, "library": db._get_library_summary_for_tconst(tconst)}


def record_watch_event(
    tconst: str,
    *,
    watched_on: str | None = None,
    notes: str | None = None,
    add_to_watched_list: bool = False,
) -> dict[str, Any]:
    db = _db()
    detail = db.get_content_detail(tconst)
    if detail is None:
        raise ValueError("Titul nebyl nalezen.")
    now = db._now_iso()
    event_id = str(db.uuid.uuid4())
    event_scope = "episode" if detail["kind"] == "episode" else "title"
    effective_watched_on = watched_on or now[:10]
    try:
        db.datetime.strptime(effective_watched_on, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("watched_on musí být ISO datum ve formátu YYYY-MM-DD.") from exc

    def write(conn):
        conn.execute(
            """
            INSERT INTO app.watch_events (
                id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
            )
            VALUES (?, ?, ?, ?, 'local_app', NULL, NULL, NULL, ?, ?)
            """,
            [event_id, tconst, event_scope, effective_watched_on, notes, now],
        )
        conn.execute(
            """
            INSERT INTO app.content_state (tconst, interest_state, last_watched_at, updated_at)
            VALUES (?, 'watched', ?, ?)
            ON CONFLICT (tconst) DO UPDATE SET
                interest_state = 'watched',
                last_watched_at = excluded.last_watched_at,
                updated_at = excluded.updated_at
            """,
            [tconst, now, now],
        )
        if detail["kind"] != "episode":
            conn.execute(
                """
                UPDATE app.user_list_items AS i
                SET is_archived = TRUE, updated_at = ?
                FROM app.user_lists AS l
                WHERE l.id = i.list_id
                  AND l.list_kind = 'watchlist'
                  AND i.is_archived = FALSE
                  AND i.tconst = ?
                """,
                [now, tconst],
            )

    db._run_duckdb_write(write)
    db.clear_title_presentation_cache()
    return {
        "id": event_id,
        "tconst": tconst,
        "event_scope": event_scope,
        "watched_on": effective_watched_on,
        "created_at": now,
        "library": db._get_library_summary_for_tconst(tconst),
    }


def record_watch_events_through_episode(
    episode_tconst: str,
    *,
    watched_on: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    db = _db()
    detail = db.get_content_detail(episode_tconst)
    if detail is None or detail.get("kind") != "episode":
        raise ValueError("Epizoda nebyla nalezena.")
    series_tconst = detail.get("series_tconst")
    season_number = detail.get("season_number")
    episode_number = detail.get("episode_number")
    if not series_tconst or season_number is None or episode_number is None:
        raise ValueError("Epizoda nema uplny serialovy kontext.")
    now = db._now_iso()
    effective_watched_on = watched_on or now[:10]
    try:
        db.datetime.strptime(effective_watched_on, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("watched_on musi byt ISO datum ve formatu YYYY-MM-DD.") from exc

    def write(conn):
        episode_rows = conn.execute(
            """
            SELECT e.episode_tconst
            FROM app.catalog_episodes AS e
            WHERE e.series_tconst = ?
              AND (
                    e.season_number < ?
                    OR (e.season_number = ? AND e.episode_number <= ?)
              )
              AND NOT EXISTS (
                    SELECT 1
                    FROM app.watch_events AS w
                    WHERE w.tconst = e.episode_tconst
              )
            ORDER BY e.season_number NULLS LAST, e.episode_number NULLS LAST, e.episode_tconst
            """,
            [series_tconst, season_number, season_number, episode_number],
        ).fetchall()
        watched_ids = [row[0] for row in episode_rows]
        if not watched_ids:
            return []
        conn.executemany(
            """
            INSERT INTO app.watch_events (
                id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
            )
            VALUES (?, ?, 'episode', ?, 'local_app', NULL, NULL, NULL, ?, ?)
            """,
            [[str(db.uuid.uuid4()), tconst, effective_watched_on, notes, now] for tconst in watched_ids],
        )
        conn.executemany(
            """
            INSERT INTO app.content_state (tconst, interest_state, last_watched_at, updated_at)
            VALUES (?, 'watched', ?, ?)
            ON CONFLICT (tconst) DO UPDATE SET
                interest_state = 'watched',
                last_watched_at = excluded.last_watched_at,
                updated_at = excluded.updated_at
            """,
            [[tconst, now, now] for tconst in watched_ids],
        )
        return watched_ids

    watched_ids = db._run_duckdb_write(write)
    db.clear_title_presentation_cache()
    return {
        "series_tconst": series_tconst,
        "target_episode_tconst": episode_tconst,
        "watched_on": effective_watched_on,
        "watched_count": len(watched_ids),
        "watched_tconsts": watched_ids,
        "library": db._get_library_summary_for_tconst(series_tconst),
    }


def delete_group_from_user_list(list_id: str, display_tconst: str) -> dict[str, Any]:
    db = _db()
    now = db._now_iso()
    result: dict[str, Any] = {"affected_rows": 0}

    def write(conn):
        list_row = conn.execute(
            """
            SELECT id, name, list_kind
            FROM app.user_lists
            WHERE id = ?
            """,
            [list_id],
        ).fetchone()
        if list_row is None:
            raise ValueError("Seznam nebyl nalezen.")

        affected = conn.execute(
            """
            UPDATE app.user_list_items
            SET is_archived = TRUE, updated_at = ?
            WHERE id IN (
                SELECT i.id
                FROM app.user_list_items AS i
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
                WHERE i.list_id = ?
                  AND i.is_archived = FALSE
                  AND COALESCE(e.series_tconst, i.tconst, i.parent_tconst) = ?
            )
            RETURNING id
            """,
            [now, list_id, display_tconst],
        ).fetchall()
        result["affected_rows"] = len(affected)

    db._run_duckdb_write(write)
    db.clear_title_presentation_cache()
    return {"list_id": list_id, "display_tconst": display_tconst, "updated_at": now, "affected_rows": int(result["affected_rows"])}


def move_group_between_user_lists(source_list_id: str, target_list_id: str, display_tconst: str) -> dict[str, Any]:
    db = _db()
    if source_list_id == target_list_id:
        raise ValueError("Zdrojový a cílový seznam jsou stejné.")
    now = db._now_iso()
    result: dict[str, Any] = {"moved_rows": 0}

    def write(conn):
        source_list = conn.execute("SELECT id, name, list_kind FROM app.user_lists WHERE id = ?", [source_list_id]).fetchone()
        if source_list is None:
            raise ValueError("Zdrojový seznam nebyl nalezen.")
        target_list = conn.execute("SELECT id, name, list_kind FROM app.user_lists WHERE id = ?", [target_list_id]).fetchone()
        if target_list is None:
            raise ValueError("Cílový seznam nebyl nalezen.")
        rows = conn.execute(
            """
            SELECT
                i.canonical_key, i.tconst, i.media_type, i.imdb_id, i.tmdb_id, i.trakt_id, i.parent_tconst, i.parent_title,
                i.title, i.season_number, i.episode_number, i.rank, i.added_at, i.notes, i.source_origin, i.source_ref
            FROM app.user_list_items AS i
            LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
            WHERE i.list_id = ?
              AND i.is_archived = FALSE
              AND COALESCE(e.series_tconst, i.tconst, i.parent_tconst) = ?
            ORDER BY i.rank NULLS LAST, i.added_at DESC NULLS LAST, i.created_at DESC
            """,
            [source_list_id, display_tconst],
        ).fetchall()
        if not rows:
            raise ValueError("V seznamu nebyla nalezena žádná položka k přesunu.")
        for row in rows:
            db._upsert_user_list_item(
                conn,
                list_id=target_list_id,
                canonical_key=row[0],
                tconst=row[1],
                media_type=row[2],
                imdb_id=row[3],
                tmdb_id=row[4],
                trakt_id=row[5],
                parent_tconst=row[6],
                parent_title=row[7],
                title=row[8],
                season_number=row[9],
                episode_number=row[10],
                rank=row[11],
                added_at=row[12],
                notes=row[13],
                source_origin=row[14],
                source_ref=row[15],
                now=now,
            )
        conn.execute(
            """
            UPDATE app.user_list_items
            SET is_archived = TRUE, updated_at = ?
            WHERE id IN (
                SELECT i.id
                FROM app.user_list_items AS i
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
                WHERE i.list_id = ?
                AND i.is_archived = FALSE
                  AND COALESCE(e.series_tconst, i.tconst, i.parent_tconst) = ?
            )
            """,
            [now, source_list_id, display_tconst],
        )
        result["moved_rows"] = len(rows)

    db._run_duckdb_write(write)
    db.clear_title_presentation_cache()
    return {"source_list_id": source_list_id, "target_list_id": target_list_id, "display_tconst": display_tconst, "moved_rows": int(result["moved_rows"]), "updated_at": now}


def copy_group_to_user_list(source_list_id: str, target_list_id: str, display_tconst: str) -> dict[str, Any]:
    db = _db()
    if source_list_id == target_list_id:
        raise ValueError("Zdrojový a cílový seznam jsou stejné.")
    now = db._now_iso()
    result: dict[str, Any] = {"copied_rows": 0}

    def write(conn):
        source_list = conn.execute("SELECT id, name, list_kind FROM app.user_lists WHERE id = ?", [source_list_id]).fetchone()
        if source_list is None:
            raise ValueError("Zdrojový seznam nebyl nalezen.")
        target_list = conn.execute("SELECT id, name, list_kind FROM app.user_lists WHERE id = ?", [target_list_id]).fetchone()
        if target_list is None:
            raise ValueError("Cílový seznam nebyl nalezen.")
        rows = conn.execute(
            """
            SELECT
                i.canonical_key, i.tconst, i.media_type, i.imdb_id, i.tmdb_id, i.trakt_id, i.parent_tconst, i.parent_title,
                i.title, i.season_number, i.episode_number, i.rank, i.added_at, i.notes, i.source_origin, i.source_ref
            FROM app.user_list_items AS i
            LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
            WHERE i.list_id = ?
              AND i.is_archived = FALSE
              AND COALESCE(e.series_tconst, i.tconst, i.parent_tconst) = ?
            ORDER BY i.rank NULLS LAST, i.added_at DESC NULLS LAST, i.created_at DESC
            """,
            [source_list_id, display_tconst],
        ).fetchall()
        if not rows:
            raise ValueError("V seznamu nebyla nalezena žádná položka ke kopii.")
        for row in rows:
            db._upsert_user_list_item(
                conn,
                list_id=target_list_id,
                canonical_key=row[0],
                tconst=row[1],
                media_type=row[2],
                imdb_id=row[3],
                tmdb_id=row[4],
                trakt_id=row[5],
                parent_tconst=row[6],
                parent_title=row[7],
                title=row[8],
                season_number=row[9],
                episode_number=row[10],
                rank=row[11],
                added_at=row[12],
                notes=row[13],
                source_origin=row[14],
                source_ref=row[15],
                now=now,
            )
        result["copied_rows"] = len(rows)

    db._run_duckdb_write(write)
    db.clear_title_presentation_cache()
    return {"source_list_id": source_list_id, "target_list_id": target_list_id, "display_tconst": display_tconst, "copied_rows": int(result["copied_rows"]), "updated_at": now}


def create_user_list(name: str, description: str | None = None) -> dict[str, Any]:
    db = _db()
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValueError("Název seznamu nesmí být prázdný.")
    cleaned_description = (description or "").strip() or None
    now = db._now_iso()
    result: dict[str, Any] = {}

    def write(conn):
        list_id = f"custom-list-{db.uuid.uuid4()}"
        slug_base = db._slugify(cleaned_name) or "list"
        slug = slug_base
        suffix = 2
        while conn.execute("SELECT 1 FROM app.user_lists WHERE slug = ? LIMIT 1", [slug]).fetchone() is not None:
            slug = f"{slug_base}-{suffix}"
            suffix += 1
        conn.execute(
            """
            INSERT INTO app.user_lists (id, slug, name, description, list_kind, source_origin, source_ref, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'custom', 'local_app', ?, ?, ?)
            """,
            [list_id, slug, cleaned_name, cleaned_description, f"manual_list:{list_id}", now, now],
        )
        result.update({"id": list_id, "slug": slug, "name": cleaned_name, "description": cleaned_description, "list_kind": "custom", "created_at": now})

    db._run_duckdb_write(write)
    return result


def update_user_list_description(list_id: str, description: str | None = None) -> dict[str, Any]:
    db = _db()
    cleaned_description = (description or "").strip() or None
    now = db._now_iso()
    result: dict[str, Any] = {}

    def write(conn):
        row = conn.execute(
            """
            SELECT id, slug, name, list_kind
            FROM app.user_lists
            WHERE id = ?
            """,
            [list_id],
        ).fetchone()
        if row is None:
            raise ValueError("Seznam nebyl nalezen.")
        if row[3] != "custom" and row[0] != "watchlist":
            raise ValueError("Popis lze upravit jen u uživatelských seznamů.")
        conn.execute(
            """
            UPDATE app.user_lists
            SET description = ?, updated_at = ?
            WHERE id = ?
            """,
            [cleaned_description, now, list_id],
        )
        result.update({"id": row[0], "slug": row[1], "name": row[2], "description": cleaned_description, "list_kind": row[3], "updated_at": now})

    db._run_duckdb_write(write)
    return result


def get_recently_watched_page(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    db = _db()
    ui_config = db.get_ui_config()
    page = db._fetch_watch_view_page(limit, offset, cutoff_days=ui_config.recently_watched_days)
    return {
        "list": {"id": db.RECENTLY_WATCHED_VIEW_ID, "slug": "recently-watched", "name": "Recently Watched", "list_kind": "view", "item_type": "view", "view_kind": "recently_watched"},
        "total": page["total"],
        "items": page["items"],
        "limit": page["limit"],
        "offset": page["offset"],
    }


def get_hot_watchlist_page(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    db = _db()
    ui_config = db.get_ui_config()
    hot_limit = ui_config.hot_watchlist_limit
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        total_all = conn.execute(
            """
            WITH latest_posters AS (
                SELECT
                    tconst,
                    local_path,
                    row_number() OVER (PARTITION BY tconst ORDER BY fetched_at DESC, id DESC) AS rn
                FROM app.tmdb_assets
                WHERE asset_kind = 'poster' AND status = 'fetched'
            ),
            watched_titles AS (
                SELECT DISTINCT
                    COALESCE(e.series_tconst, w.tconst) AS display_tconst
                FROM app.watch_events AS w
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
                WHERE w.tconst IS NOT NULL
            ),
            ranked_items AS (
                SELECT
                    COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AS display_tconst,
                    row_number() OVER (
                        PARTITION BY COALESCE(e.series_tconst, i.tconst, i.parent_tconst)
                        ORDER BY i.added_at DESC NULLS LAST, i.updated_at DESC, COALESCE(i.title, i.parent_title, i.tconst)
                    ) AS group_row
                FROM app.user_list_items AS i
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
                WHERE i.list_id = 'watchlist' AND i.is_archived = FALSE
            )
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT r.display_tconst
                FROM ranked_items AS r
                JOIN latest_posters AS p ON p.tconst = r.display_tconst AND p.rn = 1
                LEFT JOIN watched_titles AS wt ON wt.display_tconst = r.display_tconst
                WHERE r.group_row = 1
                  AND r.display_tconst IS NOT NULL
                  AND p.local_path IS NOT NULL
                  AND wt.display_tconst IS NULL
            ) AS grouped
            """
        ).fetchone()[0]
        total = min(total_all, hot_limit)
        if offset >= total:
            rows: list[tuple[Any, ...]] = []
        else:
            rows = conn.execute(
                """
                WITH latest_posters AS (
                    SELECT
                        tconst,
                        local_path,
                        row_number() OVER (PARTITION BY tconst ORDER BY fetched_at DESC, id DESC) AS rn
                    FROM app.tmdb_assets
                    WHERE asset_kind = 'poster' AND status = 'fetched'
                ),
                watched_titles AS (
                    SELECT DISTINCT
                        COALESCE(e.series_tconst, w.tconst) AS display_tconst
                    FROM app.watch_events AS w
                    LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
                    WHERE w.tconst IS NOT NULL
                ),
                latest_user_ratings AS (
                    SELECT
                        tconst,
                        rating,
                        row_number() OVER (PARTITION BY tconst ORDER BY rated_at DESC NULLS LAST, updated_at DESC, created_at DESC) AS rn
                    FROM app.user_ratings
                    WHERE tconst IS NOT NULL
                ),
                ranked_items AS (
                    SELECT
                        COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AS display_tconst,
                        i.tconst AS source_tconst,
                        i.media_type,
                        i.title,
                        i.parent_title,
                        i.season_number,
                        i.episode_number,
                        i.rank,
                        i.added_at,
                        i.notes,
                        row_number() OVER (
                            PARTITION BY COALESCE(e.series_tconst, i.tconst, i.parent_tconst)
                            ORDER BY i.added_at DESC NULLS LAST, i.updated_at DESC, COALESCE(i.title, i.parent_title, i.tconst)
                        ) AS group_row
                    FROM app.user_list_items AS i
                    LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
                    WHERE i.list_id = 'watchlist' AND i.is_archived = FALSE
                ),
                grouped_items AS (
                    SELECT
                        r.display_tconst,
                        r.media_type,
                        r.title,
                        r.parent_title,
                        r.season_number,
                        r.episode_number,
                        r.rank,
                        r.added_at,
                        r.notes
                    FROM ranked_items AS r
                    JOIN latest_posters AS p ON p.tconst = r.display_tconst AND p.rn = 1
                    LEFT JOIN watched_titles AS wt ON wt.display_tconst = r.display_tconst
                    WHERE r.group_row = 1
                      AND r.display_tconst IS NOT NULL
                      AND p.local_path IS NOT NULL
                      AND wt.display_tconst IS NULL
                    ORDER BY r.added_at DESC NULLS LAST, COALESCE(r.title, r.parent_title, r.display_tconst)
                    LIMIT ?
                )
                SELECT
                    g.display_tconst,
                    g.media_type,
                    g.title,
                    g.parent_title,
                    NULL AS season_number,
                    NULL AS episode_number,
                    g.rank,
                    g.added_at,
                    g.notes,
                    'Hot Watchlist' AS name,
                    'view' AS list_kind,
                    t.title_type AS resolved_title_type,
                    t.start_year AS resolved_year,
                    p.local_path AS poster_local_path,
                    NULL AS resolved_series_title,
                    t.primary_title AS resolved_title,
                    ur.rating AS user_rating
                FROM grouped_items AS g
                JOIN app.catalog_titles AS t ON t.tconst = g.display_tconst
                LEFT JOIN latest_posters AS p ON p.tconst = g.display_tconst AND p.rn = 1
                LEFT JOIN latest_user_ratings AS ur ON ur.tconst = g.display_tconst AND ur.rn = 1
                ORDER BY g.added_at DESC NULLS LAST, COALESCE(g.title, g.parent_title, g.display_tconst)
                LIMIT ? OFFSET ?
                """,
                [hot_limit, limit, offset],
            ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "tconst": row[0],
                "media_type": row[1],
                "title": row[15],
                "parent_title": row[3],
                "season_number": row[4],
                "episode_number": row[5],
                "rank": row[6],
                "added_at": row[7],
                "notes": row[8],
                "list_name": row[9],
                "list_kind": row[10],
                "poster_url": db._poster_url_from_local_path(row[13]),
                "title_type": row[11],
                "year": row[12],
                "end_year": None,
                "runtime_minutes": None,
                "series_title": row[14],
                "user_rating": row[16],
            }
        )

    return {
        "list": {
            "id": db.HOT_WATCHLIST_VIEW_ID,
            "slug": "hot-watchlist",
            "name": "Hot Watchlist",
            "list_kind": "view",
            "item_type": "view",
            "view_kind": "hot_watchlist",
        },
        "total": total,
        "items": items,
        "limit": limit,
        "offset": offset,
    }


def get_watched_page(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    db = _db()
    page = db._fetch_watch_view_page(limit, offset, cutoff_days=None)
    return {
        "list": {"id": db.WATCHED_VIEW_ID, "slug": "watched", "name": "Watched", "list_kind": "view", "item_type": "view", "view_kind": "watched"},
        "total": page["total"],
        "items": page["items"],
        "limit": page["limit"],
        "offset": page["offset"],
    }


def get_local_library_status() -> dict[str, Any]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        counts = {
            "lists": conn.execute("SELECT COUNT(*) FROM app.user_lists").fetchone()[0],
            "list_items": conn.execute("SELECT COUNT(*) FROM app.user_list_items WHERE is_archived = FALSE").fetchone()[0],
            "watchlist_items": conn.execute(
                """
                SELECT COUNT(*)
                FROM app.user_list_items AS i
                JOIN app.user_lists AS l ON l.id = i.list_id
                WHERE i.is_archived = FALSE AND l.list_kind = 'watchlist'
                """
            ).fetchone()[0],
            "ratings": conn.execute("SELECT COUNT(*) FROM app.user_ratings").fetchone()[0],
            "favorite_people": conn.execute("SELECT COUNT(*) FROM app.user_people WHERE is_favorite = TRUE").fetchone()[0],
            "watch_events": conn.execute("SELECT COUNT(*) FROM app.watch_events").fetchone()[0],
        }
        lists = conn.execute(
            """
            WITH grouped_items AS (
                SELECT
                    i.list_id,
                    COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AS display_tconst
                FROM app.user_list_items AS i
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
                WHERE i.is_archived = FALSE
            )
            SELECT l.id, l.slug, l.name, l.description, l.list_kind, COUNT(DISTINCT g.display_tconst) AS item_count
            FROM app.user_lists AS l
            LEFT JOIN grouped_items AS g ON g.list_id = l.id AND g.display_tconst IS NOT NULL
            GROUP BY 1,2,3,4,5
            ORDER BY l.list_kind, l.name
            """
        ).fetchall()
        ui_config = db.get_ui_config()
    watchlist_count = get_user_list_items_page("watchlist", limit=1, offset=0)["total"]
    recently_watched_count = get_recently_watched_page(limit=1, offset=0)["total"]
    hot_watchlist_count = get_hot_watchlist_page(limit=1, offset=0)["total"]
    watched_count = get_watched_page(limit=1, offset=0)["total"]
    base_lists = [{"id": row[0], "slug": row[1], "name": row[2], "description": row[3], "list_kind": row[4], "item_count": row[5], "item_type": "list"} for row in lists]
    counts["watchlist_items"] = watchlist_count
    for item in base_lists:
        if item["id"] == "watchlist":
            item["item_count"] = watchlist_count
            break
    visible_lists = list(base_lists)
    visible_lists.append({"id": db.HOT_WATCHLIST_VIEW_ID, "slug": "hot-watchlist", "name": "Hot Watchlist", "description": f"Last {ui_config.hot_watchlist_limit} titles added to Watchlist.", "list_kind": "view", "item_count": hot_watchlist_count, "item_type": "view", "view_kind": "hot_watchlist"})
    visible_lists.append({"id": db.WATCHED_VIEW_ID, "slug": "watched", "name": "Watched", "description": "All watched titles from local history.", "list_kind": "view", "item_count": watched_count, "item_type": "view", "view_kind": "watched"})
    visible_lists.append({"id": db.RECENTLY_WATCHED_VIEW_ID, "slug": "recently-watched", "name": "Recently Watched", "description": f"Local history from the last {ui_config.recently_watched_days} days.", "list_kind": "view", "item_count": recently_watched_count, "item_type": "view", "view_kind": "recently_watched"})

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        if item["id"] == "watchlist":
            return (0, item["name"].lower())
        if item.get("view_kind") == "hot_watchlist":
            return (1, item["name"].lower())
        if item.get("view_kind") == "watched":
            return (2, item["name"].lower())
        if item.get("view_kind") == "recently_watched":
            return (3, item["name"].lower())
        return (4, item["name"].lower())

    return {"counts": counts, "lists": sorted(base_lists, key=sort_key), "visible_lists": sorted(visible_lists, key=sort_key)}


def get_continue_watching_items(limit: int = 5) -> list[dict[str, Any]]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            """
            WITH latest_posters AS (
                SELECT
                    tconst,
                    local_path,
                    row_number() OVER (PARTITION BY tconst ORDER BY fetched_at DESC, id DESC) AS rn
                FROM app.tmdb_assets
                WHERE asset_kind = 'poster' AND status = 'fetched'
            )
            SELECT
                cs.tconst,
                cs.interest_state,
                cs.last_previewed_at,
                cs.last_watched_at,
                cs.updated_at,
                COALESCE(t.title_type, 'tvEpisode') AS title_type,
                COALESCE(t.primary_title, e.primary_title) AS primary_title,
                COALESCE(t.original_title, e.original_title) AS original_title,
                COALESCE(t.start_year, e.start_year) AS start_year,
                COALESCE(t.end_year, NULL) AS end_year,
                COALESCE(t.runtime_minutes, e.runtime_minutes) AS runtime_minutes,
                t.genres,
                t.average_rating,
                t.num_votes,
                e.series_tconst,
                e.season_number,
                e.episode_number,
                s.primary_title AS series_title,
                COALESCE(p.local_path, sp.local_path) AS poster_local_path
            FROM app.content_state AS cs
            LEFT JOIN app.catalog_titles AS t ON t.tconst = cs.tconst
            LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = cs.tconst
            LEFT JOIN app.catalog_titles AS s ON s.tconst = e.series_tconst
            LEFT JOIN latest_posters AS p ON p.tconst = cs.tconst AND p.rn = 1
            LEFT JOIN latest_posters AS sp ON sp.tconst = e.series_tconst AND sp.rn = 1
            WHERE cs.interest_state = 'in_progress'
              AND COALESCE(p.local_path, sp.local_path) IS NOT NULL
            ORDER BY COALESCE(cs.last_previewed_at, cs.updated_at) DESC, COALESCE(cs.last_watched_at, cs.updated_at) DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "tconst": row[0],
            "interest_state": row[1],
            "last_previewed_at": row[2],
            "last_watched_at": row[3],
            "updated_at": row[4],
            "title_type": row[5],
            "title": row[6],
            "original_title": row[7],
            "year": row[8],
            "end_year": row[9],
            "runtime_minutes": row[10],
            "genres": row[11] or [],
            "imdb_rating": row[12],
            "imdb_votes": row[13],
            "series_tconst": row[14],
            "season_number": row[15],
            "episode_number": row[16],
            "series_title": row[17],
            "poster_url": db._poster_url_from_local_path(row[18]),
        }
        items.append(item)
    return items


def get_user_list_items_page(list_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    db = _db()
    with db.duckdb.connect(db.DB_PATH.as_posix(), read_only=True) as conn:
        list_row = conn.execute(
            """
            SELECT id, slug, name, description, list_kind
            FROM app.user_lists
            WHERE id = ?
            """,
            [list_id],
        ).fetchone()
        if list_row is None:
            return {"list": None, "total": 0, "items": [], "limit": limit, "offset": offset}
        exclude_watched = list_row[4] == "watchlist"
        watched_cte = """
            ,
            watched_titles AS (
                SELECT DISTINCT
                    COALESCE(e.series_tconst, w.tconst) AS display_tconst
                FROM app.watch_events AS w
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
                WHERE w.tconst IS NOT NULL
            )
        """ if exclude_watched else ""
        watched_join = "LEFT JOIN watched_titles AS wt ON wt.display_tconst = r.display_tconst" if exclude_watched else ""
        watched_filter = "AND wt.display_tconst IS NULL" if exclude_watched else ""

        total = conn.execute(
            f"""
            WITH latest_posters AS (
                SELECT
                    tconst,
                    local_path,
                    row_number() OVER (PARTITION BY tconst ORDER BY fetched_at DESC, id DESC) AS rn
                FROM app.tmdb_assets
                WHERE asset_kind = 'poster' AND status = 'fetched'
            )
            {watched_cte},
            ranked_items AS (
                SELECT
                    COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AS display_tconst,
                    row_number() OVER (
                        PARTITION BY COALESCE(e.series_tconst, i.tconst, i.parent_tconst)
                        ORDER BY i.rank NULLS LAST, i.added_at DESC NULLS LAST, COALESCE(i.title, i.parent_title, i.tconst)
                    ) AS group_row
                FROM app.user_list_items AS i
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
                WHERE i.list_id = ? AND i.is_archived = FALSE
            )
            SELECT COUNT(*)
            FROM ranked_items AS r
            LEFT JOIN latest_posters AS p ON p.tconst = r.display_tconst AND p.rn = 1
            {watched_join}
            WHERE r.group_row = 1
              AND r.display_tconst IS NOT NULL
              AND p.local_path IS NOT NULL
              {watched_filter}
            """,
            [list_id],
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            WITH latest_posters AS (
                SELECT
                    tconst,
                    local_path,
                    row_number() OVER (PARTITION BY tconst ORDER BY fetched_at DESC, id DESC) AS rn
                FROM app.tmdb_assets
                WHERE asset_kind = 'poster' AND status = 'fetched'
            )
            {watched_cte},
            latest_user_ratings AS (
                SELECT
                    tconst,
                    rating,
                    row_number() OVER (PARTITION BY tconst ORDER BY rated_at DESC NULLS LAST, updated_at DESC, created_at DESC) AS rn
                FROM app.user_ratings
                WHERE tconst IS NOT NULL
            ),
            ranked_items AS (
                SELECT
                    COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AS display_tconst,
                    i.tconst AS source_tconst,
                    i.media_type,
                    i.title,
                    i.parent_title,
                    i.season_number,
                    i.episode_number,
                    i.rank,
                    i.added_at,
                    i.notes,
                    l.name,
                    l.list_kind,
                    row_number() OVER (
                        PARTITION BY COALESCE(e.series_tconst, i.tconst, i.parent_tconst)
                        ORDER BY i.rank NULLS LAST, i.added_at DESC NULLS LAST, COALESCE(i.title, i.parent_title, i.tconst)
                    ) AS group_row
                FROM app.user_list_items AS i
                JOIN app.user_lists AS l ON l.id = i.list_id
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
                WHERE i.list_id = ? AND i.is_archived = FALSE
            )
            SELECT
                r.display_tconst,
                r.media_type,
                r.title,
                r.parent_title,
                NULL AS season_number,
                NULL AS episode_number,
                r.rank,
                r.added_at,
                r.notes,
                r.name,
                r.list_kind,
                t.title_type AS resolved_title_type,
                t.start_year AS resolved_year,
                p.local_path AS poster_local_path,
                NULL AS resolved_series_title,
                t.primary_title AS resolved_title,
                ur.rating AS user_rating
            FROM ranked_items AS r
            JOIN app.catalog_titles AS t ON t.tconst = r.display_tconst
            LEFT JOIN latest_posters AS p ON p.tconst = r.display_tconst AND p.rn = 1
            LEFT JOIN latest_user_ratings AS ur ON ur.tconst = r.display_tconst AND ur.rn = 1
            {watched_join}
            WHERE r.group_row = 1
              AND r.display_tconst IS NOT NULL
              AND p.local_path IS NOT NULL
              {watched_filter}
            ORDER BY r.rank NULLS LAST, r.added_at DESC NULLS LAST, COALESCE(r.title, r.parent_title, r.display_tconst)
            LIMIT ? OFFSET ?
            """,
            [list_id, limit, offset],
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "tconst": row[0],
            "media_type": row[1],
            "title": row[15],
            "parent_title": row[3],
            "season_number": row[4],
            "episode_number": row[5],
            "rank": row[6],
            "added_at": row[7],
            "notes": row[8],
            "list_name": row[9],
            "list_kind": row[10],
            "poster_url": db._poster_url_from_local_path(row[13]),
            "title_type": row[11],
            "year": row[12],
            "end_year": None,
            "runtime_minutes": None,
            "series_title": row[14],
            "user_rating": row[16],
        }
        items.append(item)

    return {
        "list": {
            "id": list_row[0],
            "slug": list_row[1],
            "name": list_row[2],
            "description": list_row[3],
            "list_kind": list_row[4],
        },
        "total": total,
        "items": items,
        "limit": limit,
        "offset": offset,
    }


def get_user_list_items(list_id: str, limit: int = 12) -> list[dict[str, Any]]:
    return get_user_list_items_page(list_id, limit=limit, offset=0)["items"]
