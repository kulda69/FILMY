from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Sequence

import duckdb
from filmy.config import get_ui_config
from filmy.genre_scoring import compute_genre_scores
from filmy.integrations.plex import get_library_sections, get_metadata_snapshot, get_primary_server, iter_section_items
from filmy.paths import ASSETS_DIR, DB_PATH, IMDB_DIR, PEOPLE_ASSETS_DIR, PROJECT_ROOT

BASE_DIR = PROJECT_ROOT
DUCKDB_WRITE_RETRY_ATTEMPTS = 8
DUCKDB_WRITE_RETRY_BASE_DELAY_SECONDS = 0.25


@dataclass(frozen=True)
class SourceFile:
    key: str
    path: Path

    @property
    def stat_signature(self) -> str:
        stat = self.path.stat()
        return f"{int(stat.st_mtime)}:{stat.st_size}"

    @property
    def stat_mtime(self) -> int:
        return int(self.path.stat().st_mtime)

    @property
    def stat_size(self) -> int:
        return self.path.stat().st_size

    @property
    def sha256(self) -> str:
        digest = hashlib.sha256()
        with self.path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


SOURCE_FILES = (
    SourceFile("title_basics", IMDB_DIR / "title.basics.tsv"),
    SourceFile("title_ratings", IMDB_DIR / "title.ratings.tsv"),
    SourceFile("title_episode", IMDB_DIR / "title.episode.tsv"),
    SourceFile("title_akas", IMDB_DIR / "title.akas.tsv"),
    SourceFile("title_crew", IMDB_DIR / "title.crew.tsv"),
    SourceFile("title_principals", IMDB_DIR / "title.principals.tsv"),
    SourceFile("name_basics", IMDB_DIR / "name.basics.tsv"),
)


def ensure_database() -> None:
    """Initialize the DuckDB file and refresh derived IMDb tables when sources change."""
    DB_PATH.parent.mkdir(exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    PEOPLE_ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    with duckdb.connect(DB_PATH.as_posix()) as conn:
        _create_base_schema(conn)
        if _catalog_needs_refresh(conn):
            refresh_catalog(conn)
        _ensure_title_alias_lookup(conn)
        _migrate_watched_alias_list(conn)


def refresh_catalog(conn: duckdb.DuckDBPyConnection | None = None) -> dict[str, int]:
    owns_connection = conn is None
    if owns_connection:
        conn = duckdb.connect(DB_PATH.as_posix())

    try:
        assert conn is not None
        _create_base_schema(conn)

        conn.execute(
            """
            CREATE OR REPLACE TABLE app.catalog_titles AS
            SELECT
                b.tconst,
                b.title_type,
                b.primary_title,
                b.original_title,
                b.start_year,
                b.end_year,
                b.runtime_minutes,
                b.genres,
                r.average_rating,
                r.num_votes
            FROM raw.title_basics AS b
            LEFT JOIN raw.title_ratings AS r USING (tconst)
            WHERE b.title_type IN ('movie', 'tvMovie', 'tvSeries', 'tvMiniSeries')
              AND COALESCE(b.is_adult, FALSE) = FALSE
              AND b.primary_title IS NOT NULL
            """
        )

        conn.execute(
            """
            CREATE OR REPLACE TABLE app.catalog_episodes AS
            SELECT
                e.tconst AS episode_tconst,
                e.parent_tconst AS series_tconst,
                e.season_number,
                e.episode_number,
                b.primary_title,
                b.original_title,
                b.start_year,
                b.runtime_minutes
            FROM raw.title_episode AS e
            JOIN raw.title_basics AS b ON b.tconst = e.tconst
            WHERE b.title_type = 'tvEpisode'
            """
        )

        conn.execute(
            """
            CREATE OR REPLACE TABLE app.title_aliases AS
            SELECT DISTINCT
                title_id AS tconst,
                title,
                region,
                language,
                types,
                is_original_title
            FROM raw.title_akas
            WHERE title IS NOT NULL
            """
        )

        _rebuild_title_alias_lookup(conn)

        conn.execute(
            """
            CREATE OR REPLACE TABLE app.catalog_people AS
            WITH title_scope AS (
                SELECT tconst FROM app.catalog_titles
            ),
            people_scope AS (
                SELECT DISTINCT nconst
                FROM raw.title_principals
                WHERE tconst IN (SELECT tconst FROM title_scope)

                UNION

                SELECT DISTINCT unnest(string_split(directors, ',')) AS nconst
                FROM raw.title_crew
                WHERE directors IS NOT NULL
                  AND tconst IN (SELECT tconst FROM title_scope)

                UNION

                SELECT DISTINCT unnest(string_split(writers, ',')) AS nconst
                FROM raw.title_crew
                WHERE writers IS NOT NULL
                  AND tconst IN (SELECT tconst FROM title_scope)
            )
            SELECT DISTINCT
                nconst,
                primary_name,
                birth_year,
                death_year,
                primary_profession,
                known_for_titles
            FROM raw.name_basics
            WHERE primary_name IS NOT NULL
              AND nconst IN (SELECT nconst FROM people_scope)
            """
        )

        conn.execute(
            """
            CREATE OR REPLACE TABLE app.title_credits AS
            WITH title_scope AS (
                SELECT tconst FROM app.catalog_titles
            ),
            principal_credits AS (
                SELECT
                    p.tconst,
                    p.nconst,
                    CASE
                        WHEN p.category IN ('actor', 'actress') THEN 'cast'
                        WHEN p.category = 'director' THEN 'director'
                        WHEN p.category = 'writer' AND p.job = 'created by' THEN 'creator'
                        WHEN p.category = 'writer' THEN 'writer'
                        ELSE 'principal'
                    END AS credit_group,
                    p.category,
                    p.job,
                    p.characters,
                    p.ordering
                FROM raw.title_principals AS p
                JOIN title_scope AS s USING (tconst)
            ),
            crew_credits AS (
                SELECT
                    c.tconst,
                    c.nconst,
                    'director' AS credit_group,
                    'director' AS category,
                    NULL AS job,
                    NULL AS characters,
                    1000 + c.ordering AS ordering
                FROM (
                    SELECT
                        tconst,
                        nconst,
                        row_number() OVER (PARTITION BY tconst ORDER BY nconst) AS ordering
                    FROM (
                        SELECT
                            tconst,
                            unnest(string_split(directors, ',')) AS nconst
                        FROM raw.title_crew
                        WHERE directors IS NOT NULL
                          AND tconst IN (SELECT tconst FROM title_scope)
                    ) AS expanded_directors
                ) AS c

                UNION ALL

                SELECT
                    c.tconst,
                    c.nconst,
                    'writer' AS credit_group,
                    'writer' AS category,
                    NULL AS job,
                    NULL AS characters,
                    2000 + c.ordering AS ordering
                FROM (
                    SELECT
                        tconst,
                        nconst,
                        row_number() OVER (PARTITION BY tconst ORDER BY nconst) AS ordering
                    FROM (
                        SELECT
                            tconst,
                            unnest(string_split(writers, ',')) AS nconst
                        FROM raw.title_crew
                        WHERE writers IS NOT NULL
                          AND tconst IN (SELECT tconst FROM title_scope)
                    ) AS expanded_writers
                ) AS c
            ),
            combined AS (
                SELECT * FROM principal_credits
                UNION ALL
                SELECT * FROM crew_credits
            ),
            deduped AS (
                SELECT
                    tconst,
                    nconst,
                    credit_group,
                    category,
                    job,
                    characters,
                    ordering,
                    row_number() OVER (
                        PARTITION BY tconst, nconst, credit_group, COALESCE(job, '')
                        ORDER BY ordering
                    ) AS rn
                FROM combined
            )
            SELECT
                tconst,
                nconst,
                credit_group,
                category,
                job,
                characters,
                ordering
            FROM deduped
            WHERE rn = 1
            """
        )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_catalog_titles_primary_title ON app.catalog_titles(primary_title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_catalog_titles_start_year ON app.catalog_titles(start_year)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_catalog_episodes_series_season_episode "
            "ON app.catalog_episodes(series_tconst, season_number, episode_number)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_catalog_episodes_primary_title ON app.catalog_episodes(primary_title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_catalog_people_name ON app.catalog_people(primary_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_title_credits_tconst_group_ordering ON app.title_credits(tconst, credit_group, ordering)")
        _ensure_title_alias_lookup_indexes(conn)

        conn.execute(
            """
            CREATE OR REPLACE TABLE app.imdb_file_manifest AS
            SELECT * FROM (
                VALUES
                    (?, ?, ?, ?, ?, ?),
                    (?, ?, ?, ?, ?, ?),
                    (?, ?, ?, ?, ?, ?),
                    (?, ?, ?, ?, ?, ?),
                    (?, ?, ?, ?, ?, ?),
                    (?, ?, ?, ?, ?, ?),
                    (?, ?, ?, ?, ?, ?)
            ) AS meta(source_key, source_path, source_mtime, source_size, source_sha256, recorded_at)
            """,
            [
                SOURCE_FILES[0].key,
                SOURCE_FILES[0].path.as_posix(),
                SOURCE_FILES[0].stat_mtime,
                SOURCE_FILES[0].stat_size,
                SOURCE_FILES[0].sha256,
                _now_iso(),
                SOURCE_FILES[1].key,
                SOURCE_FILES[1].path.as_posix(),
                SOURCE_FILES[1].stat_mtime,
                SOURCE_FILES[1].stat_size,
                SOURCE_FILES[1].sha256,
                _now_iso(),
                SOURCE_FILES[2].key,
                SOURCE_FILES[2].path.as_posix(),
                SOURCE_FILES[2].stat_mtime,
                SOURCE_FILES[2].stat_size,
                SOURCE_FILES[2].sha256,
                _now_iso(),
                SOURCE_FILES[3].key,
                SOURCE_FILES[3].path.as_posix(),
                SOURCE_FILES[3].stat_mtime,
                SOURCE_FILES[3].stat_size,
                SOURCE_FILES[3].sha256,
                _now_iso(),
                SOURCE_FILES[4].key,
                SOURCE_FILES[4].path.as_posix(),
                SOURCE_FILES[4].stat_mtime,
                SOURCE_FILES[4].stat_size,
                SOURCE_FILES[4].sha256,
                _now_iso(),
                SOURCE_FILES[5].key,
                SOURCE_FILES[5].path.as_posix(),
                SOURCE_FILES[5].stat_mtime,
                SOURCE_FILES[5].stat_size,
                SOURCE_FILES[5].sha256,
                _now_iso(),
                SOURCE_FILES[6].key,
                SOURCE_FILES[6].path.as_posix(),
                SOURCE_FILES[6].stat_mtime,
                SOURCE_FILES[6].stat_size,
                SOURCE_FILES[6].sha256,
                _now_iso(),
            ],
        )

        conn.execute(
            """
            CREATE OR REPLACE TABLE app.catalog_refresh_meta AS
            SELECT source_key, source_mtime || ':' || source_size AS fingerprint
            FROM app.imdb_file_manifest
            """
        )

        stats = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM app.catalog_titles),
                (SELECT COUNT(*) FROM app.catalog_episodes),
                (SELECT COUNT(*) FROM app.title_aliases),
                (SELECT COUNT(*) FROM app.catalog_people),
                (SELECT COUNT(*) FROM app.title_credits)
            """
        ).fetchone()
        return {
            "titles": stats[0],
            "episodes": stats[1],
            "aliases": stats[2],
            "people": stats[3],
            "credits": stats[4],
        }
    finally:
        clear_title_presentation_cache()
        if owns_connection and conn is not None:
            conn.close()


def refresh_catalog_with_retry() -> dict[str, int]:
    """Refresh catalog tables with the standard DuckDB write-lock retry policy."""

    return _run_duckdb_write(lambda conn: refresh_catalog(conn))


def _rebuild_title_alias_lookup(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        f"""
        CREATE OR REPLACE TABLE app.title_alias_lookup AS
        SELECT
            tconst,
            title,
            region,
            language,
            {_alias_priority_case_sql('region', 'language')} AS alias_priority,
            {_duckdb_match_key_sql('title')} AS alias_key,
            {_duckdb_match_key_sql('title', strip_leading_articles=True)} AS alias_key_articleless,
            length({_duckdb_match_key_sql('title')}) AS alias_length,
            length({_duckdb_match_key_sql('title', strip_leading_articles=True)}) AS alias_length_articleless,
            left({_duckdb_match_key_sql('title', strip_leading_articles=True)}, 1) AS alias_prefix1_articleless,
            left({_duckdb_match_key_sql('title', strip_leading_articles=True)}, 2) AS alias_prefix2_articleless,
            left({_duckdb_match_key_sql('title', strip_leading_articles=True)}, 3) AS alias_prefix3_articleless
        FROM app.title_aliases
        WHERE title IS NOT NULL
        """
    )


def _ensure_title_alias_lookup_indexes(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_tconst ON app.title_alias_lookup(tconst)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_alias_key ON app.title_alias_lookup(alias_key)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_alias_key_articleless "
        "ON app.title_alias_lookup(alias_key_articleless)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_prefix3 "
        "ON app.title_alias_lookup(alias_prefix3_articleless)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_prefix2 "
        "ON app.title_alias_lookup(alias_prefix2_articleless)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_prefix1_len "
        "ON app.title_alias_lookup(alias_prefix1_articleless, alias_length_articleless)"
    )


def _ensure_title_alias_lookup(conn: duckdb.DuckDBPyConnection) -> None:
    table_exists = conn.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = 'app' AND table_name = 'title_alias_lookup'
        """
    ).fetchone()[0]
    if not table_exists:
        _rebuild_title_alias_lookup(conn)
    _ensure_title_alias_lookup_indexes(conn)


def clear_title_presentation_cache() -> None:
    _get_title_presentation_cached.cache_clear()


def get_catalog_stats() -> dict[str, int | str | None]:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS titles,
                COUNT(*) FILTER (WHERE title_type = 'movie') AS movies,
                COUNT(*) FILTER (WHERE title_type IN ('tvSeries', 'tvMiniSeries')) AS series,
                MIN(start_year) AS oldest_year,
                MAX(start_year) AS newest_year,
                (SELECT COUNT(*) FROM app.catalog_episodes) AS episodes,
                (SELECT COUNT(*) FROM app.title_aliases) AS aliases
            FROM app.catalog_titles
            """
        ).fetchone()

    return {
        "titles": row[0],
        "movies": row[1],
        "series": row[2],
        "oldest_year": row[3],
        "newest_year": row[4],
        "episodes": row[5],
        "aliases": row[6],
        "database_path": DB_PATH.as_posix(),
        "assets_path": ASSETS_DIR.as_posix(),
    }


def get_imdb_manifest() -> list[dict[str, Any]]:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT source_key, source_path, source_mtime, source_size, source_sha256, recorded_at
            FROM app.imdb_file_manifest
            ORDER BY source_key
            """
        ).fetchall()
    return [
        {
            "source_key": row[0],
            "source_path": row[1],
            "source_mtime": row[2],
            "source_size": row[3],
            "source_sha256": row[4],
            "recorded_at": row[5],
        }
        for row in rows
    ]


def search_catalog(query: str | None, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    sql = """
        SELECT
            tconst,
            title_type,
            primary_title,
            original_title,
            start_year,
            runtime_minutes,
            genres,
            average_rating,
            num_votes
        FROM app.catalog_titles
        WHERE (? IS NULL OR primary_title ILIKE '%' || ? || '%' OR original_title ILIKE '%' || ? || '%')
          AND (? IS NULL OR title_type = ?)
        ORDER BY
            CASE WHEN average_rating IS NULL THEN 1 ELSE 0 END,
            average_rating DESC,
            num_votes DESC,
            start_year DESC NULLS LAST,
            primary_title
        LIMIT ?
    """

    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(sql, [query, query, query, title_type, title_type, limit]).fetchall()
        items = []
        for row in rows:
            item = _catalog_row_to_dict(row)
            item["library"] = _fetch_library_summary(conn, item["tconst"], item["title_type"])
            items.append(item)
    return items


def get_content_detail(tconst: str) -> dict[str, Any] | None:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        title = conn.execute(
            """
            SELECT
                tconst,
                title_type,
                primary_title,
                original_title,
                start_year,
                end_year,
                runtime_minutes,
                genres,
                average_rating,
                num_votes
            FROM app.catalog_titles
            WHERE tconst = ?
            """,
            [tconst],
        ).fetchone()
        if title is None:
            episode = conn.execute(
                """
                SELECT
                    episode_tconst,
                    series_tconst,
                    season_number,
                    episode_number,
                    primary_title,
                    original_title,
                    start_year,
                    runtime_minutes
                FROM app.catalog_episodes
                WHERE episode_tconst = ?
                """,
                [tconst],
            ).fetchone()
            if episode is None:
                return None
            return {
                "tconst": episode[0],
                "kind": "episode",
                "series_tconst": episode[1],
                "season_number": episode[2],
                "episode_number": episode[3],
                "primary_title": episode[4],
                "original_title": episode[5],
                "start_year": episode[6],
                "runtime_minutes": episode[7],
                "aliases": _fetch_aliases(conn, tconst),
                "tmdb": _fetch_tmdb(conn, tconst),
                "content_state": _fetch_content_state(conn, tconst),
                "library": _fetch_library_summary(conn, tconst, "tvEpisode"),
            }

        detail = {
            "kind": "title",
            **_catalog_row_to_dict(title),
            "aliases": _fetch_aliases(conn, tconst),
            "tmdb": _fetch_tmdb(conn, tconst),
            "content_state": _fetch_content_state(conn, tconst),
            "library": _fetch_library_summary(conn, tconst, title[1]),
        }
        if title[1] in ("tvSeries", "tvMiniSeries"):
            detail["episodes"] = conn.execute(
                """
                WITH latest_episode_ratings AS (
                    SELECT
                        tconst,
                        rating,
                        row_number() OVER (
                            PARTITION BY tconst
                            ORDER BY rated_at DESC NULLS LAST, updated_at DESC, created_at DESC
                        ) AS rn
                    FROM app.user_ratings
                    WHERE tconst IS NOT NULL
                ),
                episode_watch_counts AS (
                    SELECT tconst, COUNT(*) AS watched_count
                    FROM app.watch_events
                    GROUP BY tconst
                )
                SELECT
                    episode_tconst,
                    season_number,
                    episode_number,
                    primary_title,
                    start_year,
                    er.rating,
                    COALESCE(ew.watched_count, 0) AS watched_count
                FROM app.catalog_episodes
                LEFT JOIN latest_episode_ratings AS er ON er.tconst = episode_tconst AND er.rn = 1
                LEFT JOIN episode_watch_counts AS ew ON ew.tconst = episode_tconst
                WHERE series_tconst = ?
                ORDER BY season_number NULLS LAST, episode_number NULLS LAST, episode_tconst
                LIMIT 50
                """,
                [tconst],
            ).fetchall()
        return detail


def describe_title_by_query(query: str, title_type: str | None = None) -> dict[str, Any] | None:
    lookup = lookup_title_by_query(query=query, title_type=title_type, candidates_limit=5)
    if lookup is None:
        return None
    return lookup["selected"]


def describe_person_by_query(query: str) -> dict[str, Any] | None:
    lookup = lookup_person_by_query(query=query, candidates_limit=5)
    if lookup is None:
        return None
    return lookup["selected"]


def lookup_title_by_query(
    query: str,
    title_type: str | None = None,
    candidates_limit: int = 5,
) -> dict[str, Any] | None:
    candidates = _search_catalog_for_lookup(query=query, title_type=title_type, limit=max(candidates_limit, 1) * 5)
    alias_candidates = _search_catalog_aliases_for_lookup(query=query, title_type=title_type, limit=max(candidates_limit, 1) * 5)
    candidates = _merge_lookup_candidates(candidates, alias_candidates)
    query_key = _normalize_match_key(query)
    query_tokens = _match_tokens(query_key)
    if candidates:
        direct_selected = _pick_best_title_match(query, candidates)
        if _is_direct_enough_lookup(query, direct_selected):
            return _build_title_lookup_result(
                query=query,
                title_type=title_type,
                selected=direct_selected,
                candidates=candidates,
                candidates_limit=candidates_limit,
            )
    should_expand = not candidates or _should_expand_to_fuzzy(query, candidates)
    if should_expand:
        fuzzy_candidates = _search_catalog_for_lookup_fuzzy(query=query, title_type=title_type, limit=max(candidates_limit, 1) * 5)
        candidates = _merge_lookup_candidates(candidates, fuzzy_candidates)
        alias_fuzzy_candidates = _search_catalog_aliases_for_lookup_fuzzy(
            query=query,
            title_type=title_type,
            limit=max(candidates_limit, 1) * 5,
        )
        candidates = _merge_lookup_candidates(candidates, alias_fuzzy_candidates)
    if not candidates:
        return None

    selected = _pick_best_title_match(query, candidates)
    if len(query_tokens) > 1 and not _is_confident_lookup(query, selected):
        wide_candidates = _search_catalog_for_lookup_levenshtein(query=query, title_type=title_type, limit=max(candidates_limit, 1) * 5)
        candidates = _merge_lookup_candidates(candidates, wide_candidates)
        alias_wide_candidates = _search_catalog_aliases_for_lookup_levenshtein(
            query=query,
            title_type=title_type,
            limit=max(candidates_limit, 1) * 5,
        )
        candidates = _merge_lookup_candidates(candidates, alias_wide_candidates)
        if not candidates:
            return None
        selected = _pick_best_title_match(query, candidates)
    else:
        selected = _pick_best_title_match(query, candidates)
    return _build_title_lookup_result(
        query=query,
        title_type=title_type,
        selected=selected,
        candidates=candidates,
        candidates_limit=candidates_limit,
    )


def lookup_person_by_query(query: str, candidates_limit: int = 5) -> dict[str, Any] | None:
    candidates = _search_people_for_lookup(query=query, limit=max(candidates_limit, 1) * 5)
    query_key = _normalize_match_key(query)
    should_expand = not candidates or _should_expand_people_to_fuzzy(query, candidates)
    if should_expand:
        fuzzy_candidates = _search_people_for_lookup_fuzzy(query=query, limit=max(candidates_limit, 1) * 5)
        candidates = _merge_lookup_candidates(candidates, fuzzy_candidates)
    if not candidates:
        return None

    selected = _pick_best_person_match(query, candidates)
    if not _is_confident_person_lookup(query, selected):
        wide_candidates = _search_people_for_lookup_levenshtein(query=query, limit=max(candidates_limit, 1) * 5)
        candidates = _merge_lookup_candidates(candidates, wide_candidates)
        if not candidates:
            return None
        selected = _pick_best_person_match(query, candidates)
    else:
        selected = _pick_best_person_match(query, candidates)

    presentation = get_person_presentation(selected["nconst"])
    if presentation is None:
        return None

    presentation["query"] = query
    presentation["match"] = _build_person_lookup_candidate(selected, query=query, is_selected=True)

    selected_key = selected["nconst"]
    ordered_candidates = sorted(
        candidates,
        key=lambda item: (0 if item["nconst"] == selected_key else 1, -(item.get("birth_year") or 0), item["primary_name"]),
    )
    return {
        "query": query,
        "selected_nconst": selected_key,
        "selected": presentation,
        "candidates": [
            _build_person_lookup_candidate(candidate, query=query, is_selected=(candidate["nconst"] == selected_key))
            for candidate in ordered_candidates[: max(candidates_limit, 1)]
        ],
        "candidate_count": len(candidates),
    }


def get_person_presentation(nconst: str) -> dict[str, Any] | None:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        cached = _load_cached_person_presentation(conn, nconst)
        if cached is not None:
            return cached
        presentation = _fetch_person_cache_source_detail(conn, nconst)
        if presentation is None:
            return None

    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        cache_fingerprint = _person_cache_source_fingerprint(conn, nconst, presentation)
    _store_cached_person_presentation(nconst, presentation, cache_fingerprint)
    return presentation


def _fetch_known_for_items(conn: duckdb.DuckDBPyConnection, known_for_titles: str | None) -> list[dict[str, Any]]:
    if not known_for_titles:
        return []

    ordered_tconsts = [item.strip() for item in str(known_for_titles).split(",") if item.strip()]
    if not ordered_tconsts:
        return []

    placeholders = ", ".join("?" for _ in ordered_tconsts)
    rows = conn.execute(
        f"""
        SELECT
            tconst,
            primary_title,
            start_year
        FROM app.catalog_titles
        WHERE tconst IN ({placeholders})
        """,
        ordered_tconsts,
    ).fetchall()
    items_by_tconst = {
        row[0]: {
            "tconst": row[0],
            "title": row[1],
            "start_year": row[2],
        }
        for row in rows
    }
    return [items_by_tconst[tconst] for tconst in ordered_tconsts if tconst in items_by_tconst]


def render_person_presentation(presentation: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(str(presentation["name"]))

    meta_bits: list[str] = []
    if presentation.get("birth_year") is not None:
        meta_bits.append(str(presentation["birth_year"]))
    if presentation.get("death_year") is not None:
        meta_bits.append(str(presentation["death_year"]))
    if presentation.get("primary_profession"):
        meta_bits.append(str(presentation["primary_profession"]))
    if meta_bits:
        lines.append(", ".join(meta_bits))

    known_for_items = presentation.get("known_for_items") or []
    known_for = presentation.get("known_for_titles") or ""
    if known_for_items:
        lines.append("")
        lines.append("Known for")
        lines.append(", ".join(item["title"] for item in known_for_items))
    elif known_for:
        lines.append("")
        lines.append("Known for")
        lines.append(known_for)

    filmography = presentation.get("filmography") or {}
    sections = [
        ("Directed", filmography.get("directed") or []),
        ("Created by", filmography.get("created") or []),
        ("Written", filmography.get("written") or []),
        ("Acted in", filmography.get("acted") or []),
    ]
    for section_title, items in sections:
        if not items:
            continue
        lines.append("")
        lines.append(section_title)
        for item in items[:20]:
            year = f" ({item['start_year']})" if item.get("start_year") is not None else ""
            role = f" as {item['character']}" if item.get("character") else ""
            lines.append(f"{item['title']}{year}{role}")

    other_items = filmography.get("other") or []
    if other_items:
        lines.append("")
        lines.append("Other credits")
        for item in other_items[:10]:
            year = f" ({item['start_year']})" if item.get("start_year") is not None else ""
            lines.append(f"{item['title']}{year}")

    lines.append("")
    lines.append(f"Total credits: {presentation.get('credit_count') or 0}")
    return "\n".join(lines)


@lru_cache(maxsize=256)
def _get_title_presentation_cached(tconst: str) -> dict[str, Any] | None:
    if not tconst:
        return None
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        cached = _load_cached_title_presentation(conn, tconst)
        if cached is not None:
            return cached

    detail = get_content_detail(tconst)
    if detail is None:
        return None

    series_title = detail.get("series_title")
    if detail.get("kind") == "episode" and detail.get("series_tconst") and series_title is None:
        with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
            series_row = conn.execute(
                """
                SELECT primary_title
                FROM app.catalog_titles
                WHERE tconst = ?
                """,
                [detail["series_tconst"]],
            ).fetchone()
        series_title = series_row[0] if series_row is not None else None

    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        people = _fetch_title_people(conn, tconst)
        cache_fingerprint = _title_cache_source_fingerprint(conn, tconst)

    overview = (((detail.get("tmdb") or {}).get("details") or {}).get("overview"))
    providers = [
        provider["provider_name"]
        for provider in ((detail.get("tmdb") or {}).get("providers") or [])
        if provider.get("provider_name")
    ]
    unique_providers = list(dict.fromkeys(providers))

    presentation = {
        "tconst": detail["tconst"],
        "title": detail.get("primary_title"),
        "original_title": detail.get("original_title"),
        "title_type": detail.get("title_type"),
        "kind": detail.get("kind"),
        "kind_label": _title_type_label(detail.get("title_type")),
        "year": detail.get("start_year"),
        "end_year": detail.get("end_year"),
        "runtime_minutes": detail.get("runtime_minutes"),
        "genres": detail.get("genres") or [],
        "imdb_rating": detail.get("average_rating"),
        "imdb_votes": detail.get("num_votes"),
        "overview": overview,
        "directed_by": people["directors"],
        "written_by": people["writers"],
        "created_by": people["creators"],
        "main_cast": people["cast"],
        "available_in_czechia": unique_providers,
        "library_state": detail.get("library") or {},
        "episodes": detail.get("episodes") or [],
        "aliases": detail.get("aliases") or [],
        "tmdb_locales": ((detail.get("tmdb") or {}).get("detail_locales") or []),
        "poster_url": _poster_url_from_detail(detail),
        "series_tconst": detail.get("series_tconst"),
        "series_title": series_title,
        "season_number": detail.get("season_number"),
        "episode_number": detail.get("episode_number"),
        "has_poster": any(asset.get("asset_kind") == "poster" for asset in ((detail.get("tmdb") or {}).get("assets") or [])),
        "has_backdrop": any(
            asset.get("asset_kind") == "backdrop" for asset in ((detail.get("tmdb") or {}).get("assets") or [])
        ),
    }
    presentation["display_text"] = render_title_presentation(presentation)
    _store_cached_title_presentation(tconst, presentation, cache_fingerprint)
    return presentation


def get_title_presentation(tconst: str) -> dict[str, Any] | None:
    return _get_title_presentation_cached(tconst)


def render_title_presentation(presentation: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(str(presentation["title"]))

    meta_bits = [presentation["kind_label"]]
    if presentation.get("year") is not None:
        meta_bits.append(str(presentation["year"]))
    lines.append(", ".join(meta_bits))

    genres = presentation.get("genres") or []
    if genres:
        lines.append(" / ".join(genres))

    rating = presentation.get("imdb_rating")
    if rating is not None:
        votes = presentation.get("imdb_votes")
        vote_suffix = f" ({votes} votes)" if votes is not None else ""
        lines.append(f"IMDb: {rating}/10{vote_suffix}")

    overview = presentation.get("overview")
    if overview:
        lines.append("")
        lines.append("What it's about")
        lines.append(str(overview))

    if presentation.get("created_by"):
        lines.append("")
        lines.append("Created by")
        lines.append(", ".join(person["name"] for person in presentation["created_by"]))

    if presentation.get("directed_by"):
        lines.append("")
        lines.append("Directed by")
        lines.append(", ".join(person["name"] for person in presentation["directed_by"]))

    if presentation.get("written_by"):
        lines.append("")
        lines.append("Written by")
        lines.append(", ".join(person["name"] for person in presentation["written_by"]))

    if presentation.get("main_cast"):
        lines.append("")
        lines.append("Main cast")
        for person in presentation["main_cast"]:
            role = f" as {person['character']}" if person.get("character") else ""
            lines.append(f"{person['name']}{role}")

    if presentation.get("available_in_czechia"):
        lines.append("")
        lines.append("Available in Czechia")
        for provider in presentation["available_in_czechia"]:
            lines.append(provider)

    library = presentation.get("library_state") or {}
    if library:
        lines.append("")
        lines.append("Your local library state")
        if library.get("watched_count") is not None:
            times = "time" if library["watched_count"] == 1 else "times"
            lines.append(f"Watched {library['watched_count']} {times}")
        if library.get("last_watched_at") is not None:
            lines.append(f"Last watched: {library['last_watched_at']}")
        if library.get("in_watchlist"):
            lines.append("In watchlist")
        if library.get("rating") is not None:
            lines.append(f"Your rating: {library['rating']}/10")

    if presentation.get("episodes"):
        lines.append("")
        lines.append("Episodes")
        lines.append(f"{len(presentation['episodes'])} episodes loaded")

    local_bits: list[str] = []
    if presentation.get("tmdb_locales"):
        local_bits.append("TMDB detail: " + ", ".join(presentation["tmdb_locales"]))
    asset_bits: list[str] = []
    if presentation.get("has_poster"):
        asset_bits.append("poster")
    if presentation.get("has_backdrop"):
        asset_bits.append("backdrop")
    if asset_bits:
        local_bits.append("assets: " + ", ".join(asset_bits))
    if local_bits:
        lines.append("")
        lines.append("Available locally")
        lines.extend(local_bits)

    return "\n".join(lines)


def _title_detail_cache_path(tconst: str) -> Path:
    if not tconst:
        raise ValueError("tconst is required")
    return ASSETS_DIR / tconst / "detail.json"


def _person_detail_cache_path(nconst: str) -> Path:
    if not nconst:
        raise ValueError("nconst is required")
    return PEOPLE_ASSETS_DIR / nconst / "detail.json"


def _person_portrait_path(nconst: str) -> Path | None:
    if not nconst:
        return None
    person_dir = PEOPLE_ASSETS_DIR / nconst
    for suffix in ("jpg", "jpeg", "webp", "png"):
        candidate = person_dir / f"portrait.{suffix}"
        if candidate.exists():
            return candidate
    return None


def _person_portrait_url(nconst: str) -> str | None:
    portrait_path = _person_portrait_path(nconst)
    if portrait_path is None:
        return None
    try:
        return f"/assets/people/{portrait_path.relative_to(PEOPLE_ASSETS_DIR).as_posix()}"
    except ValueError:
        return None


def _title_detail_cache_status(tconst: str, source_fingerprint: str) -> str:
    cache_path = _title_detail_cache_path(tconst)
    if not cache_path.exists():
        return "missing"
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(cached, dict):
        return "invalid"
    if cached.get("tconst") != tconst:
        return "stale"
    if cached.get("source_fingerprint") != source_fingerprint:
        return "stale"
    return "ready"


def _person_detail_cache_status(nconst: str, source_fingerprint: str) -> str:
    cache_path = _person_detail_cache_path(nconst)
    if not cache_path.exists():
        return "missing"
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(cached, dict):
        return "invalid"
    if cached.get("nconst") != nconst:
        return "stale"
    if cached.get("source_fingerprint") != source_fingerprint:
        return "stale"
    return "ready"


def _load_cached_title_presentation(
    conn: duckdb.DuckDBPyConnection,
    tconst: str,
) -> dict[str, Any] | None:
    cache_path = _title_detail_cache_path(tconst)
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get("tconst") != tconst:
        return None
    expected_fingerprint = _title_cache_source_fingerprint(conn, tconst)
    if expected_fingerprint is None:
        return None
    if _title_detail_cache_status(tconst, expected_fingerprint) != "ready":
        return None
    if cached.get("kind") not in {"title", "episode"}:
        return None
    if "has_poster" not in cached or "has_backdrop" not in cached:
        return None
    if cached.get("has_poster") and not cached.get("poster_url"):
        return None
    return cached


def _load_cached_person_presentation(
    conn: duckdb.DuckDBPyConnection,
    nconst: str,
) -> dict[str, Any] | None:
    cache_path = _person_detail_cache_path(nconst)
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get("nconst") != nconst:
        return None
    expected_fingerprint = _person_cache_source_fingerprint(conn, nconst)
    if expected_fingerprint is None:
        return None
    if _person_detail_cache_status(nconst, expected_fingerprint) != "ready":
        return None
    return cached


def _store_cached_title_presentation(tconst: str, presentation: dict[str, Any], source_fingerprint: str | None) -> None:
    if not source_fingerprint:
        return
    cache_path = _title_detail_cache_path(tconst)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _jsonify_for_cache({**presentation, "source_fingerprint": source_fingerprint, "cached_at": _now_iso()})
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _store_cached_person_presentation(nconst: str, presentation: dict[str, Any], source_fingerprint: str | None) -> None:
    if not source_fingerprint:
        return
    cache_path = _person_detail_cache_path(nconst)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _jsonify_for_cache({**presentation, "source_fingerprint": source_fingerprint, "cached_at": _now_iso()})
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _title_cache_source_fingerprint(
    conn: duckdb.DuckDBPyConnection,
    tconst: str,
    detail: dict[str, Any] | None = None,
) -> str | None:
    refresh_fingerprint = conn.execute(
        """
        SELECT string_agg(source_key || '=' || fingerprint, '|'
                         ORDER BY source_key)
        FROM app.catalog_refresh_meta
        """
    ).fetchone()[0]
    if refresh_fingerprint is None:
        return None

    if detail is None:
        detail = _fetch_title_cache_source_detail(conn, tconst)
    if detail is None:
        return None

    payload = {
        "refresh": refresh_fingerprint,
        "detail": {
            "tconst": detail.get("tconst"),
            "title_type": detail.get("title_type") or detail.get("kind"),
            "title": detail.get("primary_title"),
            "original_title": detail.get("original_title"),
            "start_year": detail.get("start_year"),
            "end_year": detail.get("end_year"),
            "runtime_minutes": detail.get("runtime_minutes"),
            "genres": detail.get("genres") or [],
            "tmdb_locales": ((detail.get("tmdb") or {}).get("detail_locales") or []),
            "tmdb_assets": _tmdb_asset_summary_signature(detail.get("tmdb") or {}),
            "library": detail.get("library") or {},
            "content_state": detail.get("content_state") or {},
            "aliases": detail.get("aliases") or [],
        },
    }
    digest = hashlib.sha256(json.dumps(_jsonify_for_cache(payload), sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def _person_cache_source_fingerprint(
    conn: duckdb.DuckDBPyConnection,
    nconst: str,
    presentation: dict[str, Any] | None = None,
) -> str | None:
    refresh_fingerprint = conn.execute(
        """
        SELECT string_agg(source_key || '=' || fingerprint, '|'
                         ORDER BY source_key)
        FROM app.catalog_refresh_meta
        """
    ).fetchone()[0]
    if refresh_fingerprint is None:
        return None

    if presentation is None:
        presentation = _fetch_person_cache_source_detail(conn, nconst)
        if presentation is None:
            return None

    payload = {
        "refresh": refresh_fingerprint,
        "person": {
            "nconst": presentation.get("nconst"),
            "name": presentation.get("name"),
            "birth_year": presentation.get("birth_year"),
            "death_year": presentation.get("death_year"),
            "primary_profession": presentation.get("primary_profession"),
            "known_for_titles": presentation.get("known_for_titles"),
            "filmography": presentation.get("filmography") or {},
            "credit_count": presentation.get("credit_count"),
            "portrait_url": presentation.get("portrait_url"),
            "has_portrait": presentation.get("has_portrait"),
        },
    }
    digest = hashlib.sha256(json.dumps(_jsonify_for_cache(payload), sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def _fetch_person_cache_source_detail(conn: duckdb.DuckDBPyConnection, nconst: str) -> dict[str, Any] | None:
    person = conn.execute(
        """
        SELECT nconst, primary_name, birth_year, death_year, primary_profession, known_for_titles
        FROM app.catalog_people
        WHERE nconst = ?
        """,
        [nconst],
    ).fetchone()
    if person is None:
        return None

    credits = conn.execute(
        """
        SELECT
            c.credit_group,
            c.category,
            c.job,
            c.characters,
            c.ordering,
            t.tconst,
            t.primary_title,
            t.original_title,
            t.start_year,
            t.title_type
        FROM app.title_credits AS c
        JOIN app.catalog_titles AS t ON t.tconst = c.tconst
        WHERE c.nconst = ?
        ORDER BY
            CASE c.credit_group
                WHEN 'director' THEN 0
                WHEN 'creator' THEN 1
                WHEN 'writer' THEN 2
                WHEN 'cast' THEN 3
                ELSE 4
            END,
            t.start_year DESC NULLS LAST,
            c.ordering,
            t.primary_title
        LIMIT 500
        """,
        [nconst],
    ).fetchall()

    filmography: dict[str, list[dict[str, Any]]] = {
        "directed": [],
        "written": [],
        "created": [],
        "acted": [],
        "other": [],
    }
    credit_count = 0
    seen_titles: set[str] = set()
    for row in credits:
        credit_count += 1
        tconst = row[5]
        if tconst in seen_titles:
            continue
        seen_titles.add(tconst)
        entry = {
            "tconst": tconst,
            "title": row[6],
            "original_title": row[7],
            "start_year": row[8],
            "title_type": row[9],
            "credit_group": row[0],
            "category": row[1],
            "job": row[2],
            "character": _principal_character(row[3]),
        }
        if row[0] == "director":
            filmography["directed"].append(entry)
        elif row[0] == "creator":
            filmography["created"].append(entry)
        elif row[0] == "writer":
            filmography["written"].append(entry)
        elif row[0] == "cast":
            filmography["acted"].append(entry)
        else:
            filmography["other"].append(entry)

    presentation = {
        "nconst": person[0],
        "name": person[1],
        "birth_year": person[2],
        "death_year": person[3],
        "primary_profession": person[4],
        "known_for_titles": person[5],
        "known_for_items": _fetch_known_for_items(conn, person[5]),
        "filmography": filmography,
        "credit_count": credit_count,
        "portrait_url": _person_portrait_url(person[0]),
        "has_portrait": _person_portrait_path(person[0]) is not None,
    }
    presentation["display_text"] = render_person_presentation(presentation)
    return presentation


def _fetch_title_cache_source_detail(conn: duckdb.DuckDBPyConnection, tconst: str) -> dict[str, Any] | None:
    title = conn.execute(
        """
        SELECT
            tconst,
            title_type,
            primary_title,
            original_title,
            start_year,
            end_year,
            runtime_minutes,
            genres
        FROM app.catalog_titles
        WHERE tconst = ?
        """,
        [tconst],
    ).fetchone()
    if title is not None:
        title_detail: dict[str, Any] = {
            "tconst": title[0],
            "title_type": title[1],
            "primary_title": title[2],
            "original_title": title[3],
            "start_year": title[4],
            "end_year": title[5],
            "runtime_minutes": title[6],
            "genres": title[7] or [],
        }
    else:
        episode = conn.execute(
            """
            SELECT
                episode_tconst,
                primary_title,
                original_title,
                start_year,
                runtime_minutes
            FROM app.catalog_episodes
            WHERE episode_tconst = ?
            """,
            [tconst],
        ).fetchone()
        if episode is None:
            return None
        title_detail = {
            "tconst": episode[0],
            "kind": "episode",
            "primary_title": episode[1],
            "original_title": episode[2],
            "start_year": episode[3],
            "runtime_minutes": episode[4],
        }

    title_detail["aliases"] = _fetch_aliases(conn, tconst)
    title_detail["content_state"] = _fetch_content_state(conn, tconst)

    if title is not None:
        title_detail["library"] = _fetch_library_summary(conn, tconst, title[1])
    else:
        title_detail["library"] = _fetch_library_summary(conn, tconst, "tvEpisode")

    tmdb = _fetch_tmdb(conn, tconst)
    title_detail["tmdb"] = tmdb
    return title_detail


def _tmdb_asset_summary_signature(tmdb: dict[str, Any]) -> list[dict[str, Any]]:
    assets = tmdb.get("assets") or []
    return [
        {
            "kind": asset.get("asset_kind"),
            "path": asset.get("local_path") or asset.get("relative_path"),
            "status": asset.get("status"),
            "sha256": asset.get("sha256"),
        }
        for asset in assets
    ]


def _tmdb_detail_is_cache_ready(tmdb: dict[str, Any] | None) -> bool:
    if not tmdb:
        return False
    locales = set(tmdb.get("detail_locales") or [])
    if "en-US" not in locales or "cs-CZ" not in locales:
        return False

    details = tmdb.get("details") or {}
    fetched_assets = {
        asset.get("asset_kind")
        for asset in (tmdb.get("assets") or [])
        if asset.get("status") == "fetched"
    }
    if details.get("poster_path") and "poster" not in fetched_assets:
        return False
    if details.get("backdrop_path") and "backdrop" not in fetched_assets:
        return False
    return True


def _jsonify_for_cache(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonify_for_cache(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonify_for_cache(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonify_for_cache(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, set):
        return sorted(_jsonify_for_cache(item) for item in value)
    return value


def update_content_state(tconst: str, interest_state: str) -> dict[str, Any]:
    now = _now_iso()
    def write(conn: duckdb.DuckDBPyConnection) -> None:
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
    _run_duckdb_write(write)
    clear_title_presentation_cache()
    return {"tconst": tconst, "interest_state": interest_state, "updated_at": now}


def set_watchlist_state(
    tconst: str,
    *,
    in_watchlist: bool,
    notes: str | None = None,
) -> dict[str, Any]:
    """Add or remove a title or episode from the local watchlist."""
    detail = get_content_detail(tconst)
    if detail is None:
        raise ValueError("Titul nebyl nalezen.")

    now = _now_iso()
    media = _build_local_media_identity(detail)
    canonical_key = _canonical_media_key(
        media["media_type"],
        media["tconst"],
        media["imdb_id"],
        media["tmdb_id"],
        None,
        media["season_number"],
        media["episode_number"],
    )

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        _ensure_user_list(conn, "watchlist", "Watchlist", "watchlist", "local_app", "system:watchlist", now)
        if in_watchlist:
            _upsert_user_list_item(
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
    _run_duckdb_write(write)

    clear_title_presentation_cache()
    return {
        "tconst": tconst,
        "in_watchlist": in_watchlist,
        "updated_at": now,
        "library": _get_library_summary_for_tconst(tconst),
    }


def set_user_rating(tconst: str, rating: int) -> dict[str, Any]:
    """Create or update the local rating for a title or episode."""
    if rating < 1 or rating > 10:
        raise ValueError("Rating musí být mezi 1 a 10.")

    detail = get_content_detail(tconst)
    if detail is None:
        raise ValueError("Titul nebyl nalezen.")

    now = _now_iso()
    media = _build_local_media_identity(detail)
    canonical_key = _canonical_media_key(
        media["media_type"],
        media["tconst"],
        media["imdb_id"],
        media["tmdb_id"],
        None,
        media["season_number"],
        media["episode_number"],
    )

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        _upsert_user_rating(
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
    _run_duckdb_write(write)

    clear_title_presentation_cache()
    return {
        "tconst": tconst,
        "rating": rating,
        "rated_at": now,
        "library": _get_library_summary_for_tconst(tconst),
    }


def clear_user_rating(tconst: str) -> dict[str, Any]:
    """Delete the local rating for a title or episode."""
    detail = get_content_detail(tconst)
    if detail is None:
        raise ValueError("Titul nebyl nalezen.")

    media = _build_local_media_identity(detail)
    canonical_key = _canonical_media_key(
        media["media_type"],
        media["tconst"],
        media["imdb_id"],
        media["tmdb_id"],
        None,
        media["season_number"],
        media["episode_number"],
    )
    now = _now_iso()

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute("DELETE FROM app.user_ratings WHERE canonical_key = ?", [canonical_key])
    _run_duckdb_write(write)

    clear_title_presentation_cache()
    return {
        "tconst": tconst,
        "rating": None,
        "updated_at": now,
        "library": _get_library_summary_for_tconst(tconst),
    }


def get_favorite_genres(active_only: bool = True) -> list[dict[str, Any]]:
    """Return locally curated favorite genres ordered by preference and weight."""
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        return _get_favorite_genres(conn, active_only=active_only)


def get_catalog_genres() -> list[dict[str, Any]]:
    """Return all distinct catalog genres with how many titles use them."""
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        return _get_catalog_genres(conn)


def get_favorite_traits(active_only: bool = True) -> list[dict[str, Any]]:
    """Return locally curated favorite traits ordered by preference and weight."""
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT
                trait,
                weight,
                preference_rank,
                source_origin,
                source_ref,
                notes,
                is_active,
                created_at,
                updated_at
            FROM app.favorite_traits
            WHERE (? = FALSE OR is_active = TRUE)
            ORDER BY preference_rank ASC NULLS LAST, weight DESC, trait ASC
            """,
            [active_only],
        ).fetchall()
    return [
        {
            "trait": row[0],
            "weight": row[1],
            "preference_rank": row[2],
            "source_origin": row[3],
            "source_ref": row[4],
            "notes": row[5],
            "is_active": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }
        for row in rows
    ]


def get_genre_score_source_rows() -> list[dict[str, Any]]:
    """Return title-level behavioral inputs for genre scoring."""
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        return _get_genre_score_source_rows(conn)


def replace_favorite_genres(
    genres: Sequence[str | dict[str, Any]],
    *,
    source_origin: str = "local_app",
    source_ref: str | None = None,
    archive_missing: bool = True,
) -> dict[str, Any]:
    """Replace or refresh the curated favorite genre list."""
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(genres, start=1):
        if isinstance(item, str):
            genre = item.strip()
            payload = {
                "genre": genre,
                "weight": 1.0,
                "preference_rank": index,
                "notes": None,
                "is_active": True,
            }
        else:
            genre = str(item.get("genre") or "").strip()
            payload = {
                "genre": genre,
                "weight": float(item.get("weight", 1.0)),
                "preference_rank": item.get("preference_rank", index),
                "notes": item.get("notes"),
                "is_active": bool(item.get("is_active", True)),
            }
        if not genre:
            raise ValueError("Kazdy zanr musi mit neprazdny nazev.")
        normalized.append(payload)

    now = _now_iso()
    normalized_genres = {item["genre"] for item in normalized}

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        for item in normalized:
            conn.execute(
                """
                INSERT INTO app.favorite_genres (
                    genre, weight, preference_rank, source_origin, source_ref, notes, is_active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (genre) DO UPDATE SET
                    weight = excluded.weight,
                    preference_rank = excluded.preference_rank,
                    source_origin = excluded.source_origin,
                    source_ref = excluded.source_ref,
                    notes = excluded.notes,
                    is_active = excluded.is_active,
                    updated_at = excluded.updated_at
                """,
                [
                    item["genre"],
                    item["weight"],
                    item["preference_rank"],
                    source_origin,
                    source_ref,
                    item["notes"],
                    item["is_active"],
                    now,
                    now,
                ],
            )
        if archive_missing:
            existing_genres = {
                row[0]
                for row in conn.execute("SELECT genre FROM app.favorite_genres").fetchall()
            }
            for genre in sorted(existing_genres - normalized_genres):
                conn.execute(
                    "UPDATE app.favorite_genres SET is_active = FALSE, updated_at = ? WHERE genre = ?",
                    [now, genre],
                )

    _run_duckdb_write(write)
    return {
        "count": len(normalized),
        "genres": sorted(normalized_genres),
        "updated_at": now,
    }


def replace_favorite_traits(
    traits: Sequence[str | dict[str, Any]],
    *,
    source_origin: str = "local_app",
    source_ref: str | None = None,
    archive_missing: bool = True,
) -> dict[str, Any]:
    """Replace or refresh the curated favorite trait list."""
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(traits, start=1):
        if isinstance(item, str):
            trait = item.strip()
            payload = {
                "trait": trait,
                "weight": 1.0,
                "preference_rank": index,
                "notes": None,
                "is_active": True,
            }
        else:
            trait = str(item.get("trait") or "").strip()
            payload = {
                "trait": trait,
                "weight": float(item.get("weight", 1.0)),
                "preference_rank": item.get("preference_rank", index),
                "notes": item.get("notes"),
                "is_active": bool(item.get("is_active", True)),
            }
        if not trait:
            raise ValueError("Kazdy trait musi mit neprazdny nazev.")
        normalized.append(payload)

    now = _now_iso()
    normalized_traits = {item["trait"] for item in normalized}

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        for item in normalized:
            conn.execute(
                """
                INSERT INTO app.favorite_traits (
                    trait, weight, preference_rank, source_origin, source_ref, notes, is_active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (trait) DO UPDATE SET
                    weight = excluded.weight,
                    preference_rank = excluded.preference_rank,
                    source_origin = excluded.source_origin,
                    source_ref = excluded.source_ref,
                    notes = excluded.notes,
                    is_active = excluded.is_active,
                    updated_at = excluded.updated_at
                """,
                [
                    item["trait"],
                    item["weight"],
                    item["preference_rank"],
                    source_origin,
                    source_ref,
                    item["notes"],
                    item["is_active"],
                    now,
                    now,
                ],
            )
        if archive_missing:
            existing_traits = {
                row[0]
                for row in conn.execute("SELECT trait FROM app.favorite_traits").fetchall()
            }
            for trait in sorted(existing_traits - normalized_traits):
                conn.execute(
                    "UPDATE app.favorite_traits SET is_active = FALSE, updated_at = ? WHERE trait = ?",
                    [now, trait],
                )

    _run_duckdb_write(write)
    return {
        "count": len(normalized),
        "traits": sorted(normalized_traits),
        "updated_at": now,
    }


def record_genre_score_snapshot(
    scores: Sequence[dict[str, Any]],
    *,
    score_scope: str = "default",
    algorithm_version: str | None = None,
    source_origin: str = "local_app",
    source_ref: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Persist one genre-score snapshot run with per-genre score breakdown."""
    if not scores:
        raise ValueError("Je potreba dodat alespon jeden zanr se score.")

    snapshot_time = generated_at or _now_iso()
    datetime.fromisoformat(snapshot_time.replace("Z", "+00:00"))
    prepared_rows: list[dict[str, Any]] = []
    for index, item in enumerate(scores, start=1):
        genre = str(item.get("genre") or "").strip()
        if not genre:
            raise ValueError("Kazdy zaznam genre_scores musi mit genre.")
        if item.get("final_score") is None:
            raise ValueError(f"Zaznam pro zanr '{genre}' nema final_score.")
        prepared_rows.append(
            {
                "id": str(uuid.uuid4()),
                "genre": genre,
                "titles_considered": item.get("titles_considered"),
                "watched_titles_considered": item.get("watched_titles_considered"),
                "rated_titles_considered": item.get("rated_titles_considered"),
                "contributing_titles_json": _dumps_json_or_none(item.get("contributing_titles")),
                "excluded_titles_json": _dumps_json_or_none(item.get("excluded_titles")),
                "favorite_genre_weight": item.get("favorite_genre_weight"),
                "preference_overlap_score": item.get("preference_overlap_score"),
                "preference_alignment_score": item.get("preference_alignment_score"),
                "affinity_score": item.get("affinity_score"),
                "rating_signal_score": item.get("rating_signal_score"),
                "watch_signal_score": item.get("watch_signal_score"),
                "recency_score": item.get("recency_score"),
                "frequency_score": item.get("frequency_score"),
                "consistency_score": item.get("consistency_score"),
                "novelty_score": item.get("novelty_score"),
                "confidence_score": item.get("confidence_score"),
                "manual_adjustment_score": item.get("manual_adjustment_score"),
                "final_score": item.get("final_score"),
                "normalized_score": item.get("normalized_score"),
                "rank_in_run": item.get("rank_in_run", index),
                "metrics_json": _dumps_json_or_none(item.get("metrics")),
                "explanation": item.get("explanation"),
            }
        )

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        conn.executemany(
            """
            INSERT INTO app.genre_scores (
                id, genre, generated_at, algorithm_version, score_scope, source_origin, source_ref,
                titles_considered, watched_titles_considered, rated_titles_considered,
                contributing_titles_json, excluded_titles_json,
                favorite_genre_weight, preference_overlap_score, preference_alignment_score, affinity_score,
                rating_signal_score, watch_signal_score, recency_score, frequency_score, consistency_score,
                novelty_score, confidence_score, manual_adjustment_score, final_score, normalized_score,
                rank_in_run, metrics_json, explanation, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                [
                    item["id"],
                    item["genre"],
                    snapshot_time,
                    algorithm_version,
                    score_scope,
                    source_origin,
                    source_ref,
                    item["titles_considered"],
                    item["watched_titles_considered"],
                    item["rated_titles_considered"],
                    item["contributing_titles_json"],
                    item["excluded_titles_json"],
                    item["favorite_genre_weight"],
                    item["preference_overlap_score"],
                    item["preference_alignment_score"],
                    item["affinity_score"],
                    item["rating_signal_score"],
                    item["watch_signal_score"],
                    item["recency_score"],
                    item["frequency_score"],
                    item["consistency_score"],
                    item["novelty_score"],
                    item["confidence_score"],
                    item["manual_adjustment_score"],
                    item["final_score"],
                    item["normalized_score"],
                    item["rank_in_run"],
                    item["metrics_json"],
                    item["explanation"],
                    snapshot_time,
                ]
                for item in prepared_rows
            ],
        )

    _run_duckdb_write(write)
    return {
        "generated_at": snapshot_time,
        "score_scope": score_scope,
        "algorithm_version": algorithm_version,
        "count": len(prepared_rows),
    }


def compute_and_record_genre_scores(
    *,
    score_scope: str = "default",
    algorithm_version: str | None = None,
    source_origin: str = "local_app",
    source_ref: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Compute one genre-score snapshot from local data and store it."""
    snapshot_time = generated_at or _now_iso()
    result: dict[str, Any] = {}

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        title_rows = _get_genre_score_source_rows(conn)
        favorite_genres = _get_favorite_genres(conn, active_only=True)
        catalog_genres = _get_catalog_genres(conn)
        scores = compute_genre_scores(
            title_rows,
            favorite_genres,
            catalog_genres,
            generated_at=snapshot_time,
        )
        if not scores:
            raise ValueError("Pro vypocet genre_scores zatim nejsou zadna lokalni data.")

        resolved_algorithm_version = algorithm_version or (
            (scores[0].get("metrics") or {}).get("algorithm_version")
            if scores
            else None
        )
        summary = _record_genre_score_snapshot(
            conn,
            scores,
            score_scope=score_scope,
            algorithm_version=resolved_algorithm_version,
            source_origin=source_origin,
            source_ref=source_ref,
            generated_at=snapshot_time,
        )
        top_rows = _get_latest_genre_scores(conn, score_scope=score_scope, limit=10)
        result.update(
            {
                **summary,
                "titles_considered": len(title_rows),
                "favorite_genres_count": len(favorite_genres),
                "top_genres": top_rows["items"] if top_rows else [],
            }
        )

    _run_duckdb_write(write)
    return result


def get_latest_genre_scores(
    *,
    score_scope: str | None = None,
    limit: int | None = None,
) -> dict[str, Any] | None:
    """Load the newest genre-score snapshot, optionally within one scope."""
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        return _get_latest_genre_scores(conn, score_scope=score_scope, limit=limit)


def _get_favorite_genres(
    conn: duckdb.DuckDBPyConnection,
    *,
    active_only: bool,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            genre,
            weight,
            preference_rank,
            source_origin,
            source_ref,
            notes,
            is_active,
            created_at,
            updated_at
        FROM app.favorite_genres
        WHERE (? = FALSE OR is_active = TRUE)
        ORDER BY preference_rank ASC NULLS LAST, weight DESC, genre ASC
        """,
        [active_only],
    ).fetchall()
    return [
        {
            "genre": row[0],
            "weight": row[1],
            "preference_rank": row[2],
            "source_origin": row[3],
            "source_ref": row[4],
            "notes": row[5],
            "is_active": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }
        for row in rows
    ]


def _get_catalog_genres(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH exploded AS (
            SELECT
                trim(unnest(string_split(genres, ','))) AS genre
            FROM app.catalog_titles
            WHERE genres IS NOT NULL AND genres <> ''
        )
        SELECT genre, COUNT(*) AS title_count
        FROM exploded
        WHERE genre IS NOT NULL AND genre <> ''
        GROUP BY genre
        ORDER BY genre ASC
        """
    ).fetchall()
    return [
        {
            "genre": row[0],
            "title_count": row[1],
        }
        for row in rows
    ]


def _get_genre_score_source_rows(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest_title_ratings AS (
            SELECT
                tconst,
                rating,
                row_number() OVER (
                    PARTITION BY tconst
                    ORDER BY COALESCE(rated_at, updated_at, created_at) DESC, canonical_key
                ) AS rn
            FROM app.user_ratings
            WHERE tconst IS NOT NULL
        ),
        title_watch_events AS (
            SELECT
                w.tconst,
                COALESCE(w.created_at, CAST(w.watched_on AS TIMESTAMP)) AS watched_at
            FROM app.watch_events AS w
            WHERE w.tconst IN (SELECT tconst FROM app.catalog_titles)

            UNION ALL

            SELECT
                e.series_tconst AS tconst,
                COALESCE(w.created_at, CAST(w.watched_on AS TIMESTAMP)) AS watched_at
            FROM app.watch_events AS w
            JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
            WHERE e.series_tconst IS NOT NULL
        ),
        title_watch_stats AS (
            SELECT
                tconst,
                COUNT(*) AS watch_count,
                MAX(watched_at) AS last_watched_at
            FROM title_watch_events
            GROUP BY tconst
        )
        SELECT
            t.tconst,
            t.primary_title,
            t.start_year,
            t.genres,
            r.rating,
            w.watch_count,
            w.last_watched_at
        FROM app.catalog_titles AS t
        LEFT JOIN latest_title_ratings AS r ON r.tconst = t.tconst AND r.rn = 1
        LEFT JOIN title_watch_stats AS w ON w.tconst = t.tconst
        WHERE t.genres IS NOT NULL
          AND t.genres <> ''
          AND (r.rating IS NOT NULL OR w.watch_count IS NOT NULL)
        ORDER BY t.primary_title ASC
        """
    ).fetchall()
    return [
        {
            "tconst": row[0],
            "title": row[1],
            "year": row[2],
            "genres": [part.strip() for part in (row[3] or "").split(",") if part.strip()],
            "rating": row[4],
            "watch_count": row[5] or 0,
            "last_watched_at": row[6],
        }
        for row in rows
    ]


def _record_genre_score_snapshot(
    conn: duckdb.DuckDBPyConnection,
    scores: Sequence[dict[str, Any]],
    *,
    score_scope: str,
    algorithm_version: str | None,
    source_origin: str,
    source_ref: str | None,
    generated_at: str,
) -> dict[str, Any]:
    if not scores:
        raise ValueError("Je potreba dodat alespon jeden zanr se score.")

    datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    prepared_rows: list[dict[str, Any]] = []
    for index, item in enumerate(scores, start=1):
        genre = str(item.get("genre") or "").strip()
        if not genre:
            raise ValueError("Kazdy zaznam genre_scores musi mit genre.")
        if item.get("final_score") is None:
            raise ValueError(f"Zaznam pro zanr '{genre}' nema final_score.")
        prepared_rows.append(
            {
                "id": str(uuid.uuid4()),
                "genre": genre,
                "titles_considered": item.get("titles_considered"),
                "watched_titles_considered": item.get("watched_titles_considered"),
                "rated_titles_considered": item.get("rated_titles_considered"),
                "contributing_titles_json": _dumps_json_or_none(item.get("contributing_titles")),
                "excluded_titles_json": _dumps_json_or_none(item.get("excluded_titles")),
                "favorite_genre_weight": item.get("favorite_genre_weight"),
                "preference_overlap_score": item.get("preference_overlap_score"),
                "preference_alignment_score": item.get("preference_alignment_score"),
                "affinity_score": item.get("affinity_score"),
                "rating_signal_score": item.get("rating_signal_score"),
                "watch_signal_score": item.get("watch_signal_score"),
                "recency_score": item.get("recency_score"),
                "frequency_score": item.get("frequency_score"),
                "consistency_score": item.get("consistency_score"),
                "novelty_score": item.get("novelty_score"),
                "confidence_score": item.get("confidence_score"),
                "manual_adjustment_score": item.get("manual_adjustment_score"),
                "final_score": item.get("final_score"),
                "normalized_score": item.get("normalized_score"),
                "rank_in_run": item.get("rank_in_run", index),
                "metrics_json": _dumps_json_or_none(item.get("metrics")),
                "explanation": item.get("explanation"),
            }
        )

    conn.executemany(
        """
        INSERT INTO app.genre_scores (
            id, genre, generated_at, algorithm_version, score_scope, source_origin, source_ref,
            titles_considered, watched_titles_considered, rated_titles_considered,
            contributing_titles_json, excluded_titles_json,
            favorite_genre_weight, preference_overlap_score, preference_alignment_score, affinity_score,
            rating_signal_score, watch_signal_score, recency_score, frequency_score, consistency_score,
            novelty_score, confidence_score, manual_adjustment_score, final_score, normalized_score,
            rank_in_run, metrics_json, explanation, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            [
                item["id"],
                item["genre"],
                generated_at,
                algorithm_version,
                score_scope,
                source_origin,
                source_ref,
                item["titles_considered"],
                item["watched_titles_considered"],
                item["rated_titles_considered"],
                item["contributing_titles_json"],
                item["excluded_titles_json"],
                item["favorite_genre_weight"],
                item["preference_overlap_score"],
                item["preference_alignment_score"],
                item["affinity_score"],
                item["rating_signal_score"],
                item["watch_signal_score"],
                item["recency_score"],
                item["frequency_score"],
                item["consistency_score"],
                item["novelty_score"],
                item["confidence_score"],
                item["manual_adjustment_score"],
                item["final_score"],
                item["normalized_score"],
                item["rank_in_run"],
                item["metrics_json"],
                item["explanation"],
                generated_at,
            ]
            for item in prepared_rows
        ],
    )
    return {
        "generated_at": generated_at,
        "score_scope": score_scope,
        "algorithm_version": algorithm_version,
        "count": len(prepared_rows),
    }


def _get_latest_genre_scores(
    conn: duckdb.DuckDBPyConnection,
    *,
    score_scope: str | None,
    limit: int | None,
) -> dict[str, Any] | None:
    latest_row = conn.execute(
        """
        SELECT generated_at, score_scope
        FROM app.genre_scores
        WHERE (? IS NULL OR score_scope = ?)
        ORDER BY generated_at DESC, score_scope ASC
        LIMIT 1
        """,
        [score_scope, score_scope],
    ).fetchone()
    if latest_row is None:
        return None

    generated_at = latest_row[0]
    resolved_scope = latest_row[1]
    sql = """
        SELECT
            id,
            genre,
            generated_at,
            algorithm_version,
            score_scope,
            source_origin,
            source_ref,
            titles_considered,
            watched_titles_considered,
            rated_titles_considered,
            contributing_titles_json,
            excluded_titles_json,
            favorite_genre_weight,
            preference_overlap_score,
            preference_alignment_score,
            affinity_score,
            rating_signal_score,
            watch_signal_score,
            recency_score,
            frequency_score,
            consistency_score,
            novelty_score,
            confidence_score,
            manual_adjustment_score,
            final_score,
            normalized_score,
            rank_in_run,
            metrics_json,
            explanation,
            created_at
        FROM app.genre_scores
        WHERE generated_at = ? AND score_scope = ?
        ORDER BY rank_in_run ASC NULLS LAST, final_score DESC, genre ASC
    """
    params: list[Any] = [generated_at, resolved_scope]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    items = [
        {
            "id": row[0],
            "genre": row[1],
            "generated_at": row[2],
            "algorithm_version": row[3],
            "score_scope": row[4],
            "source_origin": row[5],
            "source_ref": row[6],
            "titles_considered": row[7],
            "watched_titles_considered": row[8],
            "rated_titles_considered": row[9],
            "contributing_titles": _loads_json_or_none(row[10]),
            "excluded_titles": _loads_json_or_none(row[11]),
            "favorite_genre_weight": row[12],
            "preference_overlap_score": row[13],
            "preference_alignment_score": row[14],
            "affinity_score": row[15],
            "rating_signal_score": row[16],
            "watch_signal_score": row[17],
            "recency_score": row[18],
            "frequency_score": row[19],
            "consistency_score": row[20],
            "novelty_score": row[21],
            "confidence_score": row[22],
            "manual_adjustment_score": row[23],
            "final_score": row[24],
            "normalized_score": row[25],
            "rank_in_run": row[26],
            "metrics": _loads_json_or_none(row[27]),
            "explanation": row[28],
            "created_at": row[29],
        }
        for row in rows
    ]
    return {
        "generated_at": generated_at,
        "score_scope": resolved_scope,
        "count": len(items),
        "items": items,
    }


def record_watch_event(
    tconst: str,
    *,
    watched_on: str | None = None,
    notes: str | None = None,
    add_to_watched_list: bool = False,
) -> dict[str, Any]:
    """Append a local watch event and mark the content as watched."""
    detail = get_content_detail(tconst)
    if detail is None:
        raise ValueError("Titul nebyl nalezen.")

    now = _now_iso()
    event_id = str(uuid.uuid4())
    event_scope = "episode" if detail["kind"] == "episode" else "title"
    effective_watched_on = watched_on or now[:10]
    try:
        datetime.strptime(effective_watched_on, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("watched_on musí být ISO datum ve formátu YYYY-MM-DD.") from exc

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            """
            INSERT INTO app.watch_events (
                id,
                tconst,
                event_scope,
                watched_on,
                source,
                batch_id,
                import_row_id,
                rating,
                notes,
                created_at
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
    _run_duckdb_write(write)

    clear_title_presentation_cache()
    return {
        "id": event_id,
        "tconst": tconst,
        "event_scope": event_scope,
        "watched_on": effective_watched_on,
        "created_at": now,
        "library": _get_library_summary_for_tconst(tconst),
    }


def record_watch_events_through_episode(
    episode_tconst: str,
    *,
    watched_on: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Mark all missing episodes up to and including the target episode as watched."""
    detail = get_content_detail(episode_tconst)
    if detail is None or detail.get("kind") != "episode":
        raise ValueError("Epizoda nebyla nalezena.")

    series_tconst = detail.get("series_tconst")
    season_number = detail.get("season_number")
    episode_number = detail.get("episode_number")
    if not series_tconst or season_number is None or episode_number is None:
        raise ValueError("Epizoda nema uplny serialovy kontext.")

    now = _now_iso()
    effective_watched_on = watched_on or now[:10]
    try:
        datetime.strptime(effective_watched_on, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("watched_on musi byt ISO datum ve formatu YYYY-MM-DD.") from exc

    def write(conn: duckdb.DuckDBPyConnection) -> list[str]:
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
                id,
                tconst,
                event_scope,
                watched_on,
                source,
                batch_id,
                import_row_id,
                rating,
                notes,
                created_at
            )
            VALUES (?, ?, 'episode', ?, 'local_app', NULL, NULL, NULL, ?, ?)
            """,
            [[str(uuid.uuid4()), tconst, effective_watched_on, notes, now] for tconst in watched_ids],
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

    watched_ids = _run_duckdb_write(write)

    clear_title_presentation_cache()
    return {
        "series_tconst": series_tconst,
        "target_episode_tconst": episode_tconst,
        "watched_on": effective_watched_on,
        "watched_count": len(watched_ids),
        "watched_tconsts": watched_ids,
        "library": _get_library_summary_for_tconst(series_tconst),
    }


def delete_group_from_user_list(list_id: str, display_tconst: str) -> dict[str, Any]:
    now = _now_iso()
    result: dict[str, Any] = {"affected_rows": 0}

    def write(conn: duckdb.DuckDBPyConnection) -> None:
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

    _run_duckdb_write(write)
    clear_title_presentation_cache()
    return {
        "list_id": list_id,
        "display_tconst": display_tconst,
        "updated_at": now,
        "affected_rows": int(result["affected_rows"]),
    }


def move_group_between_user_lists(source_list_id: str, target_list_id: str, display_tconst: str) -> dict[str, Any]:
    if source_list_id == target_list_id:
        raise ValueError("Zdrojový a cílový seznam jsou stejné.")

    now = _now_iso()
    result: dict[str, Any] = {"moved_rows": 0}

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        source_list = conn.execute(
            "SELECT id, name, list_kind FROM app.user_lists WHERE id = ?",
            [source_list_id],
        ).fetchone()
        if source_list is None:
            raise ValueError("Zdrojový seznam nebyl nalezen.")

        target_list = conn.execute(
            "SELECT id, name, list_kind FROM app.user_lists WHERE id = ?",
            [target_list_id],
        ).fetchone()
        if target_list is None:
            raise ValueError("Cílový seznam nebyl nalezen.")

        rows = conn.execute(
            """
            SELECT
                i.canonical_key,
                i.tconst,
                i.media_type,
                i.imdb_id,
                i.tmdb_id,
                i.trakt_id,
                i.parent_tconst,
                i.parent_title,
                i.title,
                i.season_number,
                i.episode_number,
                i.rank,
                i.added_at,
                i.notes,
                i.source_origin,
                i.source_ref
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
            _upsert_user_list_item(
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

    _run_duckdb_write(write)
    clear_title_presentation_cache()
    return {
        "source_list_id": source_list_id,
        "target_list_id": target_list_id,
        "display_tconst": display_tconst,
        "moved_rows": int(result["moved_rows"]),
        "updated_at": now,
    }


def copy_group_to_user_list(source_list_id: str, target_list_id: str, display_tconst: str) -> dict[str, Any]:
    if source_list_id == target_list_id:
        raise ValueError("Zdrojový a cílový seznam jsou stejné.")

    now = _now_iso()
    result: dict[str, Any] = {"copied_rows": 0}

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        source_list = conn.execute(
            "SELECT id, name, list_kind FROM app.user_lists WHERE id = ?",
            [source_list_id],
        ).fetchone()
        if source_list is None:
            raise ValueError("Zdrojový seznam nebyl nalezen.")

        target_list = conn.execute(
            "SELECT id, name, list_kind FROM app.user_lists WHERE id = ?",
            [target_list_id],
        ).fetchone()
        if target_list is None:
            raise ValueError("Cílový seznam nebyl nalezen.")

        rows = conn.execute(
            """
            SELECT
                i.canonical_key,
                i.tconst,
                i.media_type,
                i.imdb_id,
                i.tmdb_id,
                i.trakt_id,
                i.parent_tconst,
                i.parent_title,
                i.title,
                i.season_number,
                i.episode_number,
                i.rank,
                i.added_at,
                i.notes,
                i.source_origin,
                i.source_ref
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
            _upsert_user_list_item(
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

    _run_duckdb_write(write)
    clear_title_presentation_cache()
    return {
        "source_list_id": source_list_id,
        "target_list_id": target_list_id,
        "display_tconst": display_tconst,
        "copied_rows": int(result["copied_rows"]),
        "updated_at": now,
    }


def create_user_list(name: str, description: str | None = None) -> dict[str, Any]:
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValueError("Název seznamu nesmí být prázdný.")
    cleaned_description = (description or "").strip() or None

    now = _now_iso()
    result: dict[str, Any] = {}

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        list_id = f"custom-list-{uuid.uuid4()}"
        slug_base = _slugify(cleaned_name) or "list"
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
        result.update(
            {
                "id": list_id,
                "slug": slug,
                "name": cleaned_name,
                "description": cleaned_description,
                "list_kind": "custom",
                "created_at": now,
            }
        )

    _run_duckdb_write(write)
    return result


def update_user_list_description(list_id: str, description: str | None = None) -> dict[str, Any]:
    cleaned_description = (description or "").strip() or None
    now = _now_iso()
    result: dict[str, Any] = {}

    def write(conn: duckdb.DuckDBPyConnection) -> None:
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
        result.update(
            {
                "id": row[0],
                "slug": row[1],
                "name": row[2],
                "description": cleaned_description,
                "list_kind": row[3],
                "updated_at": now,
            }
        )

    _run_duckdb_write(write)
    return result


def _pick_best_title_match(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    fuzzy_matches = [candidate for candidate in candidates if candidate.get("fuzzy_score") is not None]
    if fuzzy_matches:
        fuzzy_matches.sort(
            key=lambda item: (
                item.get("fuzzy_score") or 0.0,
                -int(item.get("alias_priority") or 99),
                item.get("num_votes") or 0,
                item.get("start_year") or 0,
            ),
            reverse=True,
        )
        strongest = fuzzy_matches[0]
        if (strongest.get("fuzzy_score") or 0.0) >= 0.72:
            strongest_score = strongest.get("fuzzy_score") or 0.0
            near_top = [
                item
                for item in fuzzy_matches
                if (item.get("fuzzy_score") or 0.0) >= strongest_score - 0.08
            ]
            if len(near_top) > 1:
                near_top.sort(
                    key=lambda item: (
                        item.get("num_votes") or 0,
                        item.get("start_year") or 0,
                        item.get("fuzzy_score") or 0.0,
                    ),
                    reverse=True,
                )
                return near_top[0]
            return strongest

    query_key = _normalize_match_key(query)
    query_key_articleless = _normalize_match_key(query, strip_leading_articles=True)
    exact_matches: list[dict[str, Any]] = []
    for candidate in candidates:
        if _normalize_match_key(candidate.get("primary_title")) == query_key:
            exact_matches.append(candidate)
            continue
        if _normalize_match_key(candidate.get("original_title")) == query_key:
            exact_matches.append(candidate)
            continue
        if _normalize_match_key(candidate.get("matched_alias_title")) == query_key:
            exact_matches.append(candidate)
            continue
        if _normalize_match_key(candidate.get("primary_title"), strip_leading_articles=True) == query_key_articleless:
            exact_matches.append(candidate)
            continue
        if _normalize_match_key(candidate.get("original_title"), strip_leading_articles=True) == query_key_articleless:
            exact_matches.append(candidate)
            continue
        if _normalize_match_key(candidate.get("matched_alias_title"), strip_leading_articles=True) == query_key_articleless:
            exact_matches.append(candidate)
    if exact_matches:
        exact_matches.sort(
            key=lambda item: (
                -int(item.get("alias_priority") or 99),
                item.get("num_votes") or 0,
                item.get("start_year") or 0,
            ),
            reverse=True,
        )
        return exact_matches[0]
    return candidates[0]


def _build_title_lookup_result(
    *,
    query: str,
    title_type: str | None,
    selected: dict[str, Any],
    candidates: list[dict[str, Any]],
    candidates_limit: int,
) -> dict[str, Any] | None:
    source_presentation = get_title_presentation(selected["tconst"])
    if source_presentation is None:
        return None
    presentation = dict(source_presentation)
    presentation["query"] = query
    presentation["match"] = _build_lookup_candidate(selected, query=query, is_selected=True)

    selected_key = selected["tconst"]
    ordered_candidates = sorted(
        candidates,
        key=lambda item: (
            0 if item["tconst"] == selected_key else 1,
            -(item.get("fuzzy_score") or 0.0),
            int(item.get("alias_priority") or 99),
            -(item.get("start_year") or 0),
            item["primary_title"],
        ),
    )
    return {
        "query": query,
        "title_type": title_type,
        "selected_tconst": selected_key,
        "selected": presentation,
        "candidates": [
            _build_lookup_candidate(candidate, query=query, is_selected=(candidate["tconst"] == selected_key))
            for candidate in ordered_candidates[: max(candidates_limit, 1)]
        ],
        "candidate_count": len(candidates),
    }


def _pick_best_person_match(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    fuzzy_matches = [candidate for candidate in candidates if candidate.get("fuzzy_score") is not None]
    if fuzzy_matches:
        fuzzy_matches.sort(
            key=lambda item: (
                item.get("fuzzy_score") or 0.0,
                item.get("credit_count") or 0,
                item.get("birth_year") or 0,
            ),
            reverse=True,
        )
        strongest = fuzzy_matches[0]
        if (strongest.get("fuzzy_score") or 0.0) >= 0.72:
            strongest_score = strongest.get("fuzzy_score") or 0.0
            near_top = [
                item
                for item in fuzzy_matches
                if (item.get("fuzzy_score") or 0.0) >= strongest_score - 0.08
            ]
            if len(near_top) > 1:
                near_top.sort(
                    key=lambda item: (
                        item.get("credit_count") or 0,
                        item.get("birth_year") or 0,
                        item.get("fuzzy_score") or 0.0,
                    ),
                    reverse=True,
                )
                return near_top[0]
            return strongest

    query_key = _normalize_match_key(query)
    exact_matches: list[dict[str, Any]] = []
    for candidate in candidates:
        if _normalize_match_key(candidate.get("primary_name")) == query_key:
            exact_matches.append(candidate)
    if exact_matches:
        exact_matches.sort(
            key=lambda item: (
                item.get("credit_count") or 0,
                item.get("birth_year") or 0,
            ),
            reverse=True,
        )
        return exact_matches[0]
    return candidates[0]


def _build_lookup_candidate(candidate: dict[str, Any], *, query: str, is_selected: bool) -> dict[str, Any]:
    return {
        "tconst": candidate["tconst"],
        "primary_title": candidate["primary_title"],
        "original_title": candidate["original_title"],
        "title_type": candidate["title_type"],
        "kind_label": _title_type_label(candidate.get("title_type")),
        "start_year": candidate["start_year"],
        "runtime_minutes": candidate.get("runtime_minutes"),
        "genres": candidate.get("genres") or [],
        "average_rating": candidate.get("average_rating"),
        "num_votes": candidate.get("num_votes"),
        "library": candidate.get("library") or {},
        "is_selected": is_selected,
        "is_exact_match": (
            _normalize_match_key(candidate.get("primary_title")) == _normalize_match_key(query)
            or _normalize_match_key(candidate.get("original_title")) == _normalize_match_key(query)
            or _normalize_match_key(candidate.get("matched_alias_title")) == _normalize_match_key(query)
            or _normalize_match_key(candidate.get("primary_title"), strip_leading_articles=True)
            == _normalize_match_key(query, strip_leading_articles=True)
            or _normalize_match_key(candidate.get("original_title"), strip_leading_articles=True)
            == _normalize_match_key(query, strip_leading_articles=True)
            or _normalize_match_key(candidate.get("matched_alias_title"), strip_leading_articles=True)
            == _normalize_match_key(query, strip_leading_articles=True)
        ),
        "fuzzy_score": candidate.get("fuzzy_score"),
        "matched_alias_title": candidate.get("matched_alias_title"),
    }


def _build_person_lookup_candidate(candidate: dict[str, Any], *, query: str, is_selected: bool) -> dict[str, Any]:
    return {
        "nconst": candidate["nconst"],
        "primary_name": candidate["primary_name"],
        "birth_year": candidate.get("birth_year"),
        "death_year": candidate.get("death_year"),
        "primary_profession": candidate.get("primary_profession"),
        "known_for_titles": candidate.get("known_for_titles"),
        "filmography": candidate.get("filmography") or {},
        "credit_count": candidate.get("credit_count") or 0,
        "is_selected": is_selected,
        "is_exact_match": _normalize_match_key(candidate.get("primary_name")) == _normalize_match_key(query),
        "fuzzy_score": candidate.get("fuzzy_score"),
    }


def _is_confident_person_lookup(query: str, candidate: dict[str, Any]) -> bool:
    if _normalize_match_key(candidate.get("primary_name")) == _normalize_match_key(query):
        return True
    return (candidate.get("fuzzy_score") or 0.0) >= 0.82


def _should_expand_people_to_fuzzy(query: str, candidates: list[dict[str, Any]]) -> bool:
    query_key = _normalize_match_key(query)
    if not query_key or not candidates:
        return True
    for candidate in candidates[:3]:
        if _normalize_match_key(candidate.get("primary_name")) == query_key:
            return False

    best_direct_score = max(
        _best_person_name_similarity(query_key, candidate.get("primary_name")) for candidate in candidates[:5]
    )
    return best_direct_score < 0.72


def _person_lookup_item_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "nconst": row[0],
        "primary_name": row[1],
        "birth_year": row[2],
        "death_year": row[3],
        "primary_profession": row[4],
        "known_for_titles": row[5],
        "credit_count": row[6] or 0,
    }


def _search_people_for_lookup(query: str, limit: int) -> list[dict[str, Any]]:
    sql = """
        WITH credit_counts AS (
            SELECT nconst, COUNT(*) AS credit_count
            FROM app.title_credits
            GROUP BY nconst
        )
        SELECT
            p.nconst,
            p.primary_name,
            p.birth_year,
            p.death_year,
            p.primary_profession,
            p.known_for_titles,
            COALESCE(c.credit_count, 0) AS credit_count
        FROM app.catalog_people AS p
        LEFT JOIN credit_counts AS c USING (nconst)
        WHERE p.primary_name ILIKE '%' || ? || '%'
        ORDER BY
            CASE WHEN lower(p.primary_name) = lower(?) THEN 0 ELSE 1 END,
            COALESCE(c.credit_count, 0) DESC,
            p.birth_year DESC NULLS LAST,
            p.primary_name
        LIMIT ?
    """
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(sql, [query, query, limit]).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _person_lookup_item_from_row(row)
            item["fuzzy_score"] = _best_person_name_similarity(_normalize_match_key(query), item["primary_name"])
            items.append(item)
    return items


def _search_people_for_lookup_fuzzy(query: str, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query)
    if len(query_key) < 3:
        return []
    prefix3 = query_key[:3]
    prefix2 = query_key[:2]
    length_floor = max(len(query_key) - 2, 1)
    length_ceiling = len(query_key) + 3
    sql = f"""
        WITH credit_counts AS (
            SELECT nconst, COUNT(*) AS credit_count
            FROM app.title_credits
            GROUP BY nconst
        ),
        people_keys AS (
            SELECT
                p.nconst,
                p.primary_name,
                p.birth_year,
                p.death_year,
                p.primary_profession,
                p.known_for_titles,
                COALESCE(c.credit_count, 0) AS credit_count,
                {_duckdb_match_key_sql("p.primary_name")} AS name_key,
                regexp_extract({_duckdb_match_key_sql("p.primary_name")}, '^([a-z0-9]+)', 1) AS first_token_key,
                regexp_extract({_duckdb_match_key_sql("p.primary_name")}, '([a-z0-9]+)$', 1) AS last_token_key,
                replace({_duckdb_match_key_sql("p.primary_name")}, ' ', '') AS compact_name_key
            FROM app.catalog_people AS p
            LEFT JOIN credit_counts AS c USING (nconst)
        )
        SELECT
            nconst,
            primary_name,
            birth_year,
            death_year,
            primary_profession,
            known_for_titles,
            credit_count
        FROM people_keys
        WHERE (
            left(name_key, 3) = ?
            OR left(first_token_key, 3) = ?
            OR left(last_token_key, 3) = ?
            OR left(compact_name_key, 3) = ?
            OR left(name_key, 2) = ?
            OR left(first_token_key, 2) = ?
            OR left(last_token_key, 2) = ?
            OR left(compact_name_key, 2) = ?
        )
          AND (
            length(name_key) BETWEEN ? AND ?
            OR length(last_token_key) BETWEEN ? AND ?
            OR length(compact_name_key) BETWEEN ? AND ?
          )
        ORDER BY credit_count DESC, birth_year DESC NULLS LAST, primary_name
        LIMIT 500
    """
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            sql,
            [
                prefix3,
                prefix3,
                prefix3,
                prefix3,
                prefix2,
                prefix2,
                prefix2,
                prefix2,
                length_floor,
                length_ceiling,
                length_floor,
                length_ceiling,
                length_floor,
                length_ceiling,
            ],
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _person_lookup_item_from_row(row)
            item["fuzzy_score"] = _best_person_name_similarity(query_key, item["primary_name"])
            items.append(item)
    items.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            item.get("credit_count") or 0,
            item.get("birth_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in items if (item.get("fuzzy_score") or 0.0) >= 0.65][:limit]


def _search_people_for_lookup_levenshtein(query: str, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query)
    if len(query_key) < 4:
        return []
    first_letter = query_key[0]
    query_len = len(query_key)
    length_floor = max(query_len - 4, 1)
    length_ceiling = query_len + 4
    sql = f"""
        WITH credit_counts AS (
            SELECT nconst, COUNT(*) AS credit_count
            FROM app.title_credits
            GROUP BY nconst
        ),
        people_keys AS (
            SELECT
                p.nconst,
                p.primary_name,
                p.birth_year,
                p.death_year,
                p.primary_profession,
                p.known_for_titles,
                COALESCE(c.credit_count, 0) AS credit_count,
                {_duckdb_match_key_sql("p.primary_name")} AS name_key,
                regexp_extract({_duckdb_match_key_sql("p.primary_name")}, '^([a-z0-9]+)', 1) AS first_token_key,
                regexp_extract({_duckdb_match_key_sql("p.primary_name")}, '([a-z0-9]+)$', 1) AS last_token_key,
                replace({_duckdb_match_key_sql("p.primary_name")}, ' ', '') AS compact_name_key
            FROM app.catalog_people AS p
            LEFT JOIN credit_counts AS c USING (nconst)
        )
        SELECT
            nconst,
            primary_name,
            birth_year,
            death_year,
            primary_profession,
            known_for_titles,
            credit_count,
            least(
                levenshtein(?, name_key),
                levenshtein(?, last_token_key),
                levenshtein(?, compact_name_key)
            ) AS edit_distance
        FROM people_keys
        WHERE (
            left(name_key, 1) = ?
            OR left(first_token_key, 1) = ?
            OR left(last_token_key, 1) = ?
            OR left(compact_name_key, 1) = ?
        )
          AND (
            length(name_key) BETWEEN ? AND ?
            OR length(last_token_key) BETWEEN ? AND ?
            OR length(compact_name_key) BETWEEN ? AND ?
          )
        ORDER BY edit_distance ASC, credit_count DESC, birth_year DESC NULLS LAST, primary_name
        LIMIT 500
    """
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            sql,
            [
                query_key,
                query_key,
                query_key,
                first_letter,
                first_letter,
                first_letter,
                first_letter,
                length_floor,
                length_ceiling,
                length_floor,
                length_ceiling,
                length_floor,
                length_ceiling,
            ],
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _person_lookup_item_from_row(row[:7])
            item["fuzzy_score"] = _best_person_name_similarity(query_key, item["primary_name"])
            items.append(item)
    items.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            item.get("credit_count") or 0,
            item.get("birth_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in items if (item.get("fuzzy_score") or 0.0) >= 0.65][:limit]


def _fetch_person_filmography_summary(conn: duckdb.DuckDBPyConnection, nconst: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            c.credit_group,
            t.primary_title,
            t.start_year,
            t.title_type
        FROM app.title_credits AS c
        JOIN app.catalog_titles AS t ON t.tconst = c.tconst
        WHERE c.nconst = ?
        ORDER BY t.start_year DESC NULLS LAST, t.primary_title
        LIMIT 50
        """,
        [nconst],
    ).fetchall()
    grouped = {"director": [], "creator": [], "writer": [], "cast": [], "principal": []}
    for row in rows:
        grouped.setdefault(row[0], []).append(
            {
                "title": row[1],
                "start_year": row[2],
                "title_type": row[3],
            }
        )
    return {
        "credit_count": len(rows),
        "director": grouped["director"][:20],
        "creator": grouped["creator"][:20],
        "writer": grouped["writer"][:20],
        "cast": grouped["cast"][:20],
        "principal": grouped["principal"][:20],
    }


def _is_confident_lookup(query: str, candidate: dict[str, Any]) -> bool:
    if _normalize_match_key(candidate.get("primary_title")) == _normalize_match_key(query):
        return True
    if _normalize_match_key(candidate.get("original_title")) == _normalize_match_key(query):
        return True
    if _normalize_match_key(candidate.get("matched_alias_title")) == _normalize_match_key(query):
        return True
    if _normalize_match_key(candidate.get("primary_title"), strip_leading_articles=True) == _normalize_match_key(
        query, strip_leading_articles=True
    ):
        return True
    if _normalize_match_key(candidate.get("original_title"), strip_leading_articles=True) == _normalize_match_key(
        query, strip_leading_articles=True
    ):
        return True
    if _normalize_match_key(candidate.get("matched_alias_title"), strip_leading_articles=True) == _normalize_match_key(
        query, strip_leading_articles=True
    ):
        return True
    return (candidate.get("fuzzy_score") or 0.0) >= 0.82


def _is_direct_enough_lookup(query: str, candidate: dict[str, Any]) -> bool:
    query_key = _normalize_match_key(query)
    query_key_articleless = _normalize_match_key(query, strip_leading_articles=True)
    if not query_key:
        return False

    for variant in [
        candidate.get("primary_title"),
        candidate.get("original_title"),
        candidate.get("matched_alias_title"),
    ]:
        if _normalize_match_key(variant) == query_key:
            return True
        if _normalize_match_key(variant, strip_leading_articles=True) == query_key_articleless:
            return True
    return False


def _should_expand_to_fuzzy(query: str, candidates: list[dict[str, Any]]) -> bool:
    query_key = _normalize_match_key(query)
    query_key_articleless = _normalize_match_key(query, strip_leading_articles=True)
    if not query_key or not candidates:
        return True
    for candidate in candidates[:3]:
        if _normalize_match_key(candidate.get("primary_title")) == query_key:
            return False
        if _normalize_match_key(candidate.get("original_title")) == query_key:
            return False
        if _normalize_match_key(candidate.get("matched_alias_title")) == query_key:
            return False
        if _normalize_match_key(candidate.get("primary_title"), strip_leading_articles=True) == query_key_articleless:
            return False
        if _normalize_match_key(candidate.get("original_title"), strip_leading_articles=True) == query_key_articleless:
            return False
        if _normalize_match_key(candidate.get("matched_alias_title"), strip_leading_articles=True) == query_key_articleless:
            return False

    best_direct_score = max(
        _best_title_similarity(
            query_key,
            [candidate.get("primary_title"), candidate.get("original_title"), candidate.get("matched_alias_title")],
        )
        for candidate in candidates[:5]
    )
    return best_direct_score < 0.72


def _merge_lookup_candidates(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {_lookup_identity_key(item): item for item in primary}
    for item in secondary:
        identity = _lookup_identity_key(item)
        existing = merged.get(identity)
        if existing is None:
            merged[identity] = item
            continue
        existing_score = existing.get("fuzzy_score") or 0.0
        new_score = item.get("fuzzy_score") or 0.0
        if new_score > existing_score:
            combined = {**existing, **item}
            merged[identity] = combined
    return list(merged.values())


def _lookup_identity_key(item: dict[str, Any]) -> str:
    return str(item.get("tconst") or item.get("nconst") or "")


def _alias_priority_case_sql(region_column: str, language_column: str) -> str:
    return f"""
        CASE
            WHEN lower(coalesce({language_column}, '')) = 'cs' OR upper(coalesce({region_column}, '')) = 'CZ' THEN 0
            WHEN lower(coalesce({language_column}, '')) = 'en'
                 OR upper(coalesce({region_column}, '')) IN ('US', 'GB', 'CA', 'IE', 'AU', 'NZ', 'IN') THEN 1
            ELSE 2
        END
    """


def _catalog_row_from_alias_row(row: tuple[Any, ...]) -> dict[str, Any]:
    item = _catalog_row_to_dict(row[:9])
    item["matched_alias_title"] = row[9]
    item["alias_region"] = row[10]
    item["alias_language"] = row[11]
    item["alias_priority"] = row[12]
    return item


def _search_catalog_aliases_for_lookup(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query)
    query_key_articleless = _normalize_match_key(query, strip_leading_articles=True)
    if not query_key:
        return []
    sql = f"""
        SELECT
            t.tconst,
            t.title_type,
            t.primary_title,
            t.original_title,
            t.start_year,
            t.runtime_minutes,
            t.genres,
            t.average_rating,
            t.num_votes,
            a.title AS matched_alias_title,
            a.region,
            a.language,
            a.alias_priority
        FROM app.title_alias_lookup AS a
        JOIN app.catalog_titles AS t ON t.tconst = a.tconst
        WHERE (
            a.alias_key LIKE '%' || ? || '%'
            OR a.alias_key_articleless LIKE '%' || ? || '%'
        )
          AND (? IS NULL OR t.title_type = ?)
        ORDER BY
            a.alias_priority,
            CASE
                WHEN a.alias_key = ? THEN 0
                WHEN a.alias_key_articleless = ? THEN 1
                WHEN a.alias_key LIKE ? || '%' THEN 2
                WHEN a.alias_key_articleless LIKE ? || '%' THEN 3
                ELSE 4
            END,
            t.start_year DESC NULLS LAST,
            t.num_votes DESC NULLS LAST,
            t.primary_title
        LIMIT ?
    """
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            sql,
            [
                query_key,
                query_key_articleless or query_key,
                title_type,
                title_type,
                query_key,
                query_key_articleless or query_key,
                query_key,
                query_key_articleless or query_key,
                limit,
            ],
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _catalog_row_from_alias_row(row)
            item["fuzzy_score"] = _best_title_similarity(query_key, [item.get("matched_alias_title")])
            items.append(item)
    return items


def _search_catalog_aliases_for_lookup_fuzzy(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 3:
        return []

    prefix3 = query_key[:3]
    prefix2 = query_key[:2]
    length_floor = max(len(query_key) - 2, 1)
    length_ceiling = len(query_key) + 3

    sql = f"""
        SELECT
            t.tconst,
            t.title_type,
            t.primary_title,
            t.original_title,
            t.start_year,
            t.runtime_minutes,
            t.genres,
            t.average_rating,
            t.num_votes,
            a.title AS matched_alias_title,
            a.region,
            a.language,
            a.alias_priority
        FROM app.title_alias_lookup AS a
        JOIN app.catalog_titles AS t ON t.tconst = a.tconst
        WHERE (? IS NULL OR t.title_type = ?)
          AND (
            a.alias_prefix3_articleless = ?
            OR a.alias_prefix2_articleless = ?
          )
          AND a.alias_length_articleless BETWEEN ? AND ?
        ORDER BY
            a.alias_priority,
            t.num_votes DESC NULLS LAST,
            t.average_rating DESC NULLS LAST,
            t.start_year DESC NULLS LAST
        LIMIT 500
    """
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            sql,
            [title_type, title_type, prefix3, prefix2, length_floor, length_ceiling],
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _catalog_row_from_alias_row(row)
            item["fuzzy_score"] = _best_title_similarity(query_key, [item.get("matched_alias_title")])
            items.append(item)
    items.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            -int(item.get("alias_priority") or 99),
            item.get("num_votes") or 0,
            item.get("start_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in items if (item.get("fuzzy_score") or 0.0) >= 0.55][:limit]


def _search_catalog_aliases_for_lookup_levenshtein(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 4:
        return []

    first_letter = query_key[0]
    query_len = len(query_key)
    length_floor = max(query_len - 4, 1)
    length_ceiling = query_len + 4

    sql = f"""
        SELECT
            t.tconst,
            t.title_type,
            t.primary_title,
            t.original_title,
            t.start_year,
            t.runtime_minutes,
            t.genres,
            t.average_rating,
            t.num_votes,
            a.title AS matched_alias_title,
            a.region,
            a.language,
            a.alias_priority
        FROM app.title_alias_lookup AS a
        JOIN app.catalog_titles AS t ON t.tconst = a.tconst
        WHERE (? IS NULL OR t.title_type = ?)
          AND a.alias_prefix1_articleless = ?
          AND a.alias_length_articleless BETWEEN ? AND ?
        ORDER BY
            levenshtein(?, a.alias_key_articleless) ASC,
            a.alias_priority,
            t.num_votes DESC NULLS LAST,
            t.start_year DESC NULLS LAST
        LIMIT 500
    """
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            sql,
            [title_type, title_type, first_letter, length_floor, length_ceiling, query_key],
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _catalog_row_from_alias_row(row)
            item["fuzzy_score"] = _best_title_similarity(query_key, [item.get("matched_alias_title")])
            items.append(item)
    items.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            -int(item.get("alias_priority") or 99),
            item.get("num_votes") or 0,
            item.get("start_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in items if (item.get("fuzzy_score") or 0.0) >= 0.55][:limit]


def _search_catalog_for_lookup_fuzzy(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 3:
        return []

    prefix3 = query_key[:3]
    prefix2 = query_key[:2]
    length_floor = max(len(query_key) - 2, 1)
    length_ceiling = len(query_key) + 3

    sql = f"""
        WITH title_variants AS (
            SELECT
                tconst,
                title_type,
                primary_title,
                original_title,
                start_year,
                runtime_minutes,
                genres,
                average_rating,
                num_votes,
                { _duckdb_match_key_sql("primary_title", strip_leading_articles=True) } AS primary_key,
                { _duckdb_match_key_sql("original_title", strip_leading_articles=True) } AS original_key
            FROM app.catalog_titles
            WHERE (? IS NULL OR title_type = ?)
        )
        SELECT
            tconst,
            title_type,
            primary_title,
            original_title,
            start_year,
            runtime_minutes,
            genres,
            average_rating,
            num_votes
        FROM title_variants
        WHERE (
            left(primary_key, 3) = ?
            OR left(original_key, 3) = ?
            OR left(primary_key, 2) = ?
            OR left(original_key, 2) = ?
        )
          AND (
            length(primary_key) BETWEEN ? AND ?
            OR length(original_key) BETWEEN ? AND ?
          )
        ORDER BY
            num_votes DESC NULLS LAST,
            average_rating DESC NULLS LAST,
            start_year DESC NULLS LAST
        LIMIT 500
    """

    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            sql,
            [
                title_type,
                title_type,
                prefix3,
                prefix3,
                prefix2,
                prefix2,
                length_floor,
                length_ceiling,
                length_floor,
                length_ceiling,
            ],
        ).fetchall()

        scored: list[dict[str, Any]] = []
        for row in rows:
            item = _catalog_row_to_dict(row)
            variants = [item.get("primary_title"), item.get("original_title")]
            item["fuzzy_score"] = _best_title_similarity(query_key, variants)
            scored.append(item)

    scored.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            item.get("num_votes") or 0,
            item.get("start_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in scored if (item.get("fuzzy_score") or 0.0) >= 0.55][:limit]


def _search_catalog_for_lookup_levenshtein(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    query_key = _normalize_match_key(query, strip_leading_articles=True)
    if len(query_key) < 4:
        return []

    first_letter = query_key[0]
    query_len = len(query_key)
    length_floor = max(query_len - 4, 1)
    length_ceiling = query_len + 4

    sql = f"""
        WITH title_variants AS (
            SELECT
                tconst,
                title_type,
                primary_title,
                original_title,
                start_year,
                runtime_minutes,
                genres,
                average_rating,
                num_votes,
                { _duckdb_match_key_sql("primary_title", strip_leading_articles=True) } AS primary_key,
                { _duckdb_match_key_sql("original_title", strip_leading_articles=True) } AS original_key
            FROM app.catalog_titles
            WHERE (? IS NULL OR title_type = ?)
        )
        SELECT
            tconst,
            title_type,
            primary_title,
            original_title,
            start_year,
            runtime_minutes,
            genres,
            average_rating,
            num_votes,
            LEAST(
                levenshtein(?, primary_key),
                levenshtein(?, original_key)
            ) AS edit_distance
        FROM title_variants
        WHERE (
            left(primary_key, 1) = ?
            OR left(original_key, 1) = ?
        )
          AND (
            length(primary_key) BETWEEN ? AND ?
            OR length(original_key) BETWEEN ? AND ?
        )
        ORDER BY edit_distance ASC, num_votes DESC NULLS LAST, average_rating DESC NULLS LAST, start_year DESC NULLS LAST
        LIMIT 500
    """

    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            sql,
            [
                title_type,
                title_type,
                query_key,
                query_key,
                first_letter,
                first_letter,
                length_floor,
                length_ceiling,
                length_floor,
                length_ceiling,
            ],
        ).fetchall()

        scored: list[dict[str, Any]] = []
        for row in rows:
            item = _catalog_row_to_dict(row[:9])
            item["fuzzy_score"] = _best_title_similarity(query_key, [item.get("primary_title"), item.get("original_title")])
            scored.append(item)

    scored.sort(
        key=lambda item: (
            item.get("fuzzy_score") or 0.0,
            item.get("num_votes") or 0,
            item.get("start_year") or 0,
        ),
        reverse=True,
    )
    return [item for item in scored if (item.get("fuzzy_score") or 0.0) >= 0.55][:limit]


def _best_title_similarity(query_key: str, variants: list[Any]) -> float:
    query_tokens = _match_tokens(query_key)
    best = 0.0
    for variant in variants:
        for variant_key in {
            _normalize_match_key(variant),
            _normalize_match_key(variant, strip_leading_articles=True),
        }:
            if not variant_key:
                continue
            sequence_score = difflib.SequenceMatcher(a=query_key, b=variant_key).ratio()
            token_score = _token_similarity_score(query_key, variant_key)
            if len(query_tokens) > 1:
                score = (sequence_score * 0.6) + (token_score * 0.4)
            else:
                score = max(sequence_score, token_score)
            if variant_key.startswith(query_key) or query_key.startswith(variant_key):
                score = max(score, 0.8)
            best = max(best, score)
    return best


def _best_person_name_similarity(query_key: str, primary_name: Any) -> float:
    """Return the best fuzzy score for a person name across full-name and token variants."""
    name_key = _normalize_match_key(primary_name)
    if not query_key or not name_key:
        return 0.0

    name_tokens = _match_tokens(name_key)
    variants: list[str] = [name_key, name_key.replace(" ", "")]
    variants.extend(name_tokens)
    if len(name_tokens) > 1:
        variants.append(name_tokens[-1])

    seen: set[str] = set()
    ordered_variants: list[str] = []
    for variant in variants:
        if variant and variant not in seen:
            seen.add(variant)
            ordered_variants.append(variant)
    return _best_title_similarity(query_key, ordered_variants)


def _token_similarity_score(query_key: str, variant_key: str) -> float:
    query_tokens = _match_tokens(query_key)
    variant_tokens = _match_tokens(variant_key)
    if not query_tokens or not variant_tokens:
        return 0.0

    per_token_scores: list[float] = []
    for query_token in query_tokens:
        best_score = 0.0
        for variant_token in variant_tokens:
            best_score = max(best_score, difflib.SequenceMatcher(a=query_token, b=variant_token).ratio())
        per_token_scores.append(best_score)

    base_score = sum(per_token_scores) / len(per_token_scores)
    ordered_token_bonus = 0.05 if _tokens_are_subsequence(query_tokens, variant_tokens) else 0.0
    return min(1.0, base_score + ordered_token_bonus)


def _match_tokens(value: str) -> list[str]:
    return [token for token in value.split(" ") if token]


def _tokens_are_subsequence(needle: list[str], haystack: list[str]) -> bool:
    if not needle:
        return False
    index = 0
    for token in haystack:
        if token == needle[index]:
            index += 1
            if index == len(needle):
                return True
    return False


def _search_catalog_for_lookup(query: str, title_type: str | None, limit: int) -> list[dict[str, Any]]:
    sql = """
        SELECT
            tconst,
            title_type,
            primary_title,
            original_title,
            start_year,
            runtime_minutes,
            genres,
            average_rating,
            num_votes
        FROM app.catalog_titles
        WHERE (primary_title ILIKE '%' || ? || '%' OR original_title ILIKE '%' || ? || '%')
          AND (? IS NULL OR title_type = ?)
        ORDER BY
            CASE
                WHEN lower(primary_title) = lower(?) THEN 0
                WHEN lower(original_title) = lower(?) THEN 1
                WHEN primary_title ILIKE ? || '%' THEN 2
                WHEN original_title ILIKE ? || '%' THEN 3
                ELSE 4
            END,
            start_year DESC NULLS LAST,
            CASE WHEN average_rating IS NULL THEN 1 ELSE 0 END,
            average_rating DESC,
            num_votes DESC,
            primary_title
        LIMIT ?
    """

    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            sql,
            [query, query, title_type, title_type, query, query, query, query, limit],
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _catalog_row_to_dict(row)
            items.append(item)
    return items


def _fetch_title_people(conn: duckdb.DuckDBPyConnection, tconst: str) -> dict[str, list[dict[str, Any]]]:
    credit_rows = conn.execute(
        """
        SELECT
            c.nconst,
            c.credit_group,
            c.category,
            c.job,
            c.characters,
            c.ordering,
            p.primary_name
        FROM app.title_credits AS c
        JOIN app.catalog_people AS p USING (nconst)
        WHERE c.tconst = ?
        ORDER BY c.ordering, p.primary_name
        """,
        [tconst],
    ).fetchall()

    directors: list[dict[str, Any]] = []
    writers: list[dict[str, Any]] = []
    creators: list[dict[str, Any]] = []
    cast: list[dict[str, Any]] = []
    seen_groups: set[tuple[str, str]] = set()

    for row in credit_rows:
        person = {"nconst": row[0], "name": row[6]}
        group_key = (row[1], row[0])
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)

        if row[1] == "director":
            directors.append(person)
        elif row[1] == "writer":
            writers.append(person)
        elif row[1] == "creator":
            creators.append(person)
        elif row[1] == "cast" and len(cast) < 8:
            cast.append(
                {
                    **person,
                    "character": _principal_character(row[4]),
                    "category": row[2],
                }
            )

    return {
        "directors": directors,
        "writers": writers,
        "creators": creators,
        "cast": cast,
    }


def _principal_character(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return str(value)
    if isinstance(parsed, list) and parsed:
        return str(parsed[0])
    return None


def _title_type_label(title_type: str | None) -> str:
    labels = {
        "movie": "Movie",
        "tvMovie": "TV Movie",
        "tvSeries": "TV Series",
        "tvMiniSeries": "TV Mini Series",
    }
    return labels.get(title_type or "", "Title")


def _is_duckdb_lock_error(exc: duckdb.Error) -> bool:
    message = str(exc).lower()
    return "could not set lock on file" in message or "can't open a connection to same database file" in message


def is_duckdb_lock_error(exc: duckdb.Error) -> bool:
    """Public wrapper for callers that need to detect transient DuckDB lock collisions."""

    return _is_duckdb_lock_error(exc)


def _run_duckdb_write(action: Callable[[duckdb.DuckDBPyConnection], Any]) -> Any:
    last_error: duckdb.Error | None = None
    for attempt in range(DUCKDB_WRITE_RETRY_ATTEMPTS):
        try:
            with duckdb.connect(DB_PATH.as_posix()) as conn:
                return action(conn)
        except duckdb.Error as exc:
            if not _is_duckdb_lock_error(exc):
                raise
            last_error = exc
            if attempt == DUCKDB_WRITE_RETRY_ATTEMPTS - 1:
                break
            time.sleep(DUCKDB_WRITE_RETRY_BASE_DELAY_SECONDS * (attempt + 1))
    assert last_error is not None
    raise last_error


def upsert_tmdb_mapping(
    tconst: str,
    tmdb_media_type: str,
    tmdb_id: int,
    matched_by: str,
    sync_status: str,
    last_error: str | None = None,
) -> None:
    def write(conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            """
            INSERT INTO app.tmdb_title_map (
                tconst,
                tmdb_media_type,
                tmdb_id,
                matched_by,
                matched_at,
                sync_status,
                last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tconst) DO UPDATE SET
                tmdb_media_type = excluded.tmdb_media_type,
                tmdb_id = excluded.tmdb_id,
                matched_by = excluded.matched_by,
                matched_at = excluded.matched_at,
                sync_status = excluded.sync_status,
                last_error = excluded.last_error
            """,
            [tconst, tmdb_media_type, tmdb_id, matched_by, _now_iso(), sync_status, last_error],
        )
    _run_duckdb_write(write)
    clear_title_presentation_cache()


def store_tmdb_payloads(
    tconst: str,
    locale: str,
    detail_payload: dict[str, Any],
    provider_payload: dict[str, Any] | None,
) -> None:
    poster_path = detail_payload.get("poster_path")
    backdrop_path = detail_payload.get("backdrop_path")
    display_title = detail_payload.get("title") or detail_payload.get("name")
    release_date = detail_payload.get("release_date") or detail_payload.get("first_air_date")
    genres_json = json.dumps(detail_payload.get("genres", []), ensure_ascii=False)
    raw_json = json.dumps(detail_payload, ensure_ascii=False)
    providers = (
        provider_payload.get("results", {}).get("CZ", {}) if provider_payload else {}
    )

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            """
            INSERT INTO app.tmdb_title_details (
                tconst,
                locale,
                display_title,
                original_title,
                overview,
                poster_path,
                backdrop_path,
                release_date,
                genres_json,
                raw_json,
                synced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tconst, locale) DO UPDATE SET
                display_title = excluded.display_title,
                original_title = excluded.original_title,
                overview = excluded.overview,
                poster_path = excluded.poster_path,
                backdrop_path = excluded.backdrop_path,
                release_date = excluded.release_date,
                genres_json = excluded.genres_json,
                raw_json = excluded.raw_json,
                synced_at = excluded.synced_at
            """,
            [
                tconst,
                locale,
                display_title,
                detail_payload.get("original_title") or detail_payload.get("original_name"),
                detail_payload.get("overview"),
                poster_path,
                backdrop_path,
                release_date,
                genres_json,
                raw_json,
                _now_iso(),
            ],
        )

        conn.execute("DELETE FROM app.tmdb_watch_providers WHERE tconst = ? AND country_code = 'CZ'", [tconst])
        for provider_type in ("flatrate", "rent", "buy", "ads"):
            for provider in providers.get(provider_type, []):
                conn.execute(
                    """
                    INSERT INTO app.tmdb_watch_providers (
                        tconst,
                        country_code,
                        provider_type,
                        provider_id,
                        provider_name,
                        logo_path,
                        display_priority,
                        synced_at
                    )
                    VALUES (?, 'CZ', ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        tconst,
                        provider_type,
                        provider.get("provider_id"),
                        provider.get("provider_name"),
                        provider.get("logo_path"),
                        provider.get("display_priority"),
                        _now_iso(),
                    ],
                )
    _run_duckdb_write(write)
    clear_title_presentation_cache()


def get_tmdb_mapping(tconst: str) -> dict[str, Any] | None:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        row = conn.execute(
            """
            SELECT tconst, tmdb_media_type, tmdb_id, matched_by, matched_at, sync_status, last_error
            FROM app.tmdb_title_map
            WHERE tconst = ?
            """,
            [tconst],
        ).fetchone()
    if row is None:
        return None
    return {
        "tconst": row[0],
        "tmdb_media_type": row[1],
        "tmdb_id": row[2],
        "matched_by": row[3],
        "matched_at": row[4],
        "sync_status": row[5],
        "last_error": row[6],
    }


def record_tmdb_asset(
    tconst: str,
    asset_kind: str,
    relative_path: str,
    local_path: str,
    fetch_reason: str,
    status: str,
    sha256: str | None,
) -> dict[str, Any]:
    asset_id = str(uuid.uuid4())
    fetched_at = _now_iso()
    def write(conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            """
            INSERT INTO app.tmdb_assets (
                id,
                tconst,
                asset_kind,
                relative_path,
                local_path,
                fetch_reason,
                status,
                sha256,
                fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [asset_id, tconst, asset_kind, relative_path, local_path, fetch_reason, status, sha256, fetched_at],
        )
    _run_duckdb_write(write)
    clear_title_presentation_cache()
    return {
        "id": asset_id,
        "tconst": tconst,
        "asset_kind": asset_kind,
        "relative_path": relative_path,
        "local_path": local_path,
        "fetch_reason": fetch_reason,
        "status": status,
        "sha256": sha256,
        "fetched_at": fetched_at,
    }


def get_latest_tmdb_assets(tconst: str) -> list[dict[str, Any]]:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT id, asset_kind, relative_path, local_path, fetch_reason, status, sha256, fetched_at
            FROM app.tmdb_assets
            WHERE tconst = ?
            ORDER BY fetched_at DESC
            """,
            [tconst],
        ).fetchall()
    return [
        {
            "id": row[0],
            "asset_kind": row[1],
            "relative_path": row[2],
            "local_path": row[3],
            "fetch_reason": row[4],
            "status": row[5],
            "sha256": row[6],
            "fetched_at": row[7],
        }
        for row in rows
    ]


def get_tmdb_detail_locales(tconst: str) -> list[str]:
    ui_config = get_ui_config()
    primary_locale, fallback_locale = ui_config.tmdb_locale_order
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT locale
            FROM app.tmdb_title_details
            WHERE tconst = ?
            ORDER BY
                CASE locale
                    WHEN ? THEN 0
                    WHEN ? THEN 1
                    ELSE 2
                END,
                synced_at DESC
            """,
            [tconst, primary_locale, fallback_locale],
        ).fetchall()
    return [row[0] for row in rows]


def get_tmdb_asset_summary(tconst: str) -> dict[str, dict[str, Any]]:
    latest_by_kind: dict[str, dict[str, Any]] = {}
    for asset in get_latest_tmdb_assets(tconst):
        asset_kind = asset["asset_kind"]
        if asset_kind in latest_by_kind:
            continue
        local_path = asset.get("local_path")
        latest_by_kind[asset_kind] = {
            "status": asset.get("status"),
            "local_path": local_path,
            "exists": bool(local_path and Path(local_path).exists()),
            "fetched_at": asset.get("fetched_at"),
        }
    return latest_by_kind


def get_tmdb_enrichment_targets(limit: int | None = None, include_complete: bool = True) -> list[dict[str, Any]]:
    sql = """
        WITH candidates AS (
            SELECT
                w.tconst AS target_tconst,
                'watched_title' AS reason,
                1 AS priority
            FROM app.watch_events AS w
            JOIN app.catalog_titles AS t ON t.tconst = w.tconst
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                e.series_tconst AS target_tconst,
                'watched_series' AS reason,
                1 AS priority
            FROM app.watch_events AS w
            JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                cs.tconst AS target_tconst,
                'in_progress_title' AS reason,
                2 AS priority
            FROM app.content_state AS cs
            JOIN app.catalog_titles AS t ON t.tconst = cs.tconst
            WHERE cs.interest_state = 'in_progress'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                e.series_tconst AS target_tconst,
                'in_progress_series' AS reason,
                2 AS priority
            FROM app.content_state AS cs
            JOIN app.catalog_episodes AS e ON e.episode_tconst = cs.tconst
            WHERE cs.interest_state = 'in_progress'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                i.tconst AS target_tconst,
                'watchlist' AS reason,
                3 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_titles AS t ON t.tconst = i.tconst
            WHERE i.is_archived = FALSE AND l.list_kind = 'watchlist'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                e.series_tconst AS target_tconst,
                'watchlist_series_from_episode' AS reason,
                3 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
            WHERE i.is_archived = FALSE AND l.list_kind = 'watchlist'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                i.tconst AS target_tconst,
                'plex_library' AS reason,
                3 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_titles AS t ON t.tconst = i.tconst
            WHERE i.is_archived = FALSE AND i.source_origin = 'seed_plex_library'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                e.series_tconst AS target_tconst,
                'plex_library_series_from_episode' AS reason,
                3 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
            WHERE i.is_archived = FALSE AND i.source_origin = 'seed_plex_library'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                i.tconst AS target_tconst,
                'custom_list' AS reason,
                4 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_titles AS t ON t.tconst = i.tconst
            WHERE i.is_archived = FALSE AND l.list_kind = 'custom' AND i.source_origin <> 'seed_plex_library'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                e.series_tconst AS target_tconst,
                'custom_list_series_from_episode' AS reason,
                4 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
            WHERE i.is_archived = FALSE AND l.list_kind = 'custom' AND i.source_origin <> 'seed_plex_library'
            GROUP BY 1, 2, 3
        ),
        ranked AS (
            SELECT
                target_tconst,
                MIN(priority) AS priority,
                string_agg(DISTINCT reason, ', ' ORDER BY reason) AS reasons
            FROM candidates
            WHERE target_tconst IS NOT NULL
            GROUP BY 1
        ),
        detail_flags AS (
            SELECT
                tconst,
                MAX(CASE WHEN locale = 'en-US' THEN 1 ELSE 0 END) AS has_en,
                MAX(CASE WHEN locale = 'cs-CZ' THEN 1 ELSE 0 END) AS has_cs,
                MAX(CASE WHEN locale = 'en-US' THEN poster_path WHEN locale = 'cs-CZ' THEN poster_path ELSE NULL END) AS poster_path,
                MAX(CASE WHEN locale = 'en-US' THEN backdrop_path WHEN locale = 'cs-CZ' THEN backdrop_path ELSE NULL END) AS backdrop_path
            FROM app.tmdb_title_details
            GROUP BY 1
        ),
        asset_flags AS (
            SELECT
                tconst,
                MAX(CASE WHEN asset_kind = 'poster' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_poster,
                MAX(CASE WHEN asset_kind = 'backdrop' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_backdrop
            FROM app.tmdb_assets
            GROUP BY 1
        )
        SELECT
            r.target_tconst,
            t.title_type,
            t.primary_title,
            t.start_year,
            r.priority,
            r.reasons
        FROM ranked AS r
        JOIN app.catalog_titles AS t ON t.tconst = r.target_tconst
        LEFT JOIN detail_flags AS d ON d.tconst = r.target_tconst
        LEFT JOIN asset_flags AS a ON a.tconst = r.target_tconst
        LEFT JOIN app.tmdb_title_map AS m ON m.tconst = r.target_tconst
        WHERE (
            ? = TRUE
            OR NOT (
                COALESCE(d.has_en, 0) = 1
                AND COALESCE(d.has_cs, 0) = 1
                AND (COALESCE(d.poster_path, '') = '' OR COALESCE(a.has_poster, 0) = 1)
                AND (COALESCE(d.backdrop_path, '') = '' OR COALESCE(a.has_backdrop, 0) = 1)
            )
        )
          AND COALESCE(m.sync_status, '') <> 'not_found'
        ORDER BY r.priority, t.start_year DESC NULLS LAST, t.primary_title
    """
    params: list[Any] = [include_complete]
    if limit is not None:
        sql += "\nLIMIT ?"
        params.append(limit)

    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "tconst": row[0],
            "title_type": row[1],
            "primary_title": row[2],
            "start_year": row[3],
            "priority": row[4],
            "reasons": row[5].split(", ") if row[5] else [],
        }
        for row in rows
    ]


def get_title_detail_cache_targets(limit: int | None = None, include_ready: bool = False) -> list[dict[str, Any]]:
    sql = """
        WITH candidates AS (
            SELECT
                w.tconst AS target_tconst,
                'watched_title' AS reason,
                1 AS priority
            FROM app.watch_events AS w
            JOIN app.catalog_titles AS t ON t.tconst = w.tconst
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                e.series_tconst AS target_tconst,
                'watched_series' AS reason,
                1 AS priority
            FROM app.watch_events AS w
            JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                cs.tconst AS target_tconst,
                'in_progress_title' AS reason,
                2 AS priority
            FROM app.content_state AS cs
            JOIN app.catalog_titles AS t ON t.tconst = cs.tconst
            WHERE cs.interest_state = 'in_progress'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                e.series_tconst AS target_tconst,
                'in_progress_series' AS reason,
                2 AS priority
            FROM app.content_state AS cs
            JOIN app.catalog_episodes AS e ON e.episode_tconst = cs.tconst
            WHERE cs.interest_state = 'in_progress'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                i.tconst AS target_tconst,
                'watchlist' AS reason,
                3 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_titles AS t ON t.tconst = i.tconst
            WHERE i.is_archived = FALSE AND l.list_kind = 'watchlist'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                e.series_tconst AS target_tconst,
                'watchlist_series_from_episode' AS reason,
                3 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
            WHERE i.is_archived = FALSE AND l.list_kind = 'watchlist'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                i.tconst AS target_tconst,
                'plex_library' AS reason,
                3 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_titles AS t ON t.tconst = i.tconst
            WHERE i.is_archived = FALSE AND i.source_origin = 'seed_plex_library'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                e.series_tconst AS target_tconst,
                'plex_library_series_from_episode' AS reason,
                3 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
            WHERE i.is_archived = FALSE AND i.source_origin = 'seed_plex_library'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                i.tconst AS target_tconst,
                'custom_list' AS reason,
                4 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_titles AS t ON t.tconst = i.tconst
            WHERE i.is_archived = FALSE AND l.list_kind = 'custom' AND i.source_origin <> 'seed_plex_library'
            GROUP BY 1, 2, 3

            UNION ALL

            SELECT
                e.series_tconst AS target_tconst,
                'custom_list_series_from_episode' AS reason,
                4 AS priority
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
            WHERE i.is_archived = FALSE AND l.list_kind = 'custom' AND i.source_origin <> 'seed_plex_library'
            GROUP BY 1, 2, 3
        ),
        ranked AS (
            SELECT
                target_tconst,
                MIN(priority) AS priority,
                string_agg(DISTINCT reason, ', ' ORDER BY reason) AS reasons
            FROM candidates
            WHERE target_tconst IS NOT NULL
            GROUP BY 1
        ),
        detail_flags AS (
            SELECT
                tconst,
                MAX(CASE WHEN locale = 'en-US' THEN 1 ELSE 0 END) AS has_en,
                MAX(CASE WHEN locale = 'cs-CZ' THEN 1 ELSE 0 END) AS has_cs,
                MAX(CASE WHEN locale = 'en-US' THEN poster_path WHEN locale = 'cs-CZ' THEN poster_path ELSE NULL END) AS poster_path,
                MAX(CASE WHEN locale = 'en-US' THEN backdrop_path WHEN locale = 'cs-CZ' THEN backdrop_path ELSE NULL END) AS backdrop_path
            FROM app.tmdb_title_details
            GROUP BY 1
        ),
        asset_flags AS (
            SELECT
                tconst,
                MAX(CASE WHEN asset_kind = 'poster' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_poster,
                MAX(CASE WHEN asset_kind = 'backdrop' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_backdrop
            FROM app.tmdb_assets
            GROUP BY 1
        )
        SELECT
            r.target_tconst,
            t.title_type,
            t.primary_title,
            t.start_year,
            r.priority,
            r.reasons
        FROM ranked AS r
        JOIN app.catalog_titles AS t ON t.tconst = r.target_tconst
        JOIN detail_flags AS d ON d.tconst = r.target_tconst
        LEFT JOIN asset_flags AS a ON a.tconst = r.target_tconst
        LEFT JOIN app.tmdb_title_map AS m ON m.tconst = r.target_tconst
        WHERE COALESCE(d.has_en, 0) = 1
          AND COALESCE(d.has_cs, 0) = 1
          AND (COALESCE(d.poster_path, '') = '' OR COALESCE(a.has_poster, 0) = 1)
          AND (COALESCE(d.backdrop_path, '') = '' OR COALESCE(a.has_backdrop, 0) = 1)
          AND COALESCE(m.sync_status, '') <> 'not_found'
        ORDER BY r.priority, t.start_year DESC NULLS LAST, t.primary_title
    """
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(sql).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        tconst = row[0]
        cache_path = _title_detail_cache_path(tconst)
        if not cache_path.exists():
            cache_status = "missing"
        else:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                cache_status = "ready" if isinstance(cached, dict) and cached.get("tconst") == tconst else "invalid"
            except (OSError, json.JSONDecodeError):
                cache_status = "invalid"

        if cache_status == "ready" and not include_ready:
            continue

        items.append(
            {
                "tconst": row[0],
                "title_type": row[1],
                "primary_title": row[2],
                "start_year": row[3],
                "priority": row[4],
                "reasons": row[5].split(", ") if row[5] else [],
                "cache_status": cache_status,
                "cache_path": cache_path.as_posix(),
            }
        )
        if limit is not None and len(items) >= limit:
            break
    return items


def _get_relevant_people_candidates(limit: int | None = None) -> list[dict[str, Any]]:
    title_targets = get_title_detail_cache_targets(limit=None, include_ready=True)
    if not title_targets:
        return []

    target_titles = [str(item["tconst"]) for item in title_targets]
    main_cast_limit = 8

    placeholders = ", ".join(["?"] * len(target_titles))
    sql = f"""
        SELECT
            c.nconst,
            p.primary_name,
            p.birth_year,
            p.primary_profession,
            COUNT(DISTINCT c.tconst) AS credit_count,
            MIN(
                CASE c.credit_group
                    WHEN 'director' THEN 1
                    WHEN 'cast' THEN 2
                    ELSE 5
                END
            ) AS group_priority
        FROM app.title_credits AS c
        JOIN app.catalog_people AS p USING (nconst)
        WHERE c.tconst IN ({placeholders})
          AND (
              c.credit_group = 'director'
              OR (c.credit_group = 'cast' AND c.ordering <= ?)
          )
        GROUP BY 1, 2, 3, 4
        ORDER BY group_priority, credit_count DESC, p.birth_year DESC NULLS LAST, p.primary_name
    """
    if limit is not None:
        sql += "\nLIMIT ?"
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        params: list[Any] = [*target_titles, main_cast_limit]
        if limit is not None:
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()

    return [
        {
            "nconst": str(row[0]),
            "name": row[1],
            "birth_year": row[2],
            "primary_profession": row[3],
            "credit_count": row[4],
            "group_priority": row[5],
        }
        for row in rows
    ]


def get_person_detail_cache_targets(limit: int | None = None, include_ready: bool = False) -> list[dict[str, Any]]:
    candidates = _get_relevant_people_candidates(limit=limit)
    if not candidates:
        return []

    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        items: list[dict[str, Any]] = []
        for row in candidates:
            nconst = str(row["nconst"])
            fingerprint = _person_cache_source_fingerprint(conn, nconst)
            cache_status = "missing"
            if fingerprint is not None:
                cache_status = _person_detail_cache_status(nconst, fingerprint)
            if not include_ready and cache_status == "ready":
                continue
            items.append(
                {
                    "nconst": nconst,
                    "name": row["name"],
                    "birth_year": row["birth_year"],
                    "primary_profession": row["primary_profession"],
                    "credit_count": row["credit_count"],
                    "group_priority": row["group_priority"],
                    "cache_status": cache_status,
                }
            )
    return items


def create_import_preview(
    source: str,
    filename: str,
    content: bytes,
    max_rows: int | None = None,
) -> dict[str, Any]:
    batch_id = str(uuid.uuid4())
    checksum = str(hash(content))
    text = content.decode("utf-8-sig", errors="replace")
    rows = _parse_import_rows(source, text)
    if max_rows is not None:
        rows = rows[:max_rows]
    resolver_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    preview_items: list[dict[str, Any]] = []

    with duckdb.connect(DB_PATH.as_posix()) as conn:
        resolution_context = _build_resolution_context(conn, source, rows)
        conn.execute(
            """
            INSERT INTO app.import_batches (id, source, filename, checksum, status, created_at)
            VALUES (?, ?, ?, ?, 'previewed', ?)
            """,
            [batch_id, source, filename, checksum, _now_iso()],
        )
        for idx, row in enumerate(rows, start=1):
            resolution = _resolve_import_row(conn, source, row, resolver_cache, resolution_context)
            preview_items.append({"idx": idx, "row": row, "resolution": resolution})

        if source == "netflix":
            unresolved_rows = [item["row"] for item in preview_items if item["resolution"]["status"] == "unresolved"]
            if unresolved_rows:
                alias_context = _build_netflix_alias_context(conn, unresolved_rows)
                if alias_context:
                    for item in preview_items:
                        if item["resolution"]["status"] != "unresolved":
                            continue
                        alias_resolution = _resolve_netflix_alias_resolution(conn, item["row"], alias_context)
                        if alias_resolution is not None:
                            item["resolution"] = alias_resolution

        import_row_values: list[list[Any]] = []
        resolved_count = 0
        unresolved_count = 0
        for item in preview_items:
            row = item["row"]
            resolution = item["resolution"]
            if resolution["status"] == "resolved":
                resolved_count += 1
            else:
                unresolved_count += 1
            import_row_values.append(
                [
                    str(uuid.uuid4()),
                    batch_id,
                    source,
                    item["idx"],
                    json.dumps(row, ensure_ascii=False),
                    row.get("parsed_title"),
                    row.get("parsed_year"),
                    row.get("parsed_watched_on"),
                    row.get("parsed_season_number"),
                    row.get("parsed_episode_number"),
                    row.get("parsed_imdb_id"),
                    row.get("parsed_tmdb_id"),
                    resolution["status"],
                    resolution.get("tconst"),
                    resolution.get("confidence"),
                    resolution.get("note"),
                ]
            )

        conn.executemany(
            """
            INSERT INTO app.import_rows (
                id,
                batch_id,
                source,
                row_number,
                raw_json,
                parsed_title,
                parsed_year,
                parsed_watched_on,
                parsed_season_number,
                parsed_episode_number,
                parsed_imdb_id,
                parsed_tmdb_id,
                resolution_status,
                resolved_tconst,
                resolution_confidence,
                resolution_note
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            import_row_values,
        )
    return {
        "batch_id": batch_id,
        "source": source,
        "filename": filename,
        "rows_total": len(rows),
        "rows_resolved": resolved_count,
        "rows_unresolved": unresolved_count,
    }


def get_import_batch(batch_id: str) -> dict[str, Any] | None:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        batch = conn.execute(
            """
            SELECT id, source, filename, checksum, status, created_at
            FROM app.import_batches
            WHERE id = ?
            """,
            [batch_id],
        ).fetchone()
        if batch is None:
            return None
        rows = conn.execute(
            """
            SELECT
                row_number,
                parsed_title,
                parsed_year,
                parsed_watched_on,
                parsed_season_number,
                parsed_episode_number,
                parsed_imdb_id,
                parsed_tmdb_id,
                resolution_status,
                resolved_tconst,
                resolution_confidence,
                resolution_note
            FROM app.import_rows
            WHERE batch_id = ?
            ORDER BY row_number
            LIMIT 100
            """,
            [batch_id],
        ).fetchall()
    return {
        "id": batch[0],
        "source": batch[1],
        "filename": batch[2],
        "checksum": batch[3],
        "status": batch[4],
        "created_at": batch[5],
        "rows": [
            {
                "row_number": row[0],
                "parsed_title": row[1],
                "parsed_year": row[2],
                "parsed_watched_on": row[3],
                "parsed_season_number": row[4],
                "parsed_episode_number": row[5],
                "parsed_imdb_id": row[6],
                "parsed_tmdb_id": row[7],
                "resolution_status": row[8],
                "resolved_tconst": row[9],
                "resolution_confidence": row[10],
                "resolution_note": row[11],
            }
            for row in rows
        ],
    }


def commit_import_batch(batch_id: str) -> dict[str, Any]:
    committed = 0
    with duckdb.connect(DB_PATH.as_posix()) as conn:
        status = conn.execute("SELECT status FROM app.import_batches WHERE id = ?", [batch_id]).fetchone()
        if status is None:
            raise ValueError("Import batch neexistuje.")
        if status[0] == "committed":
            return {"batch_id": batch_id, "committed": 0, "status": "already_committed"}

        rows = conn.execute(
            """
            SELECT id, source, parsed_watched_on, resolved_tconst, parsed_season_number, parsed_episode_number
            FROM app.import_rows
            WHERE batch_id = ? AND resolution_status = 'resolved' AND resolved_tconst IS NOT NULL
            """,
            [batch_id],
        ).fetchall()
        for row in rows:
            scope = "episode" if row[4] is not None or row[5] is not None else "title"
            exists = conn.execute(
                """
                SELECT COUNT(*)
                FROM app.watch_events
                WHERE batch_id = ? AND import_row_id = ?
                """,
                [batch_id, row[0]],
            ).fetchone()[0]
            if exists:
                continue
            conn.execute(
                """
                INSERT INTO app.watch_events (
                    id,
                    tconst,
                    event_scope,
                    watched_on,
                    source,
                    batch_id,
                    import_row_id,
                    rating,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                [
                    str(uuid.uuid4()),
                    row[3],
                    scope,
                    row[2],
                    row[1],
                    batch_id,
                    row[0],
                    _now_iso(),
                ],
            )
            committed += 1

        conn.execute("UPDATE app.import_batches SET status = 'committed' WHERE id = ?", [batch_id])
    return {"batch_id": batch_id, "committed": committed, "status": "committed"}


def inspect_trakt_export(export_dir: str = "trakt-export") -> dict[str, Any]:
    export_path = _resolve_export_path(export_dir)
    files = [_describe_trakt_file(path) for path in sorted(export_path.glob("*.json"))]
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
        "fingerprint": _fingerprint_trakt_files(files),
        "file_count": len(files),
        "categories": category_counts,
        "supported_categories": sorted(importable_categories),
        "files": files,
    }


def sync_trakt_export(export_dir: str = "trakt-export") -> dict[str, Any]:
    inspection = inspect_trakt_export(export_dir)
    if inspection["file_count"] == 0:
        raise ValueError("Adresář trakt exportu je prázdný.")

    with duckdb.connect(DB_PATH.as_posix()) as conn:
        _create_base_schema(conn)
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
            _backfill_trakt_snapshots_for_run(conn, latest[0])
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
            [sync_run_id, inspection["export_dir"], inspection["fingerprint"], json.dumps(inspection), _now_iso()],
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
            "history_events": _sync_trakt_history(conn, sync_run_id, files_by_category.get("watched_history", [])),
            "ratings": _sync_trakt_ratings(conn, sync_run_id, files_by_category.get("ratings", [])),
            "lists": _sync_trakt_lists(
                conn,
                sync_run_id,
                files_by_category.get("list_metadata", []),
                files_by_category.get("custom_lists", []),
                files_by_category.get("watchlist", []),
            ),
            "collection": _sync_trakt_collection(conn, sync_run_id, files_by_category.get("collection", [])),
            "last_activities": _read_last_activities(files_by_category.get("last_activities", [])),
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
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
            "summary": _loads_json_or_none(row[4]),
            "created_at": row[5],
        }
        for row in rows
    ]


def get_trakt_sync_run(sync_run_id: str) -> dict[str, Any] | None:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
        "summary": _loads_json_or_none(run[4]),
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
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
        if previous_id and _has_trakt_snapshot(conn, "old.trakt_history_snapshot", current[0]):
            changes = {
                "history_added": _snapshot_change_rows(
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
                "history_removed": _snapshot_change_rows(
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
                "ratings_changed": _snapshot_change_rows(
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
                "list_items_changed": _snapshot_change_rows(
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
                "collection_changed": _snapshot_change_rows(
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
                "history_added": _snapshot_change_count(conn, "old.trakt_history_snapshot", "history_id", current[0], previous_id),
                "history_removed": _snapshot_change_count(conn, "old.trakt_history_snapshot", "history_id", previous_id, current[0]),
                "ratings_changed": _snapshot_change_count(conn, "old.trakt_ratings_snapshot", "source_key", current[0], previous_id),
                "list_items_changed": _snapshot_change_count(conn, "old.trakt_list_items_snapshot", "source_key", current[0], previous_id),
                "collection_changed": _snapshot_change_count(conn, "old.trakt_collection_snapshot", "source_key", current[0], previous_id),
            }
        else:
            changes = {
                "history_added": _fetch_change_rows(
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
                "ratings_changed": _fetch_change_rows(
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
                "list_items_changed": _fetch_change_rows(
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
                "collection_changed": _fetch_change_rows(
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


def get_watch_history(limit: int = 100, source: str | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
        FROM app.watch_events
        WHERE (? IS NULL OR source = ?)
        ORDER BY watched_on DESC, created_at DESC
        LIMIT ?
    """
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        rows = conn.execute(sql, [source, source, limit]).fetchall()
    return [
        {
            "id": row[0],
            "tconst": row[1],
            "event_scope": row[2],
            "watched_on": row[3],
            "source": row[4],
            "batch_id": row[5],
            "import_row_id": row[6],
            "rating": row[7],
            "notes": row[8],
            "created_at": row[9],
        }
        for row in rows
    ]


RECENTLY_WATCHED_VIEW_ID = "view:recently-watched"
WATCHED_VIEW_ID = "view:watched"


def _fetch_watch_view_page(limit: int, offset: int, *, cutoff_days: int | None) -> dict[str, Any]:
    where_clause = "WHERE w.tconst IS NOT NULL"
    params: list[Any] = []
    if cutoff_days is not None:
        where_clause += " AND w.watched_on >= current_date - ?"
        params.append(cutoff_days)

    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        total = conn.execute(
            f"""
            WITH latest_posters AS (
                SELECT
                    tconst,
                    local_path,
                    row_number() OVER (PARTITION BY tconst ORDER BY fetched_at DESC, id DESC) AS rn
                FROM app.tmdb_assets
                WHERE asset_kind = 'poster' AND status = 'fetched'
            ),
            watched_titles AS (
                SELECT
                    COALESCE(e.series_tconst, w.tconst) AS display_tconst,
                    MAX(w.watched_on) AS latest_watched_on,
                    MAX(w.created_at) AS latest_created_at
                FROM app.watch_events AS w
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
                {where_clause}
                GROUP BY 1
            )
            SELECT COUNT(*)
            FROM watched_titles AS w
            LEFT JOIN latest_posters AS p ON p.tconst = w.display_tconst AND p.rn = 1
            WHERE p.local_path IS NOT NULL
            """,
            params,
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
            ),
            watched_titles AS (
                SELECT
                    COALESCE(e.series_tconst, w.tconst) AS display_tconst,
                    MAX(w.watched_on) AS latest_watched_on,
                    MAX(w.created_at) AS latest_created_at
                FROM app.watch_events AS w
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
                {where_clause}
                GROUP BY 1
            )
            SELECT
                w.display_tconst,
                t.title_type AS resolved_title_type,
                t.primary_title AS resolved_title,
                t.start_year AS resolved_year,
                NULL AS season_number,
                NULL AS episode_number,
                NULL AS resolved_series_title,
                p.local_path AS poster_local_path,
                w.latest_watched_on,
                w.latest_created_at
            FROM watched_titles AS w
            JOIN app.catalog_titles AS t ON t.tconst = w.display_tconst
            LEFT JOIN latest_posters AS p ON p.tconst = w.display_tconst AND p.rn = 1
            WHERE p.local_path IS NOT NULL
            ORDER BY w.latest_created_at DESC, resolved_title
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()

    items = [
        {
            "tconst": row[0],
            "title_type": row[1],
            "title": row[2],
            "year": row[3],
            "season_number": row[4],
            "episode_number": row[5],
            "series_title": row[6],
            "poster_url": _poster_url_from_local_path(row[7]),
            "last_watched_on": row[8],
            "last_watched_at": row[9],
            "end_year": None,
            "runtime_minutes": None,
        }
        for row in rows
    ]
    return {"total": total, "items": items, "limit": limit, "offset": offset}


def get_recently_watched_page(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    ui_config = get_ui_config()
    page = _fetch_watch_view_page(limit, offset, cutoff_days=ui_config.recently_watched_days)
    return {
        "list": {
            "id": RECENTLY_WATCHED_VIEW_ID,
            "slug": "recently-watched",
            "name": "Recently Watched",
            "list_kind": "view",
            "item_type": "view",
            "view_kind": "recently_watched",
        },
        "total": page["total"],
        "items": page["items"],
        "limit": page["limit"],
        "offset": page["offset"],
    }


def get_watched_page(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    page = _fetch_watch_view_page(limit, offset, cutoff_days=None)
    return {
        "list": {
            "id": WATCHED_VIEW_ID,
            "slug": "watched",
            "name": "Watched",
            "list_kind": "view",
            "item_type": "view",
            "view_kind": "watched",
        },
        "total": page["total"],
        "items": page["items"],
        "limit": page["limit"],
        "offset": page["offset"],
    }


def get_trakt_ratings(limit: int = 100, active_only: bool = True) -> list[dict[str, Any]]:
    sql = """
        SELECT source_key, media_type, trakt_id, imdb_id, tmdb_id, tconst, parent_title, title,
               season_number, episode_number, rating, rated_at, is_active, last_seen_sync_id
        FROM old.trakt_ratings
        WHERE (? = FALSE OR is_active = TRUE)
        ORDER BY rated_at DESC
        LIMIT ?
    """
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
                item["items"] = [_trakt_list_item_row_to_dict(r) for r in items]
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
            watchlist_items = [_trakt_list_item_row_to_dict(r) for r in watchlist_rows]

    return {
        "lists": result_lists,
        "watchlist": {
            "item_count": watchlist_count,
            "items": watchlist_items if include_items else None,
        },
    }


def get_trakt_collection(limit: int = 100, active_only: bool = True) -> list[dict[str, Any]]:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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

        summary = _loads_json_or_none(latest[4]) or {}
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
    export_path = _resolve_export_path(export_dir)
    files: list[dict[str, Any]] = []
    for path in sorted(export_path.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        item_count = _count_csv_rows(path)
        files.append(
            {
                "name": path.name,
                "path": path.as_posix(),
                "relative_path": path.name,
                "category": _categorize_imdb_list_file(path.name),
                "item_count": item_count,
                "size": path.stat().st_size,
                "mtime": int(path.stat().st_mtime),
                "sha256": _file_sha256(path),
            }
        )
    return {
        "export_dir": export_path.as_posix(),
        "fingerprint": _fingerprint_trakt_files(files),
        "file_count": len(files),
        "files": files,
    }


def sync_imdb_lists(export_dir: str = "imdb_lists") -> dict[str, Any]:
    inspection = inspect_imdb_lists(export_dir)
    if inspection["file_count"] == 0:
        raise ValueError("Adresář imdb_lists je prázdný.")

    with duckdb.connect(DB_PATH.as_posix()) as conn:
        _create_base_schema(conn)
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
            [sync_run_id, inspection["export_dir"], inspection["fingerprint"], json.dumps(inspection), _now_iso()],
        )

        files_by_category = {item["category"]: item for item in inspection["files"]}
        summary = {
            "watchlist": _sync_imdb_watchlist(conn, sync_run_id, files_by_category.get("watchlist")),
            "favorite_people": _sync_imdb_favorite_people(conn, sync_run_id, files_by_category.get("favorite_people")),
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
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
                "summary": (_loads_json_or_none(latest[4]) or {}).get("summary"),
                "created_at": latest[5],
            }
            if latest
            else None
        ),
        "counts": counts,
    }


def get_imdb_watchlist(limit: int = 100, active_only: bool = True) -> list[dict[str, Any]]:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
    server = get_primary_server()
    if server is None:
        return {"server": None, "sections": [], "fingerprint": None}

    sections = [
        section
        for section in get_library_sections(server)
        if section.get("type") in {"movie", "show"} and section.get("hidden") != "1"
    ]
    fingerprint = _plex_fingerprint(server.client_identifier, sections)
    return {
        "server": {
            "name": server.name,
            "client_identifier": server.client_identifier,
        },
        "sections": sections,
        "fingerprint": fingerprint,
    }


def sync_plex_source(section_limit: int | None = None, item_limit_per_section: int | None = None) -> dict[str, Any]:
    plex_server = get_primary_server()
    inspection = inspect_plex_source()
    server = inspection["server"]
    if server is None or plex_server is None:
        raise ValueError("Nebyl nalezen žádný dostupný Plex Media Server.")

    sections = inspection["sections"]
    if section_limit is not None:
        sections = sections[:section_limit]

    with duckdb.connect(DB_PATH.as_posix()) as conn:
        _create_base_schema(conn)
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
                "summary": _loads_json_or_none(
                    conn.execute("SELECT summary_json FROM old.plex_sync_runs WHERE id = ?", [latest[0]]).fetchone()[0]
                ),
            }

        sync_run_id = str(uuid.uuid4())
        now = _now_iso()
        conn.execute(
            """
            INSERT INTO old.plex_sync_runs (
                id, server_name, server_client_identifier, source_fingerprint, status, summary_json, created_at
            )
            VALUES (?, ?, ?, ?, 'running', '{}', ?)
            """,
            [sync_run_id, server["name"], server["client_identifier"], inspection["fingerprint"], now],
        )
        plex_list_id = _ensure_user_list(
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
            items = iter_section_items(section["key"], resource=plex_server, limit=item_limit_per_section)
            for item in items:
                rating_key = item.get("rating_key")
                if not rating_key:
                    continue
                snapshot = get_metadata_snapshot(rating_key, resource=plex_server)
                if snapshot is None:
                    continue
                _upsert_plex_library_item(conn, sync_run_id, section, snapshot)
                summary["items_imported"] += 1

                if _sync_plex_item_to_local_library(conn, plex_list_id, sync_run_id, snapshot, now):
                    summary["library_items_upserted"] += 1
                if _sync_plex_watch_state(conn, snapshot):
                    summary["watch_events_upserted"] += 1
                if _sync_plex_content_state(conn, snapshot, now):
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
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
            "summary": _loads_json_or_none(latest[5]),
            "created_at": latest[6],
        },
        "counts": counts,
    }


def _upsert_plex_library_item(
    conn: duckdb.DuckDBPyConnection,
    sync_run_id: str,
    section: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    ids = snapshot.get("ids") or {}
    imdb_id = ids.get("imdb")
    tconst = None
    if imdb_id:
        found = conn.execute("SELECT tconst FROM app.catalog_titles WHERE tconst = ?", [imdb_id]).fetchone()
        if found:
            tconst = found[0]

    source_key = _plex_source_key(snapshot["rating_key"])
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
            _safe_int(snapshot.get("year")),
            imdb_id,
            _safe_int(ids.get("tmdb")),
            _safe_int(ids.get("tvdb")),
            tconst,
            _safe_int(snapshot.get("view_count")),
            _safe_int(snapshot.get("viewed_leaf_count")),
            _safe_int(snapshot.get("leaf_count")),
            _parse_unix_timestamp(snapshot.get("last_viewed_at")),
            _parse_unix_timestamp(snapshot.get("added_at")),
            _parse_unix_timestamp(snapshot.get("updated_at")),
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
    conn: duckdb.DuckDBPyConnection,
    list_id: str,
    sync_run_id: str,
    snapshot: dict[str, Any],
    now: str,
) -> bool:
    ids = snapshot.get("ids") or {}
    imdb_id = ids.get("imdb")
    tmdb_id = _safe_int(ids.get("tmdb"))
    tconst = imdb_id if imdb_id and conn.execute("SELECT COUNT(*) FROM app.catalog_titles WHERE tconst = ?", [imdb_id]).fetchone()[0] else None
    if tconst is None and imdb_id is None and tmdb_id is None:
        return False
    canonical_key = _canonical_media_key("title", tconst, imdb_id, tmdb_id, None, None, None)

    _upsert_user_list_item(
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
        added_at=_parse_unix_timestamp(snapshot.get("added_at")) or now,
        notes=f"plex_sync:{sync_run_id}",
        source_origin="seed_plex_library",
        source_ref=_plex_source_key(snapshot["rating_key"]),
        now=now,
    )
    return True


def _sync_plex_watch_state(conn: duckdb.DuckDBPyConnection, snapshot: dict[str, Any]) -> bool:
    ids = snapshot.get("ids") or {}
    imdb_id = ids.get("imdb")
    if not imdb_id:
        return False
    found = conn.execute("SELECT COUNT(*) FROM app.catalog_titles WHERE tconst = ?", [imdb_id]).fetchone()[0]
    if not found or not _plex_item_is_watched(snapshot):
        return False

    watched_at = _parse_unix_timestamp(snapshot.get("last_viewed_at")) or _now_iso()
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


def _sync_plex_content_state(conn: duckdb.DuckDBPyConnection, snapshot: dict[str, Any], now: str) -> bool:
    ids = snapshot.get("ids") or {}
    imdb_id = ids.get("imdb")
    if not imdb_id:
        return False
    found = conn.execute("SELECT COUNT(*) FROM app.catalog_titles WHERE tconst = ?", [imdb_id]).fetchone()[0]
    if not found:
        return False

    interest_state = None
    last_watched_at = None
    if _plex_item_is_watched(snapshot):
        interest_state = "watched"
        last_watched_at = _parse_unix_timestamp(snapshot.get("last_viewed_at")) or now
    elif _plex_item_is_in_progress(snapshot):
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


def get_local_library_status() -> dict[str, Any]:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
        ui_config = get_ui_config()
    recently_watched_count = get_recently_watched_page(limit=1, offset=0)["total"]
    watched_count = get_watched_page(limit=1, offset=0)["total"]

    base_lists = [
        {
            "id": row[0],
            "slug": row[1],
            "name": row[2],
            "description": row[3],
            "list_kind": row[4],
            "item_count": row[5],
            "item_type": "list",
        }
        for row in lists
    ]
    visible_lists = list(base_lists)
    visible_lists.append(
        {
            "id": WATCHED_VIEW_ID,
            "slug": "watched",
            "name": "Watched",
            "description": "All watched titles from local history.",
            "list_kind": "view",
            "item_count": watched_count,
            "item_type": "view",
            "view_kind": "watched",
        }
    )
    visible_lists.append(
        {
            "id": RECENTLY_WATCHED_VIEW_ID,
            "slug": "recently-watched",
            "name": "Recently Watched",
            "description": f"Local history from the last {ui_config.recently_watched_days} days.",
            "list_kind": "view",
            "item_count": recently_watched_count,
            "item_type": "view",
            "view_kind": "recently_watched",
        }
    )

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        if item["id"] == "watchlist":
            return (0, item["name"].lower())
        if item.get("view_kind") == "watched":
            return (1, item["name"].lower())
        if item.get("view_kind") == "recently_watched":
            return (2, item["name"].lower())
        return (3, item["name"].lower())

    return {
        "counts": counts,
        "lists": sorted(base_lists, key=sort_key),
        "visible_lists": sorted(visible_lists, key=sort_key),
    }


def _poster_url_from_detail(detail: dict[str, Any] | None) -> str | None:
    tmdb = (detail or {}).get("tmdb") or {}
    assets = tmdb.get("assets") or []
    poster_asset = next((asset for asset in assets if asset.get("asset_kind") == "poster" and asset.get("local_path")), None)
    if not poster_asset or not poster_asset.get("local_path"):
        return None
    return _poster_url_from_local_path(str(poster_asset["local_path"]))


def _poster_url_from_local_path(local_path_value: str | None) -> str | None:
    if not local_path_value:
        return None
    local_path = Path(str(local_path_value))
    try:
        return f"/assets/tmdb/{local_path.relative_to(ASSETS_DIR).as_posix()}"
    except ValueError:
        return None


def get_continue_watching_items(limit: int = 5) -> list[dict[str, Any]]:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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
            "poster_url": _poster_url_from_local_path(row[18]),
        }
        items.append(item)
    return items


def get_user_list_items_page(list_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
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

        total = conn.execute(
            """
            WITH latest_posters AS (
                SELECT
                    tconst,
                    local_path,
                    row_number() OVER (PARTITION BY tconst ORDER BY fetched_at DESC, id DESC) AS rn
                FROM app.tmdb_assets
                WHERE asset_kind = 'poster' AND status = 'fetched'
            )
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT
                    COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AS display_tconst
                FROM app.user_list_items AS i
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
                LEFT JOIN latest_posters AS p ON p.tconst = COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AND p.rn = 1
                WHERE i.list_id = ? AND i.is_archived = FALSE
                  AND COALESCE(e.series_tconst, i.tconst, i.parent_tconst) IS NOT NULL
                  AND p.local_path IS NOT NULL
            ) AS grouped
            """,
            [list_id],
        ).fetchone()[0]

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
            WHERE r.group_row = 1
              AND r.display_tconst IS NOT NULL
              AND p.local_path IS NOT NULL
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
            "poster_url": _poster_url_from_local_path(row[13]),
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


def format_czech_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
    return dt.strftime("%d. %m. %Y %H:%M")


def _sync_trakt_history(
    conn: duckdb.DuckDBPyConnection,
    sync_run_id: str,
    files: list[dict[str, Any]],
) -> dict[str, int]:
    imported = 0
    watch_events_synced = 0
    for file_info in files:
        for item in _load_json_file(Path(file_info["path"])):
            history_id = _safe_int(item.get("id"))
            watched_at = item.get("watched_at")
            if history_id is None or not watched_at:
                continue
            media = _extract_trakt_media(item)
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


def _sync_trakt_ratings(
    conn: duckdb.DuckDBPyConnection,
    sync_run_id: str,
    files: list[dict[str, Any]],
) -> dict[str, int]:
    imported = 0
    for file_info in files:
        for item in _load_json_file(Path(file_info["path"])):
            media = _extract_trakt_media(item)
            source_key = _build_trakt_media_key(media)
            rated_at = item.get("rated_at")
            rating = _safe_int(item.get("rating"))
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
    conn: duckdb.DuckDBPyConnection,
    sync_run_id: str,
    metadata_files: list[dict[str, Any]],
    custom_list_files: list[dict[str, Any]],
    watchlist_files: list[dict[str, Any]],
) -> dict[str, int]:
    imported_lists = 0
    imported_items = 0
    list_name_map: dict[str, str] = {}

    for file_info in metadata_files:
        for item in _load_json_file(Path(file_info["path"])):
            trakt_list_id = _safe_int(((item.get("ids") or {}).get("trakt")))
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
                    _safe_int(item.get("item_count")),
                    item.get("updated_at"),
                    sync_run_id,
                    json.dumps(item, ensure_ascii=False),
                ],
            )
            list_name_map[str(trakt_list_id)] = item.get("name") or ""
            imported_lists += 1
    conn.execute("UPDATE old.trakt_lists SET is_active = FALSE WHERE last_seen_sync_id <> ?", [sync_run_id])

    for file_info in custom_list_files:
        list_id = _parse_trakt_list_id_from_filename(file_info["name"])
        list_name = list_name_map.get(list_id)
        for item in _load_json_file(Path(file_info["path"])):
            media = _extract_trakt_media(item)
            item_id = _safe_int(item.get("id"))
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
                    _safe_int(item.get("rank")),
                    item.get("listed_at"),
                    item.get("notes"),
                    _safe_int(item.get("my_rating")),
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
        for item in _load_json_file(Path(file_info["path"])):
            media = _extract_trakt_media(item)
            item_id = _safe_int(item.get("id"))
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
                    _safe_int(item.get("rank")),
                    item.get("listed_at"),
                    item.get("notes"),
                    _safe_int(item.get("my_rating")),
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


def _sync_trakt_collection(
    conn: duckdb.DuckDBPyConnection,
    sync_run_id: str,
    files: list[dict[str, Any]],
) -> dict[str, int]:
    imported = 0
    for file_info in files:
        for item in _load_json_file(Path(file_info["path"])):
            media = _extract_trakt_media(item)
            source_key = _build_trakt_media_key(media)
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
    if not files:
        return None
    return _load_json_file(Path(files[0]["path"]))


def _catalog_needs_refresh(conn: duckdb.DuckDBPyConnection) -> bool:
    manifest_exists = conn.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = 'app' AND table_name = 'imdb_file_manifest'
        """
    ).fetchone()[0]
    if manifest_exists == 0:
        return True

    table_exists = conn.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = 'app' AND table_name = 'catalog_titles'
        """
    ).fetchone()[0]
    if table_exists == 0:
        return True

    meta_exists = conn.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = 'app' AND table_name = 'catalog_refresh_meta'
        """
    ).fetchone()[0]
    if meta_exists == 0:
        return True

    stored = {
        row[0]: {
            "path": row[1],
            "mtime": row[2],
            "size": row[3],
            "sha256": row[4],
        }
        for row in conn.execute(
            "SELECT source_key, source_path, source_mtime, source_size, source_sha256 FROM app.imdb_file_manifest"
        ).fetchall()
    }

    for source in SOURCE_FILES:
        current_mtime = source.stat_mtime
        current_size = source.stat_size
        current_path = source.path.as_posix()
        stored_row = stored.get(source.key)
        if stored_row is None:
            return True
        if stored_row["path"] != current_path:
            return True
        if stored_row["mtime"] != current_mtime or stored_row["size"] != current_size:
            return True

    return False


def _create_base_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS app")
    conn.execute("CREATE SCHEMA IF NOT EXISTS old")

    conn.execute(
        f"""
        CREATE OR REPLACE VIEW raw.title_basics AS
        SELECT
            tconst,
            titleType AS title_type,
            primaryTitle AS primary_title,
            originalTitle AS original_title,
            TRY_CAST(isAdult AS BOOLEAN) AS is_adult,
            TRY_CAST(startYear AS INTEGER) AS start_year,
            TRY_CAST(endYear AS INTEGER) AS end_year,
            TRY_CAST(runtimeMinutes AS INTEGER) AS runtime_minutes,
            genres
        FROM read_csv_auto('{_sql_path(SOURCE_FILES[0].path)}', delim='\t', header=true, nullstr='\\N')
        """
    )
    conn.execute(
        f"""
        CREATE OR REPLACE VIEW raw.title_ratings AS
        SELECT
            tconst,
            TRY_CAST(averageRating AS DOUBLE) AS average_rating,
            TRY_CAST(numVotes AS INTEGER) AS num_votes
        FROM read_csv_auto('{_sql_path(SOURCE_FILES[1].path)}', delim='\t', header=true, nullstr='\\N')
        """
    )
    conn.execute(
        f"""
        CREATE OR REPLACE VIEW raw.title_episode AS
        SELECT
            tconst,
            parentTconst AS parent_tconst,
            TRY_CAST(seasonNumber AS INTEGER) AS season_number,
            TRY_CAST(episodeNumber AS INTEGER) AS episode_number
        FROM read_csv_auto('{_sql_path(SOURCE_FILES[2].path)}', delim='\t', header=true, nullstr='\\N')
        """
    )
    conn.execute(
        f"""
        CREATE OR REPLACE VIEW raw.title_akas AS
        SELECT
            titleId AS title_id,
            TRY_CAST(ordering AS INTEGER) AS ordering,
            title,
            region,
            language,
            types,
            attributes,
            TRY_CAST(isOriginalTitle AS BOOLEAN) AS is_original_title
        FROM read_csv_auto('{_sql_path(SOURCE_FILES[3].path)}', delim='\t', header=true, nullstr='\\N')
        """
    )
    conn.execute(
        f"""
        CREATE OR REPLACE VIEW raw.title_crew AS
        SELECT
            tconst,
            directors,
            writers
        FROM read_csv_auto('{_sql_path(SOURCE_FILES[4].path)}', delim='\t', header=true, nullstr='\\N')
        """
    )
    conn.execute(
        f"""
        CREATE OR REPLACE VIEW raw.title_principals AS
        SELECT
            tconst,
            TRY_CAST(ordering AS INTEGER) AS ordering,
            nconst,
            category,
            job,
            characters
        FROM read_csv_auto('{_sql_path(SOURCE_FILES[5].path)}', delim='\t', header=true, nullstr='\\N')
        """
    )
    conn.execute(
        f"""
        CREATE OR REPLACE VIEW raw.name_basics AS
        SELECT
            nconst,
            primaryName AS primary_name,
            TRY_CAST(birthYear AS INTEGER) AS birth_year,
            TRY_CAST(deathYear AS INTEGER) AS death_year,
            primaryProfession AS primary_profession,
            knownForTitles AS known_for_titles
        FROM read_csv_auto('{_sql_path(SOURCE_FILES[6].path)}', delim='\t', header=true, nullstr='\\N')
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.content_state (
            tconst VARCHAR PRIMARY KEY,
            interest_state VARCHAR NOT NULL,
            last_previewed_at TIMESTAMP,
            last_watched_at TIMESTAMP,
            updated_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.watch_events (
            id VARCHAR PRIMARY KEY,
            tconst VARCHAR NOT NULL,
            event_scope VARCHAR NOT NULL,
            watched_on DATE NOT NULL,
            source VARCHAR NOT NULL,
            batch_id VARCHAR,
            import_row_id VARCHAR,
            rating SMALLINT,
            notes VARCHAR,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.imdb_file_manifest (
            source_key VARCHAR PRIMARY KEY,
            source_path VARCHAR NOT NULL,
            source_mtime BIGINT NOT NULL,
            source_size BIGINT NOT NULL,
            source_sha256 VARCHAR NOT NULL,
            recorded_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.watchlist (
            tconst VARCHAR PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            priority SMALLINT DEFAULT 3,
            notes VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.tmdb_title_map (
            tconst VARCHAR PRIMARY KEY,
            tmdb_media_type VARCHAR NOT NULL,
            tmdb_id BIGINT NOT NULL,
            matched_by VARCHAR NOT NULL,
            matched_at TIMESTAMP NOT NULL,
            sync_status VARCHAR NOT NULL,
            last_error VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.tmdb_title_details (
            tconst VARCHAR NOT NULL,
            locale VARCHAR NOT NULL,
            display_title VARCHAR,
            original_title VARCHAR,
            overview VARCHAR,
            poster_path VARCHAR,
            backdrop_path VARCHAR,
            release_date VARCHAR,
            genres_json VARCHAR,
            raw_json VARCHAR NOT NULL,
            synced_at TIMESTAMP NOT NULL,
            PRIMARY KEY (tconst, locale)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.tmdb_watch_providers (
            tconst VARCHAR NOT NULL,
            country_code VARCHAR NOT NULL,
            provider_type VARCHAR NOT NULL,
            provider_id BIGINT NOT NULL,
            provider_name VARCHAR,
            logo_path VARCHAR,
            display_priority INTEGER,
            synced_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.tmdb_assets (
            id VARCHAR PRIMARY KEY,
            tconst VARCHAR NOT NULL,
            asset_kind VARCHAR NOT NULL,
            relative_path VARCHAR NOT NULL,
            local_path VARCHAR NOT NULL,
            fetch_reason VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            sha256 VARCHAR,
            fetched_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.import_batches (
            id VARCHAR PRIMARY KEY,
            source VARCHAR NOT NULL,
            filename VARCHAR NOT NULL,
            checksum VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.import_rows (
            id VARCHAR PRIMARY KEY,
            batch_id VARCHAR NOT NULL,
            source VARCHAR NOT NULL,
            row_number INTEGER NOT NULL,
            raw_json VARCHAR NOT NULL,
            parsed_title VARCHAR,
            parsed_year INTEGER,
            parsed_watched_on DATE,
            parsed_season_number INTEGER,
            parsed_episode_number INTEGER,
            parsed_imdb_id VARCHAR,
            parsed_tmdb_id BIGINT,
            resolution_status VARCHAR NOT NULL,
            resolved_tconst VARCHAR,
            resolution_confidence DOUBLE,
            resolution_note VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.trakt_sync_runs (
            id VARCHAR PRIMARY KEY,
            export_path VARCHAR NOT NULL,
            export_fingerprint VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            summary_json VARCHAR NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.trakt_sync_files (
            sync_run_id VARCHAR NOT NULL,
            relative_path VARCHAR NOT NULL,
            file_size BIGINT NOT NULL,
            file_mtime BIGINT NOT NULL,
            file_sha256 VARCHAR NOT NULL,
            category VARCHAR NOT NULL,
            item_count INTEGER,
            imported BOOLEAN NOT NULL,
            PRIMARY KEY (sync_run_id, relative_path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.trakt_history_events (
            history_id BIGINT PRIMARY KEY,
            tconst VARCHAR,
            media_type VARCHAR NOT NULL,
            trakt_id BIGINT,
            imdb_id VARCHAR,
            tmdb_id BIGINT,
            parent_trakt_id BIGINT,
            parent_title VARCHAR,
            title VARCHAR,
            season_number INTEGER,
            episode_number INTEGER,
            watched_at TIMESTAMP NOT NULL,
            watched_on DATE NOT NULL,
            action VARCHAR,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            last_seen_sync_id VARCHAR NOT NULL,
            raw_json VARCHAR NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.trakt_ratings (
            source_key VARCHAR PRIMARY KEY,
            media_type VARCHAR NOT NULL,
            trakt_id BIGINT,
            imdb_id VARCHAR,
            tmdb_id BIGINT,
            tconst VARCHAR,
            parent_trakt_id BIGINT,
            parent_title VARCHAR,
            title VARCHAR,
            season_number INTEGER,
            episode_number INTEGER,
            rating SMALLINT NOT NULL,
            rated_at TIMESTAMP NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            last_seen_sync_id VARCHAR NOT NULL,
            raw_json VARCHAR NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.trakt_lists (
            trakt_list_id BIGINT PRIMARY KEY,
            slug VARCHAR,
            name VARCHAR NOT NULL,
            description VARCHAR,
            privacy VARCHAR,
            list_type VARCHAR,
            item_count INTEGER,
            updated_at TIMESTAMP,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            last_seen_sync_id VARCHAR NOT NULL,
            raw_json VARCHAR NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.trakt_list_items (
            source_key VARCHAR PRIMARY KEY,
            trakt_list_id VARCHAR NOT NULL,
            list_kind VARCHAR NOT NULL,
            list_name VARCHAR,
            item_id BIGINT NOT NULL,
            media_type VARCHAR NOT NULL,
            trakt_id BIGINT,
            imdb_id VARCHAR,
            tmdb_id BIGINT,
            tconst VARCHAR,
            parent_trakt_id BIGINT,
            parent_title VARCHAR,
            title VARCHAR,
            season_number INTEGER,
            episode_number INTEGER,
            rank INTEGER,
            listed_at TIMESTAMP,
            notes VARCHAR,
            my_rating SMALLINT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            last_seen_sync_id VARCHAR NOT NULL,
            raw_json VARCHAR NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.trakt_collection_items (
            source_key VARCHAR PRIMARY KEY,
            media_type VARCHAR NOT NULL,
            trakt_id BIGINT,
            imdb_id VARCHAR,
            tmdb_id BIGINT,
            tconst VARCHAR,
            parent_trakt_id BIGINT,
            parent_title VARCHAR,
            title VARCHAR,
            season_number INTEGER,
            episode_number INTEGER,
            collected_at TIMESTAMP,
            updated_at TIMESTAMP,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            last_seen_sync_id VARCHAR NOT NULL,
            raw_json VARCHAR NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.trakt_history_snapshot (
            sync_run_id VARCHAR NOT NULL,
            history_id BIGINT NOT NULL,
            PRIMARY KEY (sync_run_id, history_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.trakt_ratings_snapshot (
            sync_run_id VARCHAR NOT NULL,
            source_key VARCHAR NOT NULL,
            PRIMARY KEY (sync_run_id, source_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.trakt_list_items_snapshot (
            sync_run_id VARCHAR NOT NULL,
            source_key VARCHAR NOT NULL,
            PRIMARY KEY (sync_run_id, source_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.trakt_collection_snapshot (
            sync_run_id VARCHAR NOT NULL,
            source_key VARCHAR NOT NULL,
            PRIMARY KEY (sync_run_id, source_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.imdb_list_sync_runs (
            id VARCHAR PRIMARY KEY,
            export_path VARCHAR NOT NULL,
            export_fingerprint VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            summary_json VARCHAR NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.imdb_watchlist_items (
            tconst VARCHAR PRIMARY KEY,
            position INTEGER,
            created_at_src DATE,
            modified_at_src DATE,
            description VARCHAR,
            title VARCHAR,
            original_title VARCHAR,
            url VARCHAR,
            title_type VARCHAR,
            imdb_rating DOUBLE,
            runtime_minutes INTEGER,
            year INTEGER,
            genres VARCHAR,
            num_votes INTEGER,
            release_date DATE,
            directors VARCHAR,
            your_rating SMALLINT,
            date_rated DATE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            last_seen_sync_id VARCHAR NOT NULL,
            raw_json VARCHAR NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.imdb_favorite_people (
            nconst VARCHAR PRIMARY KEY,
            position INTEGER,
            created_at_src DATE,
            modified_at_src DATE,
            description VARCHAR,
            name VARCHAR,
            known_for VARCHAR,
            birth_date VARCHAR,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            last_seen_sync_id VARCHAR NOT NULL,
            raw_json VARCHAR NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.plex_sync_runs (
            id VARCHAR PRIMARY KEY,
            server_name VARCHAR NOT NULL,
            server_client_identifier VARCHAR NOT NULL,
            source_fingerprint VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            summary_json VARCHAR NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old.plex_library_items (
            source_key VARCHAR PRIMARY KEY,
            plex_rating_key VARCHAR NOT NULL,
            plex_guid VARCHAR,
            section_key VARCHAR NOT NULL,
            section_title VARCHAR NOT NULL,
            library_type VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            year INTEGER,
            imdb_id VARCHAR,
            tmdb_id BIGINT,
            tvdb_id BIGINT,
            tconst VARCHAR,
            view_count INTEGER,
            viewed_leaf_count INTEGER,
            leaf_count INTEGER,
            last_viewed_at TIMESTAMP,
            added_at_src TIMESTAMP,
            updated_at_src TIMESTAMP,
            originally_available_at DATE,
            directors_json VARCHAR NOT NULL,
            roles_json VARCHAR NOT NULL,
            genres_json VARCHAR NOT NULL,
            countries_json VARCHAR NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            last_seen_sync_id VARCHAR NOT NULL,
            raw_json VARCHAR NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.local_seed_meta (
            seed_name VARCHAR PRIMARY KEY,
            seeded_at TIMESTAMP NOT NULL,
            note VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.user_lists (
            id VARCHAR PRIMARY KEY,
            slug VARCHAR NOT NULL UNIQUE,
            name VARCHAR NOT NULL,
            description VARCHAR,
            list_kind VARCHAR NOT NULL,
            source_origin VARCHAR NOT NULL,
            source_ref VARCHAR,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute("ALTER TABLE app.user_lists ADD COLUMN IF NOT EXISTS description VARCHAR")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.favorite_genres (
            genre VARCHAR PRIMARY KEY,
            weight DOUBLE NOT NULL DEFAULT 1.0,
            preference_rank INTEGER,
            source_origin VARCHAR NOT NULL,
            source_ref VARCHAR,
            notes VARCHAR,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.favorite_traits (
            trait VARCHAR PRIMARY KEY,
            weight DOUBLE NOT NULL DEFAULT 1.0,
            preference_rank INTEGER,
            source_origin VARCHAR NOT NULL,
            source_ref VARCHAR,
            notes VARCHAR,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.user_list_items (
            id VARCHAR PRIMARY KEY,
            list_id VARCHAR NOT NULL,
            canonical_key VARCHAR NOT NULL,
            tconst VARCHAR,
            media_type VARCHAR NOT NULL,
            imdb_id VARCHAR,
            tmdb_id BIGINT,
            trakt_id BIGINT,
            parent_tconst VARCHAR,
            parent_title VARCHAR,
            title VARCHAR,
            season_number INTEGER,
            episode_number INTEGER,
            rank INTEGER,
            added_at TIMESTAMP,
            notes VARCHAR,
            source_origin VARCHAR NOT NULL,
            source_ref VARCHAR,
            is_archived BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            UNIQUE (list_id, canonical_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.user_ratings (
            canonical_key VARCHAR PRIMARY KEY,
            tconst VARCHAR,
            media_type VARCHAR NOT NULL,
            imdb_id VARCHAR,
            tmdb_id BIGINT,
            trakt_id BIGINT,
            parent_tconst VARCHAR,
            parent_title VARCHAR,
            title VARCHAR,
            season_number INTEGER,
            episode_number INTEGER,
            rating SMALLINT NOT NULL,
            rated_at TIMESTAMP,
            source_origin VARCHAR NOT NULL,
            source_ref VARCHAR,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.genre_scores (
            id VARCHAR PRIMARY KEY,
            genre VARCHAR NOT NULL,
            generated_at TIMESTAMP NOT NULL,
            algorithm_version VARCHAR,
            score_scope VARCHAR,
            source_origin VARCHAR NOT NULL,
            source_ref VARCHAR,
            titles_considered INTEGER,
            watched_titles_considered INTEGER,
            rated_titles_considered INTEGER,
            contributing_titles_json VARCHAR,
            excluded_titles_json VARCHAR,
            favorite_genre_weight DOUBLE,
            preference_overlap_score DOUBLE,
            preference_alignment_score DOUBLE,
            affinity_score DOUBLE,
            rating_signal_score DOUBLE,
            watch_signal_score DOUBLE,
            recency_score DOUBLE,
            frequency_score DOUBLE,
            consistency_score DOUBLE,
            novelty_score DOUBLE,
            confidence_score DOUBLE,
            manual_adjustment_score DOUBLE,
            final_score DOUBLE NOT NULL,
            normalized_score DOUBLE,
            rank_in_run INTEGER,
            metrics_json VARCHAR,
            explanation VARCHAR,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app.user_people (
            person_key VARCHAR PRIMARY KEY,
            nconst VARCHAR,
            name VARCHAR NOT NULL,
            known_for VARCHAR,
            birth_date VARCHAR,
            source_origin VARCHAR NOT NULL,
            source_ref VARCHAR,
            is_favorite BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_favorite_genres_active_rank ON app.favorite_genres(is_active, preference_rank)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_favorite_traits_active_rank ON app.favorite_traits(is_active, preference_rank)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_genre_scores_genre_generated_at ON app.genre_scores(genre, generated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_genre_scores_scope_generated_at ON app.genre_scores(score_scope, generated_at)")

    _archive_import_reference_tables(conn)
    _migrate_legacy_watch_history(conn)
    _seed_local_library(conn)


def _archive_import_reference_tables(conn: duckdb.DuckDBPyConnection) -> None:
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
        if not app_exists:
            continue
        if not old_exists:
            continue
        old_count = conn.execute(f"SELECT COUNT(*) FROM old.{table_name}").fetchone()[0]
        if old_count == 0:
            conn.execute(f"INSERT INTO old.{table_name} SELECT * FROM app.{table_name}")
        conn.execute(f"DROP TABLE app.{table_name}")


def _migrate_legacy_watch_history(conn: duckdb.DuckDBPyConnection) -> None:
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
                _now_iso(),
                f"legacy-{row[0]}",
            ],
        )
    conn.execute("DROP TABLE app.watch_history")


def _seed_local_library(conn: duckdb.DuckDBPyConnection) -> None:
    seeded = conn.execute(
        "SELECT COUNT(*) FROM app.local_seed_meta WHERE seed_name = 'initial_import_unification'"
    ).fetchone()[0]
    if seeded:
        return

    now = _now_iso()
    watchlist_id = _ensure_user_list(conn, "watchlist", "Watchlist", "watchlist", "seed_unified", "system:watchlist", now)

    for row in conn.execute(
        """
        SELECT tconst, position, title, original_title, created_at_src, description, your_rating, date_rated
        FROM old.imdb_watchlist_items
        WHERE is_active = TRUE
        """
    ).fetchall():
        _upsert_user_list_item(
            conn,
            list_id=watchlist_id,
            canonical_key=_canonical_media_key("title", row[0], row[0], None, None, None, None),
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
            _upsert_user_rating(
                conn,
                canonical_key=_canonical_media_key("title", row[0], row[0], None, None, None, None),
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
        _upsert_user_list_item(
            conn,
            list_id=watchlist_id,
            canonical_key=_canonical_media_key(row[1], row[5], row[3], row[4], row[2], row[8], row[9]),
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
        list_id = _ensure_user_list(
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
            _upsert_user_list_item(
                conn,
                list_id=list_id,
                canonical_key=_canonical_media_key(item[1], item[5], item[3], item[4], item[2], item[8], item[9]),
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
        _upsert_user_rating(
            conn,
            canonical_key=_canonical_media_key(row[1], row[5], row[3], row[4], row[2], row[8], row[9]),
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

    conn.execute(
        """
        INSERT INTO app.local_seed_meta (seed_name, seeded_at, note)
        VALUES ('initial_import_unification', ?, ?)
        """,
        [now, "Unified imported IMDb/Trakt records into local user_* tables."],
    )


def _migrate_watched_alias_list(conn: duckdb.DuckDBPyConnection) -> None:
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


def _ensure_user_list(
    conn: duckdb.DuckDBPyConnection,
    list_id: str,
    name: str,
    list_kind: str,
    source_origin: str,
    source_ref: str,
    now: str,
    *,
    description: str | None = None,
    preferred_slug: str | None = None,
) -> str:
    slug = preferred_slug or _slugify(name) or list_id
    conn.execute(
        """
        INSERT INTO app.user_lists (id, slug, name, description, list_kind, source_origin, source_ref, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            slug = excluded.slug,
            name = excluded.name,
            description = CASE
                WHEN excluded.description IS NOT NULL THEN excluded.description
                ELSE app.user_lists.description
            END,
            list_kind = excluded.list_kind,
            updated_at = excluded.updated_at
        """,
        [list_id, slug, name, description, list_kind, source_origin, source_ref, now, now],
    )
    return list_id


def _upsert_user_list_item(
    conn: duckdb.DuckDBPyConnection,
    *,
    list_id: str,
    canonical_key: str,
    tconst: str | None,
    media_type: str,
    imdb_id: str | None,
    tmdb_id: int | None,
    trakt_id: int | None,
    parent_tconst: str | None,
    parent_title: str | None,
    title: str | None,
    season_number: int | None,
    episode_number: int | None,
    rank: int | None,
    added_at: str | None,
    notes: str | None,
    source_origin: str,
    source_ref: str | None,
    now: str,
) -> None:
    item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{list_id}|{canonical_key}"))
    conn.execute(
        """
        INSERT INTO app.user_list_items (
            id, list_id, canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title,
            title, season_number, episode_number, rank, added_at, notes, source_origin, source_ref,
            is_archived, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, ?, ?)
        ON CONFLICT (list_id, canonical_key) DO UPDATE SET
            tconst = COALESCE(app.user_list_items.tconst, excluded.tconst),
            imdb_id = COALESCE(app.user_list_items.imdb_id, excluded.imdb_id),
            tmdb_id = COALESCE(app.user_list_items.tmdb_id, excluded.tmdb_id),
            trakt_id = COALESCE(app.user_list_items.trakt_id, excluded.trakt_id),
            parent_tconst = COALESCE(app.user_list_items.parent_tconst, excluded.parent_tconst),
            parent_title = COALESCE(app.user_list_items.parent_title, excluded.parent_title),
            title = COALESCE(app.user_list_items.title, excluded.title),
            rank = COALESCE(app.user_list_items.rank, excluded.rank),
            added_at = COALESCE(app.user_list_items.added_at, excluded.added_at),
            notes = COALESCE(app.user_list_items.notes, excluded.notes),
            is_archived = FALSE,
            updated_at = excluded.updated_at
        """,
        [
            item_id,
            list_id,
            canonical_key,
            tconst,
            media_type,
            imdb_id,
            tmdb_id,
            trakt_id,
            parent_tconst,
            parent_title,
            title,
            season_number,
            episode_number,
            rank,
            added_at,
            notes,
            source_origin,
            source_ref,
            now,
            now,
        ],
    )


def _upsert_user_rating(
    conn: duckdb.DuckDBPyConnection,
    *,
    canonical_key: str,
    tconst: str | None,
    media_type: str,
    imdb_id: str | None,
    tmdb_id: int | None,
    trakt_id: int | None,
    parent_tconst: str | None,
    parent_title: str | None,
    title: str | None,
    season_number: int | None,
    episode_number: int | None,
    rating: int,
    rated_at: str | None,
    source_origin: str,
    source_ref: str | None,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO app.user_ratings (
            canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title, title,
            season_number, episode_number, rating, rated_at, source_origin, source_ref, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (canonical_key) DO UPDATE SET
            tconst = COALESCE(app.user_ratings.tconst, excluded.tconst),
            imdb_id = COALESCE(app.user_ratings.imdb_id, excluded.imdb_id),
            tmdb_id = COALESCE(app.user_ratings.tmdb_id, excluded.tmdb_id),
            trakt_id = COALESCE(app.user_ratings.trakt_id, excluded.trakt_id),
            parent_tconst = COALESCE(app.user_ratings.parent_tconst, excluded.parent_tconst),
            parent_title = COALESCE(app.user_ratings.parent_title, excluded.parent_title),
            title = COALESCE(app.user_ratings.title, excluded.title),
            rating = excluded.rating,
            rated_at = COALESCE(excluded.rated_at, app.user_ratings.rated_at),
            updated_at = excluded.updated_at
        """,
        [
            canonical_key,
            tconst,
            media_type,
            imdb_id,
            tmdb_id,
            trakt_id,
            parent_tconst,
            parent_title,
            title,
            season_number,
            episode_number,
            rating,
            rated_at,
            source_origin,
            source_ref,
            now,
            now,
        ],
    )


def _catalog_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "tconst": row[0],
        "title_type": row[1],
        "primary_title": row[2],
        "original_title": row[3],
        "start_year": row[4],
        "end_year": row[5] if len(row) > 9 else None,
        "runtime_minutes": row[6] if len(row) > 9 else row[5],
        "genres": (
            (row[7] if len(row) > 9 else row[6]).split(",")
            if (row[7] if len(row) > 9 else row[6])
            else []
        ),
        "average_rating": row[8] if len(row) > 9 else row[7],
        "num_votes": row[9] if len(row) > 9 else row[8],
    }


def _build_local_media_identity(detail: dict[str, Any]) -> dict[str, Any]:
    if detail["kind"] == "episode":
        return {
            "tconst": detail["tconst"],
            "media_type": "episode",
            "imdb_id": detail["tconst"],
            "tmdb_id": ((detail.get("tmdb") or {}).get("tmdb_id")),
            "parent_tconst": detail.get("series_tconst"),
            "parent_title": None,
            "title": detail.get("primary_title"),
            "season_number": detail.get("season_number"),
            "episode_number": detail.get("episode_number"),
        }

    return {
        "tconst": detail["tconst"],
        "media_type": "title",
        "imdb_id": detail["tconst"],
        "tmdb_id": ((detail.get("tmdb") or {}).get("tmdb_id")),
        "parent_tconst": None,
        "parent_title": None,
        "title": detail.get("primary_title"),
        "season_number": None,
        "episode_number": None,
    }


def _get_library_summary_for_tconst(tconst: str) -> dict[str, Any]:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        title_type = conn.execute(
            """
            SELECT title_type
            FROM app.catalog_titles
            WHERE tconst = ?
            """,
            [tconst],
        ).fetchone()
        if title_type is not None:
            return _fetch_library_summary(conn, tconst, title_type[0])

        episode = conn.execute(
            """
            SELECT 1
            FROM app.catalog_episodes
            WHERE episode_tconst = ?
            """,
            [tconst],
        ).fetchone()
        if episode is not None:
            return _fetch_library_summary(conn, tconst, "tvEpisode")

    raise ValueError("Titul nebyl nalezen.")


def _fetch_aliases(conn: duckdb.DuckDBPyConnection, tconst: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT title, region, language, types, is_original_title
        FROM app.title_aliases
        WHERE tconst = ?
        ORDER BY region NULLS LAST, language NULLS LAST, title
        LIMIT 20
        """,
        [tconst],
    ).fetchall()
    return [
        {
            "title": row[0],
            "region": row[1],
            "language": row[2],
            "types": row[3],
            "is_original_title": row[4],
        }
        for row in rows
    ]


def _fetch_tmdb(conn: duckdb.DuckDBPyConnection, tconst: str) -> dict[str, Any] | None:
    ui_config = get_ui_config()
    primary_locale, fallback_locale = ui_config.tmdb_locale_order
    mapping = conn.execute(
        """
        SELECT tmdb_media_type, tmdb_id, matched_by, matched_at, sync_status, last_error
        FROM app.tmdb_title_map
        WHERE tconst = ?
        """,
        [tconst],
    ).fetchone()
    if mapping is None:
        return None

    details = conn.execute(
        """
        SELECT locale, display_title, overview, poster_path, backdrop_path, release_date, synced_at
        FROM app.tmdb_title_details
        WHERE tconst = ?
        ORDER BY
            CASE locale
                WHEN ? THEN 0
                WHEN ? THEN 1
                ELSE 2
            END,
            synced_at DESC
        LIMIT 1
        """,
        [tconst, primary_locale, fallback_locale],
    ).fetchone()
    detail_locales = conn.execute(
        """
        SELECT locale
        FROM app.tmdb_title_details
        WHERE tconst = ?
        ORDER BY
            CASE locale
                WHEN ? THEN 0
                WHEN ? THEN 1
                ELSE 2
            END,
            synced_at DESC
        """,
        [tconst, primary_locale, fallback_locale],
    ).fetchall()
    providers = conn.execute(
        """
        SELECT provider_type, provider_name, logo_path
        FROM app.tmdb_watch_providers
        WHERE tconst = ?
        ORDER BY provider_type, display_priority NULLS LAST, provider_name
        """,
        [tconst],
    ).fetchall()
    assets = get_latest_tmdb_assets(tconst)

    return {
        "media_type": mapping[0],
        "tmdb_id": mapping[1],
        "matched_by": mapping[2],
        "matched_at": mapping[3],
        "sync_status": mapping[4],
        "last_error": mapping[5],
        "details": (
            {
                "locale": details[0],
                "display_title": details[1],
                "overview": details[2],
                "poster_path": details[3],
                "backdrop_path": details[4],
                "release_date": details[5],
                "synced_at": details[6],
            }
            if details
            else None
        ),
        "detail_locales": [row[0] for row in detail_locales],
        "providers": [
            {"provider_type": row[0], "provider_name": row[1], "logo_path": row[2]} for row in providers
        ],
        "assets": assets,
    }


def _fetch_content_state(conn: duckdb.DuckDBPyConnection, tconst: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT interest_state, last_previewed_at, last_watched_at, updated_at
        FROM app.content_state
        WHERE tconst = ?
        """,
        [tconst],
    ).fetchone()
    if row is None:
        return None
    return {
        "interest_state": row[0],
        "last_previewed_at": row[1],
        "last_watched_at": row[2],
        "updated_at": row[3],
    }


def _fetch_library_summary(conn: duckdb.DuckDBPyConnection, tconst: str, title_type: str | None) -> dict[str, Any]:
    if title_type in ("tvSeries", "tvMiniSeries"):
        watched_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM app.watch_events AS w
            JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
            WHERE e.series_tconst = ?
            """,
            [tconst],
        ).fetchone()[0]
        last_watched_at = conn.execute(
            """
            SELECT MAX(created_at)
            FROM app.watch_events AS w
            JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
            WHERE e.series_tconst = ?
            """,
            [tconst],
        ).fetchone()[0]
    else:
        watched_count = conn.execute("SELECT COUNT(*) FROM app.watch_events WHERE tconst = ?", [tconst]).fetchone()[0]
        last_watched_at = conn.execute("SELECT MAX(created_at) FROM app.watch_events WHERE tconst = ?", [tconst]).fetchone()[0]

    in_watchlist = conn.execute(
        """
        SELECT COUNT(*)
        FROM app.user_list_items AS i
        JOIN app.user_lists AS l ON l.id = i.list_id
        WHERE i.tconst = ? AND l.list_kind = 'watchlist' AND i.is_archived = FALSE
        """,
        [tconst],
    ).fetchone()[0] > 0
    rating = conn.execute(
        """
        SELECT rating, rated_at
        FROM app.user_ratings
        WHERE tconst = ?
        ORDER BY rated_at DESC NULLS LAST, updated_at DESC
        LIMIT 1
        """,
        [tconst],
    ).fetchone()
    list_rows = conn.execute(
        """
        SELECT l.name, l.list_kind, i.rank, i.added_at
        FROM app.user_list_items AS i
        JOIN app.user_lists AS l ON l.id = i.list_id
        WHERE i.tconst = ? AND i.is_archived = FALSE
        ORDER BY
            CASE WHEN l.list_kind = 'watchlist' THEN 1 ELSE 0 END,
            i.added_at DESC NULLS LAST,
            i.rank NULLS LAST
        LIMIT 20
        """,
        [tconst],
    ).fetchall()
    return {
        "watched_count": watched_count,
        "last_watched_at": last_watched_at,
        "in_watchlist": in_watchlist,
        "rating": (
            {
                "value": rating[0],
                "rated_at": rating[1],
            }
            if rating
            else None
        ),
        "lists": [
            {
                "name": row[0],
                "kind": row[1],
                "rank": row[2],
                "added_at": row[3],
            }
            for row in list_rows
        ],
    }


def _fetch_trakt_summary(conn: duckdb.DuckDBPyConnection, tconst: str, title_type: str | None) -> dict[str, Any]:
    if title_type in ("tvSeries", "tvMiniSeries"):
        history_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM old.trakt_history_events AS h
            JOIN app.catalog_episodes AS e ON e.episode_tconst = h.tconst
            WHERE e.series_tconst = ? AND h.is_active = TRUE
            """,
            [tconst],
        ).fetchone()[0]
        last_watched_at = conn.execute(
            """
            SELECT MAX(h.watched_at)
            FROM old.trakt_history_events AS h
            JOIN app.catalog_episodes AS e ON e.episode_tconst = h.tconst
            WHERE e.series_tconst = ? AND h.is_active = TRUE
            """,
            [tconst],
        ).fetchone()[0]
        collection_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM old.trakt_collection_items AS c
            LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = c.tconst
            WHERE c.is_active = TRUE AND (c.tconst = ? OR e.series_tconst = ?)
            """,
            [tconst, tconst],
        ).fetchone()[0]
    else:
        history_count = conn.execute(
            "SELECT COUNT(*) FROM old.trakt_history_events WHERE tconst = ? AND is_active = TRUE",
            [tconst],
        ).fetchone()[0]
        last_watched_at = conn.execute(
            "SELECT MAX(watched_at) FROM old.trakt_history_events WHERE tconst = ? AND is_active = TRUE",
            [tconst],
        ).fetchone()[0]
        collection_count = conn.execute(
            "SELECT COUNT(*) FROM old.trakt_collection_items WHERE tconst = ? AND is_active = TRUE",
            [tconst],
        ).fetchone()[0]

    direct_rating = conn.execute(
        """
        SELECT rating, rated_at
        FROM old.trakt_ratings
        WHERE tconst = ? AND is_active = TRUE
        ORDER BY rated_at DESC
        LIMIT 1
        """,
        [tconst],
    ).fetchone()
    watchlist = conn.execute(
        """
        SELECT COUNT(*)
        FROM old.trakt_list_items
        WHERE tconst = ? AND list_kind = 'watchlist' AND is_active = TRUE
        """,
        [tconst],
    ).fetchone()[0]
    list_rows = conn.execute(
        """
        SELECT list_name, list_kind, rank, listed_at
        FROM old.trakt_list_items
        WHERE tconst = ? AND is_active = TRUE
        ORDER BY
            CASE WHEN list_kind = 'watchlist' THEN 1 ELSE 0 END,
            listed_at DESC NULLS LAST,
            rank NULLS LAST
        LIMIT 10
        """,
        [tconst],
    ).fetchall()
    return {
        "history_count": history_count,
        "last_watched_at": last_watched_at,
        "in_watchlist": watchlist > 0,
        "collection_count": collection_count,
        "rating": (
            {
                "value": direct_rating[0],
                "rated_at": direct_rating[1],
            }
            if direct_rating
            else None
        ),
        "lists": [
            {
                "name": row[0],
                "kind": row[1],
                "rank": row[2],
                "listed_at": row[3],
            }
            for row in list_rows
        ],
    }


def _fetch_imdb_summary(conn: duckdb.DuckDBPyConnection, tconst: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT position, your_rating, date_rated, is_active
        FROM old.imdb_watchlist_items
        WHERE tconst = ? AND is_active = TRUE
        LIMIT 1
        """,
        [tconst],
    ).fetchone()
    if row is None:
        return {"in_watchlist": False, "position": None, "your_rating": None, "date_rated": None}
    return {
        "in_watchlist": True,
        "position": row[0],
        "your_rating": row[1],
        "date_rated": row[2],
    }


def _parse_import_rows(source: str, text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    if source == "netflix":
        return [_parse_netflix_row(row) for row in reader]
    if source == "trakt":
        return [_parse_trakt_row(row) for row in reader]
    raise ValueError(f"Nepodporovaný source: {source}")


def _parse_netflix_row(row: dict[str, str | None]) -> dict[str, Any]:
    title = (row.get("Title") or row.get("title") or "").strip()
    watched_on = _parse_netflix_date((row.get("Date") or row.get("date") or "").strip())
    parts = [part.strip() for part in title.split(":")]
    parsed_title = parts[0] if parts else title
    season_number = None
    episode_number = None
    episode_title = None
    if len(parts) > 1:
        parsed_title = parts[0]
        season_number = _extract_season_number(parts[1])
    if len(parts) > 2:
        episode_title = ": ".join(part.strip() for part in parts[2:] if part.strip()) or None
        episode_number = _extract_episode_number(episode_title)
    year = _extract_year(parsed_title)
    return {
        "parsed_title": parsed_title,
        "parsed_year": year,
        "parsed_watched_on": watched_on or None,
        "parsed_season_number": season_number,
        "parsed_episode_number": episode_number,
        "parsed_imdb_id": None,
        "parsed_tmdb_id": None,
        "series_title": parsed_title,
        "episode_title": episode_title,
        "raw": row,
    }


def _parse_trakt_row(row: dict[str, str | None]) -> dict[str, Any]:
    title = (row.get("title") or row.get("Title") or "").strip()
    year = _safe_int(row.get("year") or row.get("Year"))
    watched_on = (row.get("watched_at") or row.get("Watched At") or row.get("watched_on") or "").strip()
    season_number = _safe_int(row.get("season") or row.get("Season"))
    episode_number = _safe_int(row.get("episode") or row.get("Episode"))
    imdb_id = (row.get("imdb_id") or row.get("IMDb ID") or "").strip() or None
    tmdb_id = _safe_int(row.get("tmdb_id") or row.get("TMDB ID"))
    return {
        "parsed_title": title,
        "parsed_year": year,
        "parsed_watched_on": watched_on or None,
        "parsed_season_number": season_number,
        "parsed_episode_number": episode_number,
        "parsed_imdb_id": imdb_id,
        "parsed_tmdb_id": tmdb_id,
        "raw": row,
    }


def _resolve_import_row(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    row: dict[str, Any],
    resolver_cache: dict[tuple[Any, ...], dict[str, Any]] | None = None,
    resolution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cache_key = (
        source,
        row.get("parsed_title"),
        row.get("parsed_year"),
        row.get("parsed_season_number"),
        row.get("parsed_episode_number"),
        row.get("parsed_imdb_id"),
        row.get("parsed_tmdb_id"),
        row.get("series_title"),
        row.get("episode_title"),
    )
    if resolver_cache is not None and cache_key in resolver_cache:
        return resolver_cache[cache_key]

    imdb_id = row.get("parsed_imdb_id")
    if imdb_id:
        found = conn.execute("SELECT tconst FROM app.catalog_titles WHERE tconst = ?", [imdb_id]).fetchone()
        if found:
            return _cache_resolution(
                resolver_cache,
                cache_key,
                {"status": "resolved", "tconst": found[0], "confidence": 1.0, "note": "matched_by_imdb_id"},
            )

    tmdb_id = row.get("parsed_tmdb_id")
    if tmdb_id is not None:
        found = conn.execute(
            "SELECT tconst FROM app.tmdb_title_map WHERE tmdb_id = ? ORDER BY matched_at DESC LIMIT 1",
            [tmdb_id],
        ).fetchone()
        if found:
            return _cache_resolution(
                resolver_cache,
                cache_key,
                {"status": "resolved", "tconst": found[0], "confidence": 0.95, "note": "matched_by_tmdb_id"},
            )

    if source == "netflix" and resolution_context is not None:
        episode_tconst = _resolve_netflix_episode_from_context(row, resolution_context)
        if episode_tconst:
            return _cache_resolution(
                resolver_cache,
                cache_key,
                {"status": "resolved", "tconst": episode_tconst, "confidence": 0.9, "note": "matched_by_episode_context"},
            )
        title_tconst = _resolve_netflix_title_from_context(row, resolution_context)
        if title_tconst:
            return _cache_resolution(
                resolver_cache,
                cache_key,
                {"status": "resolved", "tconst": title_tconst, "confidence": 0.8, "note": "matched_by_title_context"},
            )

    if row.get("parsed_season_number") is not None or row.get("parsed_episode_number") is not None:
        found = conn.execute(
            """
            SELECT e.episode_tconst
            FROM app.catalog_episodes AS e
            JOIN app.catalog_titles AS s ON s.tconst = e.series_tconst
            WHERE lower(s.primary_title) = lower(?)
              AND (? IS NULL OR e.season_number = ?)
              AND (? IS NULL OR e.episode_number = ?)
            LIMIT 1
            """,
            [
                row.get("series_title") or row.get("parsed_title"),
                row.get("parsed_season_number"),
                row.get("parsed_season_number"),
                row.get("parsed_episode_number"),
                row.get("parsed_episode_number"),
            ],
        ).fetchone()
        if found:
            return _cache_resolution(
                resolver_cache,
                cache_key,
                {"status": "resolved", "tconst": found[0], "confidence": 0.85, "note": "matched_by_episode"},
            )

        episode_title = row.get("episode_title")
        if episode_title:
            found = conn.execute(
                """
                SELECT e.episode_tconst
                FROM app.catalog_episodes AS e
                JOIN app.catalog_titles AS s ON s.tconst = e.series_tconst
                WHERE lower(s.primary_title) = lower(?)
                  AND (? IS NULL OR e.season_number = ?)
                  AND lower(e.primary_title) = lower(?)
                LIMIT 1
                """,
                [
                    row.get("series_title") or row.get("parsed_title"),
                    row.get("parsed_season_number"),
                    row.get("parsed_season_number"),
                    episode_title,
                ],
            ).fetchone()
            if found:
                return _cache_resolution(
                    resolver_cache,
                    cache_key,
                    {
                        "status": "resolved",
                        "tconst": found[0],
                        "confidence": 0.9,
                        "note": "matched_by_episode_title",
                    },
                )

    found = conn.execute(
        """
        SELECT tconst
        FROM app.catalog_titles
        WHERE lower(primary_title) = lower(?)
          AND (? IS NULL OR start_year = ?)
        ORDER BY num_votes DESC NULLS LAST, average_rating DESC NULLS LAST
        LIMIT 1
        """,
        [row.get("parsed_title"), row.get("parsed_year"), row.get("parsed_year")],
    ).fetchone()
    if found:
        return _cache_resolution(
            resolver_cache,
            cache_key,
            {"status": "resolved", "tconst": found[0], "confidence": 0.8, "note": "matched_by_title_year"},
        )

    if source != "netflix":
        found = conn.execute(
            """
            SELECT tconst
            FROM app.title_aliases
            WHERE lower(title) = lower(?)
            LIMIT 1
            """,
            [row.get("parsed_title")],
        ).fetchone()
        if found:
            return _cache_resolution(
                resolver_cache,
                cache_key,
                {"status": "resolved", "tconst": found[0], "confidence": 0.7, "note": "matched_by_alias"},
            )

    return _cache_resolution(
        resolver_cache,
        cache_key,
        {"status": "unresolved", "tconst": None, "confidence": 0.0, "note": f"unresolved_{source}"},
    )


def _extract_year(title: str) -> int | None:
    if len(title) < 6:
        return None
    for idx in range(len(title) - 5):
        chunk = title[idx : idx + 6]
        if chunk.startswith("(") and chunk.endswith(")") and chunk[1:5].isdigit():
            return int(chunk[1:5])
    return None


def _extract_season_number(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"season\s+(\d+)", value, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _extract_episode_number(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"episode\s+(\d+)", value.strip(), flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _parse_netflix_date(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value


def _safe_int(value: Any) -> int | None:
    if value in (None, "", "\\N"):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _parse_unix_timestamp(value: Any) -> str | None:
    parsed = _safe_int(value)
    if parsed is None:
        return None
    return datetime.fromtimestamp(parsed, UTC).isoformat()


def _safe_float(value: Any) -> float | None:
    if value in (None, "", "\\N"):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def _parse_iso_date(value: Any) -> str | None:
    if value in (None, "", "\\N"):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _slugify(value: str | None) -> str:
    return _normalize_match_key(value).replace(" ", "-")


def _plex_source_key(rating_key: str) -> str:
    return f"plex:{rating_key}"


def _plex_item_is_watched(snapshot: dict[str, Any]) -> bool:
    view_count = _safe_int(snapshot.get("view_count")) or 0
    viewed_leaf_count = _safe_int(snapshot.get("viewed_leaf_count")) or 0
    leaf_count = _safe_int(snapshot.get("leaf_count")) or 0
    return view_count > 0 or (leaf_count > 0 and viewed_leaf_count >= leaf_count)


def _plex_item_is_in_progress(snapshot: dict[str, Any]) -> bool:
    if _plex_item_is_watched(snapshot):
        return False
    viewed_leaf_count = _safe_int(snapshot.get("viewed_leaf_count")) or 0
    leaf_count = _safe_int(snapshot.get("leaf_count")) or 0
    return leaf_count > 0 and viewed_leaf_count > 0


def _plex_fingerprint(server_client_identifier: str, sections: list[dict[str, Any]]) -> str:
    payload = {
        "server_client_identifier": server_client_identifier,
        "sections": [
            {
                "key": section.get("key"),
                "type": section.get("type"),
                "title": section.get("title"),
                "updatedAt": section.get("updatedAt"),
                "scannedAt": section.get("scannedAt"),
                "contentChangedAt": section.get("contentChangedAt"),
            }
            for section in sections
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _canonical_media_key(
    media_type: str | None,
    tconst: str | None,
    imdb_id: str | None,
    tmdb_id: int | None,
    trakt_id: int | None,
    season_number: int | None,
    episode_number: int | None,
) -> str:
    normalized_type = (media_type or "title").lower()
    if tconst:
        return f"{normalized_type}:tconst:{tconst}"
    if imdb_id:
        return f"{normalized_type}:imdb:{imdb_id}"
    if tmdb_id is not None:
        return f"{normalized_type}:tmdb:{tmdb_id}"
    if trakt_id is not None:
        return f"{normalized_type}:trakt:{trakt_id}"
    return f"{normalized_type}:s{season_number or 0}:e{episode_number or 0}"


def _cache_resolution(
    resolver_cache: dict[tuple[Any, ...], dict[str, Any]] | None,
    cache_key: tuple[Any, ...],
    resolution: dict[str, Any],
) -> dict[str, Any]:
    if resolver_cache is not None:
        resolver_cache[cache_key] = resolution
    return resolution


def _build_resolution_context(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if source != "netflix":
        return None

    title_names = sorted({(row.get("parsed_title") or "").strip().lower() for row in rows if row.get("parsed_title")})
    title_keys = sorted({_normalize_match_key(row.get("parsed_title")) for row in rows if row.get("parsed_title")})
    series_names = sorted({(row.get("series_title") or "").strip().lower() for row in rows if row.get("series_title")})
    series_keys = sorted({_normalize_match_key(row.get("series_title")) for row in rows if row.get("series_title")})

    title_map: dict[str, str] = {}
    normalized_title_map: dict[str, str] = {}
    episode_by_number: dict[tuple[str, int, int], str] = {}
    episode_by_title: dict[tuple[str, int, str], str] = {}
    normalized_episode_by_number: dict[tuple[str, int, int], str] = {}
    normalized_episode_by_title: dict[tuple[str, int, str], str] = {}

    if title_names:
        conn.execute("CREATE TEMP TABLE tmp_title_keys (title_lower VARCHAR)")
        conn.executemany("INSERT INTO tmp_title_keys VALUES (?)", [[name] for name in title_names])
        title_rows = conn.execute(
            """
            SELECT lowered_title, tconst
            FROM (
                SELECT
                    lower(primary_title) AS lowered_title,
                    tconst,
                    ROW_NUMBER() OVER (
                        PARTITION BY lower(primary_title)
                        ORDER BY num_votes DESC NULLS LAST, average_rating DESC NULLS LAST
                    ) AS rn
                FROM app.catalog_titles
                WHERE lower(primary_title) IN (SELECT title_lower FROM tmp_title_keys)
            )
            WHERE rn = 1
            """
        ).fetchall()
        title_map = {row[0]: row[1] for row in title_rows}
        conn.execute("DROP TABLE tmp_title_keys")

    if title_keys:
        conn.execute("CREATE TEMP TABLE tmp_norm_title_keys (title_key VARCHAR)")
        conn.executemany("INSERT INTO tmp_norm_title_keys VALUES (?)", [[name] for name in title_keys])
        norm_title_rows = conn.execute(
            f"""
            SELECT norm_title, tconst
            FROM (
                SELECT
                    {_duckdb_match_key_sql('primary_title')} AS norm_title,
                    tconst,
                    ROW_NUMBER() OVER (
                        PARTITION BY {_duckdb_match_key_sql('primary_title')}
                        ORDER BY num_votes DESC NULLS LAST, average_rating DESC NULLS LAST
                    ) AS rn
                FROM app.catalog_titles
                WHERE {_duckdb_match_key_sql('primary_title')} IN (SELECT title_key FROM tmp_norm_title_keys)
            )
            WHERE rn = 1
            """
        ).fetchall()
        normalized_title_map = {row[0]: row[1] for row in norm_title_rows}
        conn.execute("DROP TABLE tmp_norm_title_keys")

    if series_names:
        conn.execute("CREATE TEMP TABLE tmp_series_keys (series_lower VARCHAR)")
        conn.executemany("INSERT INTO tmp_series_keys VALUES (?)", [[name] for name in series_names])
        episode_rows = conn.execute(
            """
            SELECT
                lower(s.primary_title) AS series_lower,
                e.season_number,
                e.episode_number,
                lower(e.primary_title) AS episode_title_lower,
                e.episode_tconst
            FROM app.catalog_episodes AS e
            JOIN app.catalog_titles AS s ON s.tconst = e.series_tconst
            WHERE lower(s.primary_title) IN (SELECT series_lower FROM tmp_series_keys)
            """
        ).fetchall()
        conn.execute("DROP TABLE tmp_series_keys")
        for series_lower, season_number, episode_number, episode_title_lower, episode_tconst in episode_rows:
            if season_number is not None and episode_number is not None:
                episode_by_number.setdefault((series_lower, season_number, episode_number), episode_tconst)
            if season_number is not None and episode_title_lower:
                episode_by_title.setdefault((series_lower, season_number, episode_title_lower), episode_tconst)

    if series_keys:
        conn.execute("CREATE TEMP TABLE tmp_norm_series_keys (series_key VARCHAR)")
        conn.executemany("INSERT INTO tmp_norm_series_keys VALUES (?)", [[name] for name in series_keys])
        norm_episode_rows = conn.execute(
            f"""
            SELECT
                {_duckdb_match_key_sql('s.primary_title')} AS series_key,
                e.season_number,
                e.episode_number,
                {_duckdb_match_key_sql('e.primary_title')} AS episode_title_key,
                e.episode_tconst
            FROM app.catalog_episodes AS e
            JOIN app.catalog_titles AS s ON s.tconst = e.series_tconst
            WHERE {_duckdb_match_key_sql('s.primary_title')} IN (SELECT series_key FROM tmp_norm_series_keys)
            """
        ).fetchall()
        conn.execute("DROP TABLE tmp_norm_series_keys")
        for series_key, season_number, episode_number, episode_title_key, episode_tconst in norm_episode_rows:
            if season_number is not None and episode_number is not None:
                normalized_episode_by_number.setdefault((series_key, season_number, episode_number), episode_tconst)
            if season_number is not None and episode_title_key:
                normalized_episode_by_title.setdefault((series_key, season_number, episode_title_key), episode_tconst)

    return {
        "title_map": title_map,
        "normalized_title_map": normalized_title_map,
        "episode_by_number": episode_by_number,
        "episode_by_title": episode_by_title,
        "normalized_episode_by_number": normalized_episode_by_number,
        "normalized_episode_by_title": normalized_episode_by_title,
    }


def _resolve_netflix_episode_from_context(row: dict[str, Any], resolution_context: dict[str, Any]) -> str | None:
    series_title = (row.get("series_title") or "").strip().lower()
    series_key = _normalize_match_key(row.get("series_title"))
    season_number = row.get("parsed_season_number")
    episode_number = row.get("parsed_episode_number")
    episode_title = (row.get("episode_title") or "").strip().lower()
    episode_key = _normalize_match_key(row.get("episode_title"))
    if series_title and season_number is not None and episode_number is not None:
        match = resolution_context["episode_by_number"].get((series_title, season_number, episode_number))
        if match:
            return match
    if series_key and season_number is not None and episode_number is not None:
        match = resolution_context["normalized_episode_by_number"].get((series_key, season_number, episode_number))
        if match:
            return match
    if series_title and season_number is not None and episode_title:
        return resolution_context["episode_by_title"].get((series_title, season_number, episode_title))
    if series_key and season_number is not None and episode_key:
        match = resolution_context["normalized_episode_by_title"].get((series_key, season_number, episode_key))
        if match:
            return match
    if series_key and episode_key:
        for key in (
            (series_key, season_number or 0, episode_key),
            (series_key, 0, episode_key),
        ):
            match = resolution_context["normalized_episode_by_title"].get(key)
            if match:
                return match
    return None


def _resolve_netflix_title_from_context(row: dict[str, Any], resolution_context: dict[str, Any]) -> str | None:
    title = (row.get("parsed_title") or "").strip().lower()
    title_key = _normalize_match_key(row.get("parsed_title"))
    if not title:
        return None
    return resolution_context["title_map"].get(title) or resolution_context["normalized_title_map"].get(title_key)


def _build_netflix_alias_context(
    conn: duckdb.DuckDBPyConnection,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    title_keys = sorted({_normalize_match_key(row.get("parsed_title")) for row in rows if row.get("parsed_title")})
    if not title_keys:
        return None

    conn.execute("CREATE TEMP TABLE tmp_alias_keys (alias_key VARCHAR)")
    conn.executemany("INSERT INTO tmp_alias_keys VALUES (?)", [[key] for key in title_keys])
    alias_rows = conn.execute(
        f"""
        SELECT alias_key, tconst
        FROM (
            SELECT
                {_duckdb_match_key_sql('title')} AS alias_key,
                tconst,
                ROW_NUMBER() OVER (
                    PARTITION BY {_duckdb_match_key_sql('title')}
                    ORDER BY tconst
                ) AS rn
            FROM app.title_aliases
            WHERE {_duckdb_match_key_sql('title')} IN (SELECT alias_key FROM tmp_alias_keys)
        )
        WHERE rn = 1
        """
    ).fetchall()
    conn.execute("DROP TABLE tmp_alias_keys")
    if not alias_rows:
        return None
    return {"title_map": {row[0]: row[1] for row in alias_rows}}


def _resolve_netflix_alias_resolution(
    conn: duckdb.DuckDBPyConnection,
    row: dict[str, Any],
    alias_context: dict[str, Any],
) -> dict[str, Any] | None:
    title_key = _normalize_match_key(row.get("parsed_title"))
    series_key = _normalize_match_key(row.get("series_title"))
    alias_tconst = alias_context["title_map"].get(title_key)
    if alias_tconst is None:
        return None

    if row.get("parsed_season_number") is None and row.get("parsed_episode_number") is None and not row.get("episode_title"):
        return {"status": "resolved", "tconst": alias_tconst, "confidence": 0.72, "note": "matched_by_alias_title"}

    episode = _resolve_episode_by_series_tconst(
        conn,
        alias_tconst,
        row.get("parsed_season_number"),
        row.get("parsed_episode_number"),
        row.get("episode_title"),
        title_key=series_key,
    )
    if episode:
        return {"status": "resolved", "tconst": episode, "confidence": 0.78, "note": "matched_by_alias_series"}

    return {"status": "resolved", "tconst": alias_tconst, "confidence": 0.7, "note": "matched_by_alias_title"}


def _resolve_episode_by_series_tconst(
    conn: duckdb.DuckDBPyConnection,
    series_tconst: str,
    season_number: int | None,
    episode_number: int | None,
    episode_title: str | None,
    *,
    title_key: str | None = None,
) -> str | None:
    if season_number is not None and episode_number is not None:
        row = conn.execute(
            """
            SELECT episode_tconst
            FROM app.catalog_episodes
            WHERE series_tconst = ? AND season_number = ? AND episode_number = ?
            LIMIT 1
            """,
            [series_tconst, season_number, episode_number],
        ).fetchone()
        if row:
            return row[0]

    if episode_title:
        row = conn.execute(
            f"""
            SELECT episode_tconst
            FROM app.catalog_episodes
            WHERE series_tconst = ?
              AND {_duckdb_match_key_sql('primary_title')} = ?
            LIMIT 1
            """,
            [series_tconst, _normalize_match_key(episode_title)],
        ).fetchone()
        if row:
            return row[0]

    if title_key and episode_title:
        row = conn.execute(
            f"""
            SELECT episode_tconst
            FROM app.catalog_episodes
            WHERE series_tconst = ?
              AND {_duckdb_match_key_sql('primary_title')} = ?
            LIMIT 1
            """,
            [series_tconst, _normalize_match_key(episode_title)],
        ).fetchone()
        if row:
            return row[0]

    return None


def _resolve_export_path(export_dir: str) -> Path:
    raw_path = Path(export_dir)
    path = raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path
    path = path.resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Adresář s Trakt exportem neexistuje: {path}")
    return path


def _sync_imdb_watchlist(
    conn: duckdb.DuckDBPyConnection,
    sync_run_id: str,
    file_info: dict[str, Any] | None,
) -> dict[str, int]:
    if file_info is None:
        return {"imported": 0}
    imported = 0
    for row in _read_csv_rows(Path(file_info["path"])):
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
                _safe_int(row.get("Position")),
                row.get("Created") or None,
                row.get("Modified") or None,
                row.get("Description") or None,
                row.get("Title") or None,
                row.get("Original Title") or None,
                row.get("URL") or None,
                row.get("Title Type") or None,
                _safe_float(row.get("IMDb Rating")),
                _safe_int(row.get("Runtime (mins)")),
                _safe_int(row.get("Year")),
                row.get("Genres") or None,
                _safe_int(row.get("Num Votes")),
                _parse_iso_date(row.get("Release Date")),
                row.get("Directors") or None,
                _safe_int(row.get("Your Rating")),
                _parse_iso_date(row.get("Date Rated")),
                sync_run_id,
                json.dumps(row, ensure_ascii=False),
            ],
        )
        imported += 1
    conn.execute("UPDATE old.imdb_watchlist_items SET is_active = FALSE WHERE last_seen_sync_id <> ?", [sync_run_id])
    return {"imported": imported}


def _sync_imdb_favorite_people(
    conn: duckdb.DuckDBPyConnection,
    sync_run_id: str,
    file_info: dict[str, Any] | None,
) -> dict[str, int]:
    if file_info is None:
        return {"imported": 0}
    imported = 0
    for row in _read_csv_rows(Path(file_info["path"])):
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
                _safe_int(row.get("Position")),
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


def _loads_json_or_none(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)


def _dumps_json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _fetch_change_rows(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any],
) -> list[dict[str, Any]]:
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "entity_id": row[0],
            "media_type": row[1],
            "parent_title": row[2],
            "title": row[3],
            "changed_at": row[4],
        }
        for row in rows
    ]


def _trakt_list_item_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "source_key": row[0],
        "media_type": row[1],
        "imdb_id": row[2],
        "tmdb_id": row[3],
        "tconst": row[4],
        "parent_title": row[5],
        "title": row[6],
        "season_number": row[7],
        "episode_number": row[8],
        "rank": row[9],
        "listed_at": row[10],
        "notes": row[11],
        "my_rating": row[12],
        "is_active": row[13],
    }


def _has_trakt_snapshot(conn: duckdb.DuckDBPyConnection, table_name: str, sync_run_id: str) -> bool:
    return conn.execute(f"SELECT COUNT(*) FROM {table_name} WHERE sync_run_id = ?", [sync_run_id]).fetchone()[0] > 0


def _backfill_trakt_snapshots_for_run(conn: duckdb.DuckDBPyConnection, sync_run_id: str) -> None:
    if not _has_trakt_snapshot(conn, "old.trakt_history_snapshot", sync_run_id):
        conn.execute(
            """
            INSERT INTO old.trakt_history_snapshot (sync_run_id, history_id)
            SELECT ?, history_id
            FROM old.trakt_history_events
            WHERE is_active = TRUE AND last_seen_sync_id = ?
            """,
            [sync_run_id, sync_run_id],
        )
    if not _has_trakt_snapshot(conn, "old.trakt_ratings_snapshot", sync_run_id):
        conn.execute(
            """
            INSERT INTO old.trakt_ratings_snapshot (sync_run_id, source_key)
            SELECT ?, source_key
            FROM old.trakt_ratings
            WHERE is_active = TRUE AND last_seen_sync_id = ?
            """,
            [sync_run_id, sync_run_id],
        )
    if not _has_trakt_snapshot(conn, "old.trakt_list_items_snapshot", sync_run_id):
        conn.execute(
            """
            INSERT INTO old.trakt_list_items_snapshot (sync_run_id, source_key)
            SELECT ?, source_key
            FROM old.trakt_list_items
            WHERE is_active = TRUE AND last_seen_sync_id = ?
            """,
            [sync_run_id, sync_run_id],
        )
    if not _has_trakt_snapshot(conn, "old.trakt_collection_snapshot", sync_run_id):
        conn.execute(
            """
            INSERT INTO old.trakt_collection_snapshot (sync_run_id, source_key)
            SELECT ?, source_key
            FROM old.trakt_collection_items
            WHERE is_active = TRUE AND last_seen_sync_id = ?
            """,
            [sync_run_id, sync_run_id],
        )


def _snapshot_change_count(
    conn: duckdb.DuckDBPyConnection,
    snapshot_table: str,
    key_column: str,
    left_sync_id: str,
    right_sync_id: str,
) -> int:
    return conn.execute(
        f"""
        SELECT COUNT(*)
        FROM {snapshot_table} AS cur
        LEFT JOIN {snapshot_table} AS prev
          ON prev.sync_run_id = ? AND prev.{key_column} = cur.{key_column}
        WHERE cur.sync_run_id = ? AND prev.{key_column} IS NULL
        """,
        [right_sync_id, left_sync_id],
    ).fetchone()[0]


def _snapshot_change_rows(
    conn: duckdb.DuckDBPyConnection,
    snapshot_table: str,
    snapshot_key_column: str,
    entity_table: str,
    entity_key_column: str,
    media_type_column: str,
    parent_title_column: str,
    title_column: str,
    changed_at_column: str,
    left_sync_id: str,
    right_sync_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT
            ent.{entity_key_column} AS entity_id,
            ent.{media_type_column} AS media_type,
            ent.{parent_title_column} AS parent_title,
            ent.{title_column} AS title,
            ent.{changed_at_column} AS changed_at
        FROM {snapshot_table} AS cur
        LEFT JOIN {snapshot_table} AS prev
          ON prev.sync_run_id = ? AND prev.{snapshot_key_column} = cur.{snapshot_key_column}
        JOIN {entity_table} AS ent
          ON ent.{entity_key_column} = cur.{snapshot_key_column}
        WHERE cur.sync_run_id = ? AND prev.{snapshot_key_column} IS NULL
        ORDER BY ent.{changed_at_column} DESC NULLS LAST
        LIMIT ?
        """,
        [right_sync_id, left_sync_id, limit],
    ).fetchall()
    return [
        {
            "entity_id": row[0],
            "media_type": row[1],
            "parent_title": row[2],
            "title": row[3],
            "changed_at": row[4],
        }
        for row in rows
    ]


def _describe_trakt_file(path: Path) -> dict[str, Any]:
    payload = _load_json_file(path)
    stat = path.stat()
    return {
        "name": path.name,
        "path": path.as_posix(),
        "relative_path": path.name,
        "category": _categorize_trakt_file(path.name),
        "item_count": len(payload) if isinstance(payload, list) else 1,
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "sha256": _file_sha256(path),
    }


def _categorize_trakt_file(name: str) -> str:
    if name.startswith("watched-history-"):
        return "watched_history"
    if name.startswith("ratings-"):
        return "ratings"
    if name.startswith("collection-"):
        return "collection"
    if name == "lists-watchlist.json":
        return "watchlist"
    if name == "lists-lists.json":
        return "list_metadata"
    if name.startswith("lists-list-"):
        return "custom_lists"
    if name == "user-last-activities.json":
        return "last_activities"
    if name.startswith("watched-shows-") or name.startswith("watched-movies-"):
        return "watched_summary"
    return "ignored"


def _categorize_imdb_list_file(name: str) -> str:
    if name == "watchlist.csv":
        return "watchlist"
    if name == "favorite_person.csv":
        return "favorite_people"
    return "ignored"


def _fingerprint_trakt_files(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(
            f"{item['relative_path']}|{item['size']}|{item['mtime']}|{item['sha256']}\n".encode("utf-8")
        )
    return digest.hexdigest()


def _load_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _count_csv_rows(path: Path) -> int:
    return len(_read_csv_rows(path))


def _parse_trakt_list_id_from_filename(name: str) -> str:
    match = re.match(r"lists-list-(\d+)-", name)
    return match.group(1) if match else "unknown"


def _extract_trakt_media(item: dict[str, Any]) -> dict[str, Any]:
    media_type = str(item.get("type") or "")
    if media_type == "movie":
        media = item.get("movie") or {}
        ids = media.get("ids") or {}
        imdb_id = ids.get("imdb")
        tmdb_id = _safe_int(ids.get("tmdb"))
        return {
            "media_type": "movie",
            "trakt_id": _safe_int(ids.get("trakt")),
            "imdb_id": imdb_id,
            "tmdb_id": tmdb_id,
            "tconst": imdb_id,
            "parent_trakt_id": None,
            "parent_title": None,
            "title": media.get("title"),
            "season_number": None,
            "episode_number": None,
        }
    if media_type == "show":
        media = item.get("show") or {}
        ids = media.get("ids") or {}
        imdb_id = ids.get("imdb")
        tmdb_id = _safe_int(ids.get("tmdb"))
        return {
            "media_type": "show",
            "trakt_id": _safe_int(ids.get("trakt")),
            "imdb_id": imdb_id,
            "tmdb_id": tmdb_id,
            "tconst": imdb_id,
            "parent_trakt_id": None,
            "parent_title": None,
            "title": media.get("title"),
            "season_number": None,
            "episode_number": None,
        }
    if media_type == "season":
        season = item.get("season") or {}
        show = item.get("show") or {}
        season_ids = season.get("ids") or {}
        show_ids = show.get("ids") or {}
        return {
            "media_type": "season",
            "trakt_id": _safe_int(season_ids.get("trakt")),
            "imdb_id": season_ids.get("imdb"),
            "tmdb_id": _safe_int(season_ids.get("tmdb")),
            "tconst": None,
            "parent_trakt_id": _safe_int(show_ids.get("trakt")),
            "parent_title": show.get("title"),
            "title": f"{show.get('title') or ''} season {season.get('number')}".strip(),
            "season_number": _safe_int(season.get("number")),
            "episode_number": None,
        }
    if media_type == "episode":
        episode = item.get("episode") or {}
        show = item.get("show") or {}
        episode_ids = episode.get("ids") or {}
        show_ids = show.get("ids") or {}
        imdb_id = episode_ids.get("imdb")
        return {
            "media_type": "episode",
            "trakt_id": _safe_int(episode_ids.get("trakt")),
            "imdb_id": imdb_id,
            "tmdb_id": _safe_int(episode_ids.get("tmdb")),
            "tconst": imdb_id,
            "parent_trakt_id": _safe_int(show_ids.get("trakt")),
            "parent_title": show.get("title"),
            "title": episode.get("title"),
            "season_number": _safe_int(episode.get("season")),
            "episode_number": _safe_int(episode.get("number")),
        }
    return {
        "media_type": media_type or "unknown",
        "trakt_id": None,
        "imdb_id": None,
        "tmdb_id": None,
        "tconst": None,
        "parent_trakt_id": None,
        "parent_title": None,
        "title": None,
        "season_number": None,
        "episode_number": None,
    }


def _build_trakt_media_key(media: dict[str, Any]) -> str:
    trakt_id = media.get("trakt_id")
    imdb_id = media.get("imdb_id")
    tmdb_id = media.get("tmdb_id")
    if trakt_id is not None:
        return f"{media['media_type']}:{trakt_id}"
    if imdb_id:
        return f"{media['media_type']}:{imdb_id}"
    if tmdb_id is not None:
        return f"{media['media_type']}:tmdb:{tmdb_id}"
    if media.get("parent_trakt_id") is not None and media.get("season_number") is not None:
        return (
            f"{media['media_type']}:{media['parent_trakt_id']}:"
            f"{media['season_number']}:{media.get('episode_number') or 0}"
        )
    return ""


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _normalize_match_key(value: Any, *, strip_leading_articles: bool = False) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    text = re.sub(r"\(\s*\d{4}\s*\)", " ", text)
    text = text.replace("&", " and ")
    text = text.replace("%", " percent ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\bper\s+cent\b", " percent ", text)
    text = re.sub(r"\bpct\b", " percent ", text)
    text = re.sub(r"\bprocent[a-z]*\b", " percent ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if strip_leading_articles:
        text = re.sub(r"^(the|a|an)\s+", "", text).strip()
    return text


def _duckdb_match_key_sql(column: str, strip_leading_articles: bool = False) -> str:
    base = f"lower({column})"
    base = f"regexp_replace({base}, '%', ' percent ', 'g')"
    base = f"regexp_replace({base}, '\\\\bper\\\\s+cent\\\\b', ' percent ', 'g')"
    base = f"regexp_replace({base}, '\\\\bpct\\\\b', ' percent ', 'g')"
    base = f"regexp_replace({base}, '\\\\bprocent[a-z]*\\\\b', ' percent ', 'g')"
    base = f"regexp_replace({base}, '\\\\(\\\\s*[0-9]{{4}}\\\\s*\\\\)', ' ', 'g')"
    base = f"regexp_replace({base}, '[^[:alnum:]]+', ' ', 'g')"
    base = f"trim(regexp_replace({base}, '\\\\s+', ' ', 'g'))"
    if strip_leading_articles:
        return f"trim(regexp_replace({base}, '^(the|a|an)\\s+', '', 'g'))"
    return base


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
