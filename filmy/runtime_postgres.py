from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import json
from typing import Any

from dotenv import dotenv_values
import psycopg

from filmy.config import UiConfig, get_ui_config
from filmy.paths import ENV_PATH


TARGET_DATABASE = "filmy"


@dataclass(frozen=True)
class RuntimePostgresConfig:
    """Minimal runtime PostgreSQL connection settings."""

    host: str
    port: str
    database: str
    user: str
    password: str


def content_state_uses_postgres(ui_config: UiConfig | None = None) -> bool:
    """Return whether content-state reads/writes should use PostgreSQL."""

    current = ui_config or get_ui_config()
    return current.runtime_content_state_backend == "postgres"


def user_ratings_uses_postgres(ui_config: UiConfig | None = None) -> bool:
    """Return whether user-rating reads/writes should use PostgreSQL."""

    current = ui_config or get_ui_config()
    return current.runtime_user_ratings_backend == "postgres"


def watch_events_uses_postgres(ui_config: UiConfig | None = None) -> bool:
    """Return whether watch-event reads/writes should use PostgreSQL."""

    current = ui_config or get_ui_config()
    return current.runtime_watch_events_backend == "postgres"


def user_lists_uses_postgres(ui_config: UiConfig | None = None) -> bool:
    """Return whether user-list reads/writes should use PostgreSQL."""

    current = ui_config or get_ui_config()
    return current.runtime_user_lists_backend == "postgres"


def app_state_uses_postgres(ui_config: UiConfig | None = None) -> bool:
    """Return whether small app-state reads/writes should use PostgreSQL."""

    current = ui_config or get_ui_config()
    return current.runtime_app_state_backend == "postgres"


def import_backend_uses_postgres(ui_config: UiConfig | None = None) -> bool:
    """Return whether import preview/commit state should use PostgreSQL."""

    current = ui_config or get_ui_config()
    return current.runtime_import_backend == "postgres"


def catalog_backend_uses_postgres(ui_config: UiConfig | None = None) -> bool:
    """Return whether catalog read paths should use PostgreSQL."""

    current = ui_config or get_ui_config()
    return current.runtime_catalog_backend == "postgres"


def tmdb_backend_uses_postgres(ui_config: UiConfig | None = None) -> bool:
    """Return whether TMDB runtime tables should use PostgreSQL."""

    current = ui_config or get_ui_config()
    return current.runtime_tmdb_backend == "postgres"


def meta_backend_uses_postgres(ui_config: UiConfig | None = None) -> bool:
    """Return whether catalog metadata/seed guard should use PostgreSQL."""

    current = ui_config or get_ui_config()
    return current.runtime_meta_backend == "postgres"


def fetch_catalog_search_rows(
    *,
    query: str | None,
    title_type: str | None,
    limit: int,
) -> list[tuple[Any, ...]]:
    """Return base catalog search rows from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
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
            WHERE (%s::text IS NULL OR primary_title ILIKE '%%' || %s::text || '%%' OR original_title ILIKE '%%' || %s::text || '%%')
              AND (%s::text IS NULL OR title_type = %s::text)
            ORDER BY
                CASE WHEN average_rating IS NULL THEN 1 ELSE 0 END,
                average_rating DESC,
                num_votes DESC,
                start_year DESC NULLS LAST,
                primary_title
            LIMIT %s
            """,
            (query, query, query, title_type, title_type, limit),
        )
        return cursor.fetchall()


def fetch_catalog_stats_row() -> dict[str, int | None]:
    """Read top-level catalog stats from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
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
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL catalog stats dotaz nevrátil žádný řádek.")
    return {
        "titles": int(row[0] or 0),
        "movies": int(row[1] or 0),
        "series": int(row[2] or 0),
        "oldest_year": int(row[3]) if row[3] is not None else None,
        "newest_year": int(row[4]) if row[4] is not None else None,
        "episodes": int(row[5] or 0),
        "aliases": int(row[6] or 0),
    }


def fetch_catalog_title_row(tconst: str) -> tuple[Any, ...] | None:
    """Read one title row from PostgreSQL catalog_titles."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
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
            WHERE tconst = %s
            """,
            (tconst,),
        )
        return cursor.fetchone()


def fetch_tconst_for_tmdb_id(tmdb_id: int) -> str | None:
    """Resolve one TMDB ID to the newest mapped IMDb tconst."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT tconst
            FROM app.tmdb_title_map
            WHERE tmdb_id = %s
            ORDER BY matched_at DESC, tconst
            LIMIT 1
            """,
            (tmdb_id,),
        )
        row = cursor.fetchone()
    return str(row[0]) if row and row[0] is not None else None


def fetch_primary_title_matches(lower_titles: list[str]) -> dict[str, str]:
    """Map lower(primary_title) values to best matching tconsts."""

    if not lower_titles:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT lowered_title, tconst
            FROM (
                SELECT
                    lower(primary_title) AS lowered_title,
                    tconst,
                    row_number() OVER (
                        PARTITION BY lower(primary_title)
                        ORDER BY num_votes DESC NULLS LAST, average_rating DESC NULLS LAST, tconst
                    ) AS rn
                FROM app.catalog_titles
                WHERE lower(primary_title) = ANY(%s)
            ) AS ranked
            WHERE rn = 1
            """,
            (lower_titles,),
        )
        rows = cursor.fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}


def fetch_title_lookup_primary_key_matches(title_keys: list[str]) -> dict[str, str]:
    """Map normalized primary-title keys to best matching tconsts."""

    if not title_keys:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT primary_key, tconst
            FROM (
                SELECT
                    primary_key,
                    tconst,
                    row_number() OVER (
                        PARTITION BY primary_key
                        ORDER BY num_votes DESC NULLS LAST, average_rating DESC NULLS LAST, tconst
                    ) AS rn
                FROM app.title_lookup
                WHERE primary_key = ANY(%s)
            ) AS ranked
            WHERE rn = 1
            """,
            (title_keys,),
        )
        rows = cursor.fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}


def fetch_title_alias_lookup_matches(alias_keys: list[str]) -> dict[str, str]:
    """Map normalized alias keys to best matching tconsts."""

    if not alias_keys:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT alias_key, tconst
            FROM (
                SELECT
                    alias_key,
                    tconst,
                    row_number() OVER (
                        PARTITION BY alias_key
                        ORDER BY alias_priority, num_votes DESC NULLS LAST, average_rating DESC NULLS LAST, tconst
                    ) AS rn
                FROM app.title_alias_lookup
                WHERE alias_key = ANY(%s)
            ) AS ranked
            WHERE rn = 1
            """,
            (alias_keys,),
        )
        rows = cursor.fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}


def fetch_title_by_primary_title_year(title: str, year: int | None) -> str | None:
    """Resolve exact primary title with optional year to best tconst."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT tconst
            FROM app.catalog_titles
            WHERE lower(primary_title) = lower(%s::text)
              AND (%s::integer IS NULL OR start_year = %s::integer)
            ORDER BY num_votes DESC NULLS LAST, average_rating DESC NULLS LAST, tconst
            LIMIT 1
            """,
            (title, year, year),
        )
        row = cursor.fetchone()
    return str(row[0]) if row and row[0] is not None else None


def fetch_catalog_primary_title(tconst: str) -> str | None:
    """Read only the primary title for one catalog title."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT primary_title FROM app.catalog_titles WHERE tconst = %s", (tconst,))
        row = cursor.fetchone()
    return str(row[0]) if row and row[0] is not None else None


def fetch_catalog_episode_row(tconst: str) -> tuple[Any, ...] | None:
    """Read one episode row from PostgreSQL catalog_episodes."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
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
            WHERE episode_tconst = %s
            """,
            (tconst,),
        )
        return cursor.fetchone()


def fetch_series_episode_rows(series_tconst: str) -> list[tuple[Any, ...]]:
    """Read ordered episode rows for one series from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                episode_tconst,
                season_number,
                episode_number,
                primary_title,
                start_year
            FROM app.catalog_episodes
            WHERE series_tconst = %s
            ORDER BY season_number NULLS LAST, episode_number NULLS LAST, episode_tconst
            """,
            (series_tconst,),
        )
        return cursor.fetchall()


def fetch_title_alias_rows(tconst: str, *, limit: int = 20) -> list[tuple[Any, ...]]:
    """Read ordered alias rows for one title from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT title, region, language, types, is_original_title
            FROM app.title_aliases
            WHERE tconst = %s
            ORDER BY region NULLS LAST, language NULLS LAST, title
            LIMIT %s
            """,
            (tconst, limit),
        )
        return cursor.fetchall()


def fetch_title_people_rows(tconst: str) -> list[tuple[Any, ...]]:
    """Read credit rows with joined people names for one title from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
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
            WHERE c.tconst = %s
            ORDER BY c.ordering, p.primary_name
            """,
            (tconst,),
        )
        return cursor.fetchall()


def fetch_title_people_preview_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read lightweight director/cast preview rows for many titles from PostgreSQL."""

    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                c.tconst,
                c.credit_group,
                c.ordering,
                p.primary_name
            FROM app.title_credits AS c
            JOIN app.catalog_people AS p USING (nconst)
            WHERE c.tconst = ANY(%s)
              AND c.credit_group IN ('director', 'cast')
              AND (
                  c.credit_group <> 'cast'
                  OR c.ordering IS NULL
                  OR c.ordering <= 5
              )
            ORDER BY
                c.tconst,
                CASE c.credit_group
                    WHEN 'director' THEN 0
                    WHEN 'cast' THEN 1
                    ELSE 2
                END,
                c.ordering NULLS LAST,
                p.primary_name
            """,
            (tconsts,),
        )
        return cursor.fetchall()


def fetch_person_catalog_row(nconst: str) -> tuple[Any, ...] | None:
    """Read one person row from PostgreSQL catalog_people."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT nconst, primary_name, birth_year, death_year, primary_profession, known_for_titles
            FROM app.catalog_people
            WHERE nconst = %s
            """,
            (nconst,),
        )
        return cursor.fetchone()


def fetch_person_lookup_row(nconst: str) -> tuple[Any, ...] | None:
    """Read one person row with lookup-oriented credit count from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                p.nconst,
                p.primary_name,
                p.birth_year,
                p.death_year,
                p.primary_profession,
                p.known_for_titles,
                COALESCE((
                    SELECT COUNT(*)
                    FROM app.title_credits AS c
                    WHERE c.nconst = p.nconst
                ), 0) AS credit_count
            FROM app.catalog_people AS p
            WHERE p.nconst = %s
            """,
            (nconst,),
        )
        return cursor.fetchone()


def fetch_person_credit_rows(nconst: str, *, limit: int = 500) -> list[tuple[Any, ...]]:
    """Read joined person credit rows from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
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
            WHERE c.nconst = %s
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
            LIMIT %s
            """,
            (nconst, limit),
        )
        return cursor.fetchall()


def fetch_person_episode_series_credit_rows(nconst: str, *, limit: int = 200) -> list[tuple[Any, ...]]:
    """Read aggregated episode-only acting credits to parent series from PostgreSQL raw/app tables."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            WITH existing_series AS (
                SELECT DISTINCT tconst
                FROM app.title_credits
                WHERE nconst = %s AND credit_group = 'cast'
            )
            SELECT
                e.parent_tconst AS series_tconst,
                s.primary_title,
                s.original_title,
                s.start_year,
                s.title_type,
                COUNT(*) AS episode_count,
                MIN(p.ordering) AS best_ordering
            FROM raw.title_principals AS p
            JOIN raw.title_episode AS e ON e.tconst = p.tconst
            JOIN app.catalog_titles AS s ON s.tconst = e.parent_tconst
            LEFT JOIN existing_series AS x ON x.tconst = e.parent_tconst
            WHERE p.nconst = %s
              AND p.category IN ('actor', 'actress')
              AND x.tconst IS NULL
            GROUP BY 1, 2, 3, 4, 5
            ORDER BY s.start_year DESC NULLS LAST, best_ordering, s.primary_title
            LIMIT %s
            """,
            (nconst, nconst, limit),
        )
        return cursor.fetchall()


def fetch_known_for_title_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read lightweight title metadata for known-for rows from PostgreSQL."""

    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                tconst,
                primary_title,
                start_year
            FROM app.catalog_titles
            WHERE tconst = ANY(%s)
            """,
            (tconsts,),
        )
        return cursor.fetchall()


def fetch_people_for_lookup_rows(query: str, limit: int) -> list[tuple[Any, ...]]:
    """Read direct person lookup rows from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                nconst,
                primary_name,
                birth_year,
                death_year,
                primary_profession,
                known_for_titles,
                credit_count
            FROM app.person_lookup
            WHERE primary_name ILIKE '%%' || %s::text || '%%'
            ORDER BY
                CASE WHEN lower(primary_name) = lower(%s::text) THEN 0 ELSE 1 END,
                credit_count DESC,
                birth_year DESC NULLS LAST,
                primary_name
            LIMIT %s
            """,
            (query, query, limit),
        )
        return cursor.fetchall()


def fetch_people_for_lookup_fuzzy_rows(query_key: str, limit: int) -> list[tuple[Any, ...]]:
    """Read fuzzy-scannable person lookup rows from PostgreSQL."""

    if len(query_key) < 3:
        return []
    prefix3 = query_key[:3]
    prefix2 = query_key[:2]
    length_floor = max(len(query_key) - 2, 1)
    length_ceiling = len(query_key) + 3
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                nconst,
                primary_name,
                birth_year,
                death_year,
                primary_profession,
                known_for_titles,
                credit_count
            FROM app.person_lookup
            WHERE (
                name_prefix3 = %s
                OR first_token_prefix3 = %s
                OR last_token_prefix3 = %s
                OR compact_name_prefix3 = %s
                OR name_prefix2 = %s
                OR first_token_prefix2 = %s
                OR last_token_prefix2 = %s
                OR compact_name_prefix2 = %s
            )
              AND (
                name_length BETWEEN %s AND %s
                OR last_token_length BETWEEN %s AND %s
                OR compact_name_length BETWEEN %s AND %s
              )
            ORDER BY credit_count DESC, birth_year DESC NULLS LAST, primary_name
            LIMIT %s
            """,
            (
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
                max(limit, 500),
            ),
        )
        return cursor.fetchall()


def fetch_people_for_lookup_levenshtein_rows(query_key: str, limit: int) -> list[tuple[Any, ...]]:
    """Read levenshtein-scannable person lookup rows from PostgreSQL."""

    if len(query_key) < 4:
        return []
    first_letter = query_key[0]
    query_len = len(query_key)
    length_floor = max(query_len - 4, 1)
    length_ceiling = query_len + 4
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                nconst,
                primary_name,
                birth_year,
                death_year,
                primary_profession,
                known_for_titles,
                credit_count,
                least(
                    levenshtein(%s, name_key),
                    levenshtein(%s, last_token_key),
                    levenshtein(%s, compact_name_key)
                ) AS edit_distance
            FROM app.person_lookup
            WHERE (
                name_prefix1 = %s
                OR first_token_prefix1 = %s
                OR last_token_prefix1 = %s
                OR compact_name_prefix1 = %s
            )
              AND (
                name_length BETWEEN %s AND %s
                OR last_token_length BETWEEN %s AND %s
                OR compact_name_length BETWEEN %s AND %s
              )
            ORDER BY edit_distance ASC, credit_count DESC, birth_year DESC NULLS LAST, primary_name
            LIMIT %s
            """,
            (
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
                max(limit, 500),
            ),
        )
        return cursor.fetchall()


def fetch_episode_series_map(tconsts: list[str]) -> dict[str, str]:
    """Map episode tconsts to parent series tconsts from PostgreSQL."""

    if not tconsts:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT episode_tconst, series_tconst
            FROM app.catalog_episodes
            WHERE episode_tconst = ANY(%s)
              AND series_tconst IS NOT NULL
            """,
            (tconsts,),
        )
        rows = cursor.fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}


def fetch_title_card_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read lightweight title-card rows including latest poster from PostgreSQL."""

    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                tconst,
                title_type,
                start_year,
                primary_title,
                poster_relative_path,
                poster_local_path
            FROM app.catalog_title_cards
            WHERE tconst = ANY(%s)
            """,
            (tconsts,),
        )
        return cursor.fetchall()


def fetch_title_card_detail_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read richer lightweight title-card rows for many titles from PostgreSQL."""

    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                t.tconst,
                t.title_type,
                t.start_year,
                t.primary_title,
                t.original_title,
                t.runtime_minutes,
                t.genres,
                t.average_rating,
                t.num_votes,
                c.poster_relative_path,
                c.poster_local_path
            FROM app.catalog_titles AS t
            LEFT JOIN app.catalog_title_cards AS c ON c.tconst = t.tconst
            WHERE t.tconst = ANY(%s)
            """,
            (tconsts,),
        )
        return cursor.fetchall()


def fetch_catalog_brief_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read lightweight catalog rows for many tconsts from PostgreSQL."""

    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT tconst, title_type, primary_title, start_year
            FROM app.catalog_titles
            WHERE tconst = ANY(%s)
            """,
            (tconsts,),
        )
        return cursor.fetchall()


def fetch_title_overviews(tconsts: list[str], *, primary_locale: str = "cs-CZ", fallback_locale: str = "en-US") -> dict[str, str]:
    """Read best available TMDB overview text for many titles from PostgreSQL."""

    if not tconsts:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT ranked.tconst, ranked.overview
            FROM (
                SELECT
                    d.tconst,
                    d.overview,
                    row_number() OVER (
                        PARTITION BY d.tconst
                        ORDER BY
                            CASE d.locale
                                WHEN %s THEN 0
                                WHEN %s THEN 1
                                ELSE 2
                            END,
                            d.synced_at DESC
                    ) AS rn
                FROM app.tmdb_title_details AS d
                WHERE d.tconst = ANY(%s)
                  AND COALESCE(length(trim(d.overview)), 0) > 0
            ) AS ranked
            WHERE ranked.rn = 1
            """,
            (primary_locale, fallback_locale, tconsts),
        )
        rows = cursor.fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}


def fetch_continue_watching_catalog_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read continue-watching catalog/title+episode rows with latest posters from PostgreSQL."""

    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                COALESCE(t.tconst, e.episode_tconst) AS target_tconst,
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
                COALESCE(p.poster_relative_path, sp.poster_relative_path) AS poster_relative_path,
                COALESCE(p.poster_local_path, sp.poster_local_path) AS poster_local_path
            FROM app.catalog_titles AS t
            LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = t.tconst
            LEFT JOIN app.catalog_titles AS s ON s.tconst = e.series_tconst
            LEFT JOIN app.latest_title_posters AS p ON p.tconst = t.tconst
            LEFT JOIN app.latest_title_posters AS sp ON sp.tconst = e.series_tconst
            WHERE t.tconst = ANY(%s)

            UNION ALL

            SELECT
                e.episode_tconst AS target_tconst,
                'tvEpisode' AS title_type,
                e.primary_title AS primary_title,
                e.original_title AS original_title,
                e.start_year AS start_year,
                NULL AS end_year,
                e.runtime_minutes AS runtime_minutes,
                NULL AS genres,
                NULL AS average_rating,
                NULL AS num_votes,
                e.series_tconst,
                e.season_number,
                e.episode_number,
                s.primary_title AS series_title,
                COALESCE(p.poster_relative_path, sp.poster_relative_path) AS poster_relative_path,
                COALESCE(p.poster_local_path, sp.poster_local_path) AS poster_local_path
            FROM app.catalog_episodes AS e
            LEFT JOIN app.catalog_titles AS s ON s.tconst = e.series_tconst
            LEFT JOIN app.latest_title_posters AS p ON p.tconst = e.episode_tconst
            LEFT JOIN app.latest_title_posters AS sp ON sp.tconst = e.series_tconst
            WHERE e.episode_tconst = ANY(%s)
            """,
            (tconsts, tconsts),
        )
        return cursor.fetchall()


def fetch_watch_view_page_rows(
    *,
    limit: int,
    offset: int,
    cutoff_days: int | None,
) -> tuple[int, list[tuple[Any, ...]]]:
    """Read grouped watched/recently-watched rows directly from PostgreSQL."""

    cutoff_filter = ""
    params: list[Any] = []
    if cutoff_days is not None:
        cutoff_filter = "AND w.watched_on >= current_date - (%s * INTERVAL '1 day')"
        params.append(cutoff_days)

    total_sql = f"""
        WITH grouped AS (
            SELECT
                w.display_tconst,
                w.latest_created_at,
                w.latest_watched_on
            FROM app.watched_display_rollup AS w
            WHERE w.display_tconst IS NOT NULL
              {cutoff_filter}
        )
        SELECT COUNT(*)
        FROM grouped AS g
        JOIN app.catalog_title_cards AS c ON c.tconst = g.display_tconst
        WHERE COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL
    """
    rows_sql = f"""
        WITH grouped AS (
            SELECT
                w.display_tconst,
                w.latest_created_at,
                w.latest_watched_on
            FROM app.watched_display_rollup AS w
            WHERE w.display_tconst IS NOT NULL
              {cutoff_filter}
        )
        SELECT
            c.tconst,
            c.title_type,
            c.primary_title,
            c.start_year,
            c.poster_relative_path,
            c.poster_local_path,
            g.latest_watched_on,
            g.latest_created_at
        FROM grouped AS g
        JOIN app.catalog_title_cards AS c ON c.tconst = g.display_tconst
        WHERE COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL
        ORDER BY g.latest_created_at DESC NULLS LAST, c.primary_title
        LIMIT %s OFFSET %s
    """

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(total_sql, params)
        total = int(cursor.fetchone()[0] or 0)
        cursor.execute(rows_sql, [*params, limit, offset])
        rows = cursor.fetchall()
    return total, rows


def fetch_hot_watchlist_page_rows(
    *,
    hot_limit: int,
    limit: int,
    offset: int,
) -> tuple[int, list[tuple[Any, ...]]]:
    """Read grouped hot-watchlist rows directly from PostgreSQL."""

    total_sql = """
        WITH watched_titles AS (
            SELECT display_tconst
            FROM app.watched_display_rollup
            WHERE display_tconst IS NOT NULL
        ),
        ranked_items AS (
            SELECT
                i.display_tconst,
                row_number() OVER (
                    PARTITION BY i.display_tconst
                    ORDER BY i.added_at DESC NULLS LAST, i.updated_at DESC, COALESCE(i.title, i.parent_title, i.tconst)
                ) AS group_row
            FROM app.active_user_list_display_items AS i
            WHERE i.list_id = 'watchlist'
        )
        SELECT COUNT(*)
        FROM (
            SELECT DISTINCT r.display_tconst
            FROM ranked_items AS r
            JOIN app.catalog_title_cards AS c ON c.tconst = r.display_tconst
            LEFT JOIN watched_titles AS wt ON wt.display_tconst = r.display_tconst
            WHERE r.group_row = 1
              AND r.display_tconst IS NOT NULL
              AND COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL
              AND wt.display_tconst IS NULL
            LIMIT %s
        ) AS grouped
    """
    rows_sql = """
        WITH watched_titles AS (
            SELECT display_tconst
            FROM app.watched_display_rollup
            WHERE display_tconst IS NOT NULL
        ),
        ranked_items AS (
            SELECT
                i.display_tconst,
                i.media_type,
                i.title,
                i.parent_title,
                i.rank,
                i.added_at,
                i.notes,
                row_number() OVER (
                    PARTITION BY i.display_tconst
                    ORDER BY i.added_at DESC NULLS LAST, i.updated_at DESC, COALESCE(i.title, i.parent_title, i.tconst)
                ) AS group_row
            FROM app.active_user_list_display_items AS i
            WHERE i.list_id = 'watchlist'
        ),
        grouped_items AS (
            SELECT
                r.display_tconst,
                r.media_type,
                r.title,
                r.parent_title,
                NULL AS season_number,
                NULL AS episode_number,
                r.rank,
                r.added_at,
                r.notes
            FROM ranked_items AS r
            JOIN app.catalog_title_cards AS c ON c.tconst = r.display_tconst
            LEFT JOIN watched_titles AS wt ON wt.display_tconst = r.display_tconst
            WHERE r.group_row = 1
              AND r.display_tconst IS NOT NULL
              AND COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL
              AND wt.display_tconst IS NULL
            ORDER BY r.added_at DESC NULLS LAST, COALESCE(r.title, r.parent_title, r.display_tconst)
            LIMIT %s
        )
        SELECT
            g.display_tconst,
            g.media_type,
            g.title,
            g.parent_title,
            g.season_number,
            g.episode_number,
            g.rank,
            g.added_at,
            g.notes,
            'Hot Watchlist' AS name,
            'view' AS list_kind,
            c.title_type,
            c.start_year,
            c.poster_relative_path,
            c.poster_local_path,
            c.primary_title
        FROM grouped_items AS g
        JOIN app.catalog_title_cards AS c ON c.tconst = g.display_tconst
        ORDER BY g.added_at DESC NULLS LAST, COALESCE(g.title, g.parent_title, g.display_tconst)
        LIMIT %s OFFSET %s
    """
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(total_sql, (hot_limit,))
        total = int(cursor.fetchone()[0] or 0)
        cursor.execute(rows_sql, (hot_limit, limit, offset))
        rows = cursor.fetchall()
    return total, rows


def fetch_library_status_projection(*, recently_watched_days: int, hot_watchlist_limit: int) -> dict[str, int]:
    """Read grouped library-status counts directly from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            WITH grouped_list_items AS (
                SELECT i.list_id, i.display_tconst
                FROM app.active_user_list_display_items AS i
            ),
            list_counts AS (
                SELECT list_id, COUNT(DISTINCT display_tconst) AS item_count
                FROM grouped_list_items
                WHERE display_tconst IS NOT NULL
                GROUP BY list_id
            ),
            poster_tconsts AS (
                SELECT tconst
                FROM app.catalog_title_cards
                WHERE COALESCE(poster_relative_path, poster_local_path) IS NOT NULL
            ),
            watchlist_unwatched AS (
                SELECT DISTINCT g.display_tconst
                FROM grouped_list_items AS g
                LEFT JOIN app.watched_display_rollup AS w ON w.display_tconst = g.display_tconst
                WHERE g.list_id = 'watchlist'
                  AND g.display_tconst IS NOT NULL
                  AND w.display_tconst IS NULL
            )
            SELECT
                COALESCE((SELECT item_count FROM list_counts WHERE list_id = 'watchlist'), 0) AS watchlist_item_count,
                COALESCE((
                    SELECT COUNT(*)
                    FROM watchlist_unwatched AS wu
                    JOIN poster_tconsts AS p ON p.tconst = wu.display_tconst
                ), 0) AS watchlist_unwatched_with_poster_count,
                COALESCE((
                    SELECT COUNT(*)
                    FROM app.watched_display_rollup AS w
                    JOIN poster_tconsts AS p ON p.tconst = w.display_tconst
                ), 0) AS watched_with_poster_count,
                COALESCE((
                    SELECT COUNT(*)
                    FROM app.watched_display_rollup AS w
                    JOIN poster_tconsts AS p ON p.tconst = w.display_tconst
                    WHERE w.latest_watched_on >= current_date - (%s * INTERVAL '1 day')
                ), 0) AS recently_watched_with_poster_count
            """,
            (recently_watched_days,),
        )
        row = cursor.fetchone()

    watchlist_count = int(row[0] or 0)
    hot_watchlist_with_poster_count = int(row[1] or 0)
    watched_count = int(row[2] or 0)
    recently_watched_count = int(row[3] or 0)
    return {
        "watchlist_count": watchlist_count,
        "hot_watchlist_count": min(hot_watchlist_with_poster_count, hot_watchlist_limit),
        "watched_count": watched_count,
        "recently_watched_count": recently_watched_count,
    }


def fetch_library_status_snapshot(*, recently_watched_days: int, hot_watchlist_limit: int) -> dict[str, Any]:
    """Read homepage/library summary counts and base-list item counts directly from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            WITH grouped_list_items AS (
                SELECT i.list_id, i.display_tconst
                FROM app.active_user_list_display_items AS i
            ),
            list_counts AS (
                SELECT list_id, COUNT(DISTINCT display_tconst) AS item_count
                FROM grouped_list_items
                WHERE display_tconst IS NOT NULL
                GROUP BY list_id
            ),
            poster_tconsts AS (
                SELECT tconst
                FROM app.catalog_title_cards
                WHERE COALESCE(poster_relative_path, poster_local_path) IS NOT NULL
            ),
            watchlist_unwatched AS (
                SELECT DISTINCT g.display_tconst
                FROM grouped_list_items AS g
                LEFT JOIN app.watched_display_rollup AS w ON w.display_tconst = g.display_tconst
                WHERE g.list_id = 'watchlist'
                  AND g.display_tconst IS NOT NULL
                  AND w.display_tconst IS NULL
            )
            SELECT
                (SELECT COUNT(*) FROM app.user_lists) AS lists_count,
                (SELECT COUNT(*) FROM app.user_list_items WHERE is_archived = FALSE) AS list_items_count,
                (SELECT COUNT(*) FROM app.user_ratings) AS ratings_count,
                (SELECT COUNT(*) FROM app.user_people WHERE is_favorite = TRUE) AS favorite_people_count,
                (SELECT COUNT(*) FROM app.watch_events) AS watch_events_count,
                COALESCE((SELECT item_count FROM list_counts WHERE list_id = 'watchlist'), 0) AS watchlist_count,
                COALESCE((
                    SELECT COUNT(*)
                    FROM watchlist_unwatched AS wu
                    JOIN poster_tconsts AS p ON p.tconst = wu.display_tconst
                ), 0) AS hot_watchlist_candidate_count,
                COALESCE((
                    SELECT COUNT(*)
                    FROM app.watched_display_rollup AS w
                    JOIN poster_tconsts AS p ON p.tconst = w.display_tconst
                ), 0) AS watched_count,
                COALESCE((
                    SELECT COUNT(*)
                    FROM app.watched_display_rollup AS w
                    JOIN poster_tconsts AS p ON p.tconst = w.display_tconst
                    WHERE w.latest_watched_on >= current_date - (%s * INTERVAL '1 day')
                ), 0) AS recently_watched_count
            """,
            (recently_watched_days,),
        )
        counts_row = cursor.fetchone()

        cursor.execute(
            """
            WITH grouped_list_items AS (
                SELECT
                    i.list_id,
                    COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AS display_tconst
                FROM app.user_list_items AS i
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
                WHERE i.is_archived = FALSE
            ),
            list_counts AS (
                SELECT list_id, COUNT(DISTINCT display_tconst) AS item_count
                FROM grouped_list_items
                WHERE display_tconst IS NOT NULL
                GROUP BY list_id
            )
            SELECT
                l.id,
                l.slug,
                l.name,
                l.description,
                l.list_kind,
                COALESCE(c.item_count, 0) AS item_count
            FROM app.user_lists AS l
            LEFT JOIN list_counts AS c ON c.list_id = l.id
            ORDER BY l.list_kind, l.name
            """
        )
        list_rows = cursor.fetchall()

    return {
        "counts": {
            "lists": int(counts_row[0] or 0),
            "list_items": int(counts_row[1] or 0),
            "ratings": int(counts_row[2] or 0),
            "favorite_people": int(counts_row[3] or 0),
            "watch_events": int(counts_row[4] or 0),
            "watchlist_items": int(counts_row[5] or 0),
        },
        "watchlist_count": int(counts_row[5] or 0),
        "hot_watchlist_count": min(int(counts_row[6] or 0), hot_watchlist_limit),
        "watched_count": int(counts_row[7] or 0),
        "recently_watched_count": int(counts_row[8] or 0),
        "base_lists": [
            {
                "id": row[0],
                "slug": row[1],
                "name": row[2],
                "description": row[3],
                "list_kind": row[4],
                "item_count": int(row[5] or 0),
                "item_type": "list",
            }
            for row in list_rows
        ],
    }


def fetch_home_suggestion_candidate_rows(
    *,
    min_start_year: int,
    primary_locale: str = "cs-CZ",
    fallback_locale: str = "en-US",
) -> list[tuple[Any, ...]]:
    """Read compact unwatched homepage suggestion candidates directly from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            WITH latest_tmdb_details AS (
                SELECT ranked.tconst, ranked.overview, ranked.release_date
                FROM (
                    SELECT
                        d.tconst,
                        d.overview,
                        d.release_date,
                        row_number() OVER (
                        PARTITION BY d.tconst
                        ORDER BY
                            CASE d.locale
                                WHEN %s THEN 0
                                WHEN %s THEN 1
                                ELSE 2
                            END,
                                d.synced_at DESC
                        ) AS rn
                    FROM app.tmdb_title_details AS d
                ) AS ranked
                WHERE ranked.rn = 1
            ),
            cz_provider_stats AS (
                SELECT tconst, COUNT(*) AS cz_provider_count
                FROM app.tmdb_watch_providers
                WHERE country_code = 'CZ'
                GROUP BY tconst
            ),
            title_watch_events AS (
                SELECT
                    COALESCE(e.series_tconst, w.tconst) AS tconst,
                    COALESCE(w.created_at, w.watched_on::timestamp) AS watched_at
                FROM app.watch_events AS w
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
                WHERE w.tconst IS NOT NULL
            ),
            title_watch_stats AS (
                SELECT
                    tconst,
                    COUNT(*) AS watch_count
                FROM title_watch_events
                GROUP BY tconst
            ),
            rated_cast_affinity AS (
                SELECT
                    c.tconst,
                    SUM(p.affinity_rating::double precision * CASE
                        WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0
                        ELSE 1.0 / sqrt(c.ordering::double precision)
                    END)
                    / NULLIF(
                        SUM(CASE
                            WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0
                            ELSE 1.0 / sqrt(c.ordering::double precision)
                        END),
                        0.0
                    ) AS actor_affinity_rating
                FROM app.title_credits AS c
                JOIN app.user_people AS p ON p.nconst = c.nconst
                WHERE c.credit_group = 'cast'
                  AND p.affinity_rating > 0
                  AND (c.ordering IS NULL OR c.ordering <= 8)
                GROUP BY c.tconst
            )
            SELECT
                t.tconst,
                t.title_type,
                t.primary_title,
                t.start_year,
                t.genres,
                t.average_rating,
                t.num_votes,
                d.overview,
                d.release_date,
                COALESCE(p.cz_provider_count, 0) AS cz_provider_count,
                COALESCE(w.watch_count, 0) AS watch_count,
                a.actor_affinity_rating
            FROM app.catalog_titles AS t
            LEFT JOIN latest_tmdb_details AS d ON d.tconst = t.tconst
            LEFT JOIN cz_provider_stats AS p ON p.tconst = t.tconst
            LEFT JOIN title_watch_stats AS w ON w.tconst = t.tconst
            LEFT JOIN rated_cast_affinity AS a ON a.tconst = t.tconst
            WHERE COALESCE(w.watch_count, 0) = 0
              AND (
                    COALESCE(length(trim(d.overview)), 0) > 0
                    OR COALESCE(NULLIF(d.release_date::text, '')::date >= current_date - INTERVAL '540 day', FALSE)
                    OR COALESCE(t.start_year, 0) >= %s
                  )
            ORDER BY
                COALESCE(t.start_year, 0) DESC,
                COALESCE(t.num_votes, 0) DESC,
                COALESCE(t.average_rating, 0.0) DESC,
                t.primary_title
            LIMIT 3000
            """,
            (primary_locale, fallback_locale, min_start_year),
        )
        return cursor.fetchall()


def fetch_content_state(tconst: str) -> dict[str, Any] | None:
    """Read one content_state row from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                tconst,
                interest_state,
                last_previewed_at,
                last_watched_at,
                updated_at
            FROM app.content_state
            WHERE tconst = %s
            """,
            (tconst,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_content_state(row)


def update_content_state(tconst: str, interest_state: str, now: str) -> dict[str, Any]:
    """Upsert one content_state row in PostgreSQL and return the stored state."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.content_state (tconst, interest_state, last_previewed_at, last_watched_at, updated_at)
            VALUES (
                %s,
                %s,
                CASE WHEN %s = 'previewed' THEN %s::timestamp ELSE NULL END,
                CASE WHEN %s = 'watched' THEN %s::timestamp ELSE NULL END,
                %s::timestamp
            )
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
            RETURNING
                tconst,
                interest_state,
                last_previewed_at,
                last_watched_at,
                updated_at
            """,
            (tconst, interest_state, interest_state, now, interest_state, now, now),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL content_state upsert nevrátil žádný řádek.")
        conn.commit()
    return _row_to_content_state(row)


def list_in_progress_content_states(limit: int | None = None) -> list[dict[str, Any]]:
    """Return ordered in-progress content_state rows from PostgreSQL."""

    sql = """
        SELECT
            tconst,
            interest_state,
            last_previewed_at,
            last_watched_at,
            updated_at
        FROM app.content_state
        WHERE interest_state = 'in_progress'
        ORDER BY COALESCE(last_previewed_at, updated_at) DESC, COALESCE(last_watched_at, updated_at) DESC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += "\nLIMIT %s"
        params = (limit,)
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    return [_row_to_content_state(row) for row in rows]


def upsert_user_rating(
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
    liked_notes: str | None = None,
    disliked_notes: str | None = None,
) -> dict[str, Any]:
    """Upsert one app.user_ratings row in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.user_ratings (
                canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title, title,
                season_number, episode_number, rating, liked_notes, disliked_notes, rated_at,
                source_origin, source_ref, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamp, %s, %s, %s::timestamp, %s::timestamp)
            ON CONFLICT (canonical_key) DO UPDATE SET
                tconst = COALESCE(app.user_ratings.tconst, excluded.tconst),
                imdb_id = COALESCE(app.user_ratings.imdb_id, excluded.imdb_id),
                tmdb_id = COALESCE(app.user_ratings.tmdb_id, excluded.tmdb_id),
                trakt_id = COALESCE(app.user_ratings.trakt_id, excluded.trakt_id),
                parent_tconst = COALESCE(app.user_ratings.parent_tconst, excluded.parent_tconst),
                parent_title = COALESCE(app.user_ratings.parent_title, excluded.parent_title),
                title = COALESCE(app.user_ratings.title, excluded.title),
                rating = excluded.rating,
                liked_notes = excluded.liked_notes,
                disliked_notes = excluded.disliked_notes,
                rated_at = COALESCE(excluded.rated_at, app.user_ratings.rated_at),
                updated_at = excluded.updated_at
            RETURNING
                canonical_key,
                tconst,
                rating,
                liked_notes,
                disliked_notes,
                rated_at,
                updated_at
            """,
            (
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
                liked_notes,
                disliked_notes,
                rated_at,
                source_origin,
                source_ref,
                now,
                now,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL user_ratings upsert nevrátil žádný řádek.")
        conn.commit()
    return _row_to_user_rating(row)


def delete_user_rating(canonical_key: str) -> None:
    """Delete one app.user_ratings row by canonical key in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("DELETE FROM app.user_ratings WHERE canonical_key = %s", (canonical_key,))
        conn.commit()


def fetch_latest_rating_for_tconst(tconst: str) -> dict[str, Any] | None:
    """Read the latest user rating for one title/episode tconst from PostgreSQL."""

    rows = fetch_latest_ratings_for_tconsts([tconst])
    return rows.get(tconst)


def fetch_latest_ratings_for_tconsts(tconsts: list[str]) -> dict[str, dict[str, Any]]:
    """Read latest user ratings for multiple tconsts from PostgreSQL."""

    clean_tconsts = [str(tconst).strip() for tconst in tconsts if str(tconst).strip()]
    if not clean_tconsts:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ON (tconst)
                tconst,
                rating,
                liked_notes,
                disliked_notes,
                rated_at,
                updated_at
            FROM app.user_ratings
            WHERE tconst = ANY(%s)
              AND tconst IS NOT NULL
            ORDER BY tconst, rated_at DESC NULLS LAST, updated_at DESC, created_at DESC
            """,
            (clean_tconsts,),
        )
        rows = cursor.fetchall()
    return {
        str(row[0]): {
            "tconst": row[0],
            "rating": row[1],
            "liked_notes": row[2],
            "disliked_notes": row[3],
            "rated_at": _parse_optional_timestamp(row[4]),
            "updated_at": _parse_optional_timestamp(row[5]),
        }
        for row in rows
        if row[0] is not None
    }


def fetch_ai_taste_seed_rows(*, source_list: str, limit: int) -> dict[str, Any]:
    """Return compact user-taste examples for an external AI recommender.

    The source list can be a user-list id, slug, or exact name. The payload is
    intentionally read-only and carries local signals; it does not decide new
    recommendations inside this app.
    """

    normalized_source = str(source_list or "").strip()
    if not normalized_source:
        normalized_source = "kouknout-znovu"
    source_aliases = [normalized_source]
    if normalized_source.casefold() == "kouknout-znovu":
        source_aliases.append("kouknout-znou")
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, slug, name, description, list_kind
            FROM app.user_lists
            WHERE id = ANY(%s)
               OR slug = ANY(%s)
               OR lower(name) = ANY(%s)
            ORDER BY
                CASE
                    WHEN id = %s THEN 0
                    WHEN slug = %s THEN 1
                    ELSE 2
                END
            LIMIT 1
            """,
            (
                source_aliases,
                source_aliases,
                [item.casefold().replace("-", " ") for item in source_aliases],
                normalized_source,
                normalized_source,
            ),
        )
        list_row = cursor.fetchone()
        if list_row is None:
            return {
                "source_list": {
                    "query": normalized_source,
                    "found": False,
                },
                "items": [],
                "limit": limit,
            }

        cursor.execute(
            """
            WITH ranked_items AS (
                SELECT
                    i.display_tconst,
                    i.tmdb_id,
                    i.added_at,
                    i.rank,
                    row_number() OVER (
                        PARTITION BY i.display_tconst
                        ORDER BY i.rank NULLS LAST, i.added_at DESC NULLS LAST, COALESCE(i.title, i.parent_title, i.tconst)
                    ) AS group_row
                FROM app.active_user_list_display_items AS i
                WHERE i.list_id = %s
                  AND i.display_tconst IS NOT NULL
            ),
            latest_ratings AS (
                SELECT DISTINCT ON (tconst)
                    tconst,
                    rating,
                    liked_notes,
                    disliked_notes,
                    rated_at,
                    updated_at
                FROM app.user_ratings
                WHERE tconst IS NOT NULL
                ORDER BY tconst, rated_at DESC NULLS LAST, updated_at DESC, created_at DESC
            ),
            latest_scores AS (
                SELECT DISTINCT ON (genre)
                    genre,
                    final_score,
                    rating_signal_score,
                    watch_signal_score,
                    actor_affinity_score,
                    generated_at
                FROM app.genre_scores
                WHERE score_scope = 'default'
                ORDER BY genre, generated_at DESC, rank_in_run ASC
            ),
            title_actor_affinity AS (
                SELECT
                    c.tconst,
                    ROUND(AVG(p.affinity_rating)::numeric, 3)::double precision AS actor_affinity_rating
                FROM app.title_credits AS c
                JOIN app.user_people AS p ON p.nconst = c.nconst
                WHERE c.credit_group = 'cast'
                  AND p.affinity_rating > 0
                  AND (c.ordering IS NULL OR c.ordering <= 8)
                GROUP BY c.tconst
            )
            SELECT
                r.display_tconst,
                t.primary_title,
                t.original_title,
                t.title_type,
                t.start_year,
                t.genres,
                t.average_rating,
                t.num_votes,
                COALESCE(map.tmdb_id, r.tmdb_id),
                lr.rating,
                lr.liked_notes,
                lr.disliked_notes,
                lr.rated_at,
                ta.actor_affinity_rating,
                COALESCE(
                    jsonb_agg(
                        DISTINCT jsonb_build_object(
                            'genre', score.genre,
                            'final_score', score.final_score,
                            'rating_signal_score', score.rating_signal_score,
                            'watch_signal_score', score.watch_signal_score,
                            'actor_affinity_score', score.actor_affinity_score
                        )
                    ) FILTER (WHERE score.genre IS NOT NULL),
                    '[]'::jsonb
                ) AS genre_score_signals
            FROM ranked_items AS r
            JOIN app.catalog_titles AS t ON t.tconst = r.display_tconst
            LEFT JOIN app.tmdb_title_map AS map ON map.tconst = r.display_tconst
            LEFT JOIN latest_ratings AS lr ON lr.tconst = r.display_tconst
            LEFT JOIN title_actor_affinity AS ta ON ta.tconst = r.display_tconst
            LEFT JOIN latest_scores AS score ON score.genre = ANY(string_to_array(COALESCE(t.genres, ''), ','))
            WHERE r.group_row = 1
            GROUP BY
                r.display_tconst,
                t.primary_title,
                t.original_title,
                t.title_type,
                t.start_year,
                t.genres,
                t.average_rating,
                t.num_votes,
                map.tmdb_id,
                r.tmdb_id,
                lr.rating,
                lr.liked_notes,
                lr.disliked_notes,
                lr.rated_at,
                ta.actor_affinity_rating,
                r.rank,
                r.added_at
            ORDER BY r.rank NULLS LAST, r.added_at DESC NULLS LAST, t.primary_title
            LIMIT %s
            """,
            (list_row[0], limit),
        )
        rows = cursor.fetchall()

    return {
        "source_list": {
            "query": normalized_source,
            "found": True,
            "id": list_row[0],
            "slug": list_row[1],
            "name": list_row[2],
            "description": list_row[3],
            "list_kind": list_row[4],
        },
        "limit": limit,
        "items": [
            {
                "imdb_id": row[0],
                "tconst": row[0],
                "tmdb_id": row[8],
                "title": row[1],
                "original_title": row[2],
                "title_type": row[3],
                "year": row[4],
                "genres": [genre for genre in str(row[5] or "").split(",") if genre],
                "imdb_rating": row[6],
                "imdb_votes": row[7],
                "user_rating": row[9],
                "liked_notes": row[10],
                "disliked_notes": row[11],
                "rated_at": _parse_optional_timestamp(row[12]),
                "actor_affinity_rating": row[13],
                "genre_score_signals": row[14] or [],
            }
            for row in rows
        ],
    }


def insert_watch_event(
    *,
    event_id: str,
    tconst: str,
    event_scope: str,
    watched_on: str,
    notes: str | None,
    created_at: str,
) -> dict[str, Any]:
    """Insert one local watch event and sync content_state in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.watch_events (
                id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
            )
            VALUES (%s, %s, %s, %s::date, 'local_app', NULL, NULL, NULL, %s, %s::timestamp)
            RETURNING id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
            """,
            (event_id, tconst, event_scope, watched_on, notes, created_at),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL watch_events insert nevrátil žádný řádek.")
        cursor.execute(
            """
            INSERT INTO app.content_state (tconst, interest_state, last_previewed_at, last_watched_at, updated_at)
            VALUES (%s, 'watched', NULL, %s::timestamp, %s::timestamp)
            ON CONFLICT (tconst) DO UPDATE SET
                interest_state = 'watched',
                last_watched_at = excluded.last_watched_at,
                updated_at = excluded.updated_at
            """,
            (tconst, created_at, created_at),
        )
        conn.commit()
    return _row_to_watch_event(row)


def record_watched(
    *,
    event_id: str,
    tconst: str,
    event_scope: str,
    watched_on: str,
    notes: str | None,
    created_at: str,
    archive_from_list_id: str | None = None,
    archive_canonical_key: str | None = None,
    archive_display_tconst: str | None = None,
) -> dict[str, Any]:
    """Run the server-side watched action in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_id, content_state_changed, archived_items
            FROM app.record_watched(
                %s,
                %s,
                %s,
                %s::date,
                %s,
                %s::timestamp,
                %s,
                %s,
                %s
            )
            """,
            (
                event_id,
                tconst,
                event_scope,
                watched_on,
                notes,
                created_at,
                archive_from_list_id,
                archive_canonical_key,
                archive_display_tconst,
            ),
        )
        row = cursor.fetchone()
        conn.commit()
    if row is None:
        raise RuntimeError("PostgreSQL record_watched nevratil vysledek.")
    return {
        "event_id": str(row[0]),
        "content_state_changed": bool(row[1]),
        "archived_items": int(row[2] or 0),
    }


def insert_watch_events(
    events: list[dict[str, Any]],
    *,
    created_at: str,
) -> list[dict[str, Any]]:
    """Insert multiple local watch events and sync their content_state rows."""

    if not events:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        inserted: list[dict[str, Any]] = []
        for event in events:
            cursor.execute(
                """
                INSERT INTO app.watch_events (
                    id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
                )
                VALUES (%s, %s, %s, %s::date, 'local_app', NULL, NULL, NULL, %s, %s::timestamp)
                RETURNING id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
                """,
                (
                    event["id"],
                    event["tconst"],
                    event["event_scope"],
                    event["watched_on"],
                    event.get("notes"),
                    created_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("PostgreSQL watch_events batch insert nevrátil řádek.")
            inserted.append(_row_to_watch_event(row))
        cursor.executemany(
            """
            INSERT INTO app.content_state (tconst, interest_state, last_previewed_at, last_watched_at, updated_at)
            VALUES (%s, 'watched', NULL, %s::timestamp, %s::timestamp)
            ON CONFLICT (tconst) DO UPDATE SET
                interest_state = 'watched',
                last_watched_at = excluded.last_watched_at,
                updated_at = excluded.updated_at
            """,
            [(event["tconst"], created_at, created_at) for event in events],
        )
        conn.commit()
    return inserted


def fetch_watch_history(limit: int = 100, source: str | None = None) -> list[dict[str, Any]]:
    """Read latest watch history rows from PostgreSQL."""

    sql = """
        SELECT id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
        FROM app.watch_events
        {where_clause}
        ORDER BY watched_on DESC, created_at DESC
        LIMIT %s
    """
    where_clause = "" if source is None else "WHERE source = %s"
    params: tuple[Any, ...] = (limit,) if source is None else (source, limit)
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(sql.format(where_clause=where_clause), params)
        rows = cursor.fetchall()
    return [_row_to_watch_event(row) for row in rows]


def fetch_existing_watch_tconsts(tconsts: list[str]) -> set[str]:
    """Return which of the given tconsts already have a watch event in PostgreSQL."""

    clean_tconsts = [str(tconst).strip() for tconst in tconsts if str(tconst).strip()]
    if not clean_tconsts:
        return set()
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT tconst FROM app.watch_events WHERE tconst = ANY(%s)",
            (clean_tconsts,),
        )
        rows = cursor.fetchall()
    return {str(row[0]) for row in rows if row[0] is not None}


def fetch_watch_stats_for_tconsts(tconsts: list[str]) -> dict[str, dict[str, Any]]:
    """Return count and last watch timestamp for the given raw event tconsts."""

    clean_tconsts = [str(tconst).strip() for tconst in tconsts if str(tconst).strip()]
    if not clean_tconsts:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                tconst,
                COUNT(*) AS watched_count,
                MAX(created_at) AS last_watched_at
            FROM app.watch_events
            WHERE tconst = ANY(%s)
            GROUP BY tconst
            """,
            (clean_tconsts,),
        )
        rows = cursor.fetchall()
    return {
        str(row[0]): {
            "tconst": row[0],
            "watched_count": int(row[1]),
            "last_watched_at": _parse_optional_timestamp(row[2]),
        }
        for row in rows
        if row[0] is not None
    }


def fetch_library_summary_snapshot(tconst: str, title_type: str | None) -> dict[str, Any]:
    """Return one PostgreSQL-backed library summary for a title or episode."""

    if title_type in ("tvSeries", "tvMiniSeries"):
        watch_sql = """
            SELECT COUNT(*), MAX(w.created_at)
            FROM app.watch_events AS w
            JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
            WHERE e.series_tconst = %s
        """
    else:
        watch_sql = """
            SELECT COUNT(*), MAX(created_at)
            FROM app.watch_events
            WHERE tconst = %s
        """

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(watch_sql, (tconst,))
        watch_row = cursor.fetchone()
        watched_count = int((watch_row[0] if watch_row is not None else 0) or 0)
        last_watched_at = _parse_optional_timestamp(watch_row[1] if watch_row is not None else None)

        cursor.execute(
            """
            SELECT l.name, l.list_kind, i.rank, i.added_at
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            WHERE i.tconst = %s
              AND i.is_archived = FALSE
              AND l.list_kind <> 'watchlist'
            ORDER BY
                CASE WHEN i.rank IS NULL THEN 1 ELSE 0 END,
                i.rank,
                i.added_at DESC NULLS LAST,
                l.name
            LIMIT 20
            """,
            (tconst,),
        )
        list_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT EXISTS(
                SELECT 1
                FROM app.user_list_items AS i
                JOIN app.user_lists AS l ON l.id = i.list_id
                WHERE i.tconst = %s
                  AND i.is_archived = FALSE
                  AND l.list_kind = 'watchlist'
            )
            """,
            (tconst,),
        )
        watchlist_row = cursor.fetchone()
        raw_in_watchlist = bool(watchlist_row[0]) if watchlist_row is not None else False

        cursor.execute(
            """
            SELECT rating, liked_notes, disliked_notes, rated_at
            FROM app.user_ratings
            WHERE tconst = %s
            ORDER BY rated_at DESC NULLS LAST, updated_at DESC, created_at DESC
            LIMIT 1
            """,
            (tconst,),
        )
        rating_row = cursor.fetchone()

    return {
        "watched_count": watched_count,
        "last_watched_at": last_watched_at,
        "in_watchlist": raw_in_watchlist and watched_count == 0,
        "rating": (
            {
                "value": rating_row[0],
                "liked_notes": rating_row[1],
                "disliked_notes": rating_row[2],
                "rated_at": _parse_optional_timestamp(rating_row[3]),
            }
            if rating_row is not None
            else None
        ),
        "lists": [
            {
                "name": row[0],
                "kind": row[1],
                "rank": row[2],
                "added_at": _parse_optional_timestamp(row[3]),
            }
            for row in list_rows
        ],
    }


def fetch_all_watch_events(source: str | None = None) -> list[dict[str, Any]]:
    """Return all watch events ordered newest-first for overlay/aggregation use."""

    sql = """
        SELECT id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at
        FROM app.watch_events
        {where_clause}
        ORDER BY watched_on DESC, created_at DESC
    """
    where_clause = "" if source is None else "WHERE source = %s"
    params: tuple[Any, ...] = () if source is None else (source,)
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(sql.format(where_clause=where_clause), params)
        rows = cursor.fetchall()
    return [_row_to_watch_event(row) for row in rows]


def fetch_user_list(list_id: str) -> dict[str, Any] | None:
    """Read one user list row from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, slug, name, description, list_kind
            FROM app.user_lists
            WHERE id = %s
            """,
            (list_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "slug": row[1],
        "name": row[2],
        "description": row[3],
        "list_kind": row[4],
    }


def fetch_user_list_page_rows(
    *,
    list_id: str,
    limit: int,
    offset: int,
    exclude_watched: bool,
) -> tuple[dict[str, Any] | None, int, list[tuple[Any, ...]]]:
    """Read one grouped user-list page directly from PostgreSQL.

    The normal path gets page rows and the filtered total in one scan. A small
    count fallback is kept only for out-of-range offsets where a window count
    cannot be returned with the empty page.
    """

    watched_cte = ""
    watched_join = ""
    watched_filter = ""
    if exclude_watched:
        watched_cte = """
            ,
            watched_titles AS (
                SELECT display_tconst
                FROM app.watched_display_rollup
                WHERE display_tconst IS NOT NULL
            )
        """
        watched_join = "LEFT JOIN watched_titles AS wt ON wt.display_tconst = r.display_tconst"
        watched_filter = "AND wt.display_tconst IS NULL"

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, slug, name, description, list_kind
            FROM app.user_lists
            WHERE id = %s
            """,
            (list_id,),
        )
        list_row = cursor.fetchone()
        if list_row is None:
            return None, 0, []

        count_sql = f"""
            WITH ranked_items AS (
                SELECT
                    i.display_tconst,
                    row_number() OVER (
                        PARTITION BY i.display_tconst
                        ORDER BY i.rank NULLS LAST, i.added_at DESC NULLS LAST, COALESCE(i.title, i.parent_title, i.tconst)
                    ) AS group_row
                FROM app.active_user_list_display_items AS i
                WHERE i.list_id = %s
            )
            {watched_cte}
            SELECT COUNT(*)
            FROM ranked_items AS r
            JOIN app.catalog_title_cards AS c ON c.tconst = r.display_tconst
            {watched_join}
            WHERE r.group_row = 1
              AND r.display_tconst IS NOT NULL
              AND COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL
              {watched_filter}
        """
        rows_sql = f"""
            WITH ranked_items AS (
                SELECT
                    i.display_tconst,
                    i.media_type,
                    i.title,
                    i.parent_title,
                    i.rank,
                    i.added_at,
                    i.notes,
                    i.list_name,
                    i.list_kind,
                    row_number() OVER (
                        PARTITION BY i.display_tconst
                        ORDER BY i.rank NULLS LAST, i.added_at DESC NULLS LAST, COALESCE(i.title, i.parent_title, i.tconst)
                    ) AS group_row
                FROM app.active_user_list_display_items AS i
                WHERE i.list_id = %s
            )
            {watched_cte}
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
                r.list_name,
                r.list_kind,
                c.title_type,
                c.start_year,
                c.poster_relative_path,
                c.poster_local_path,
                c.primary_title,
                COUNT(*) OVER () AS filtered_total
            FROM ranked_items AS r
            JOIN app.catalog_title_cards AS c ON c.tconst = r.display_tconst
            {watched_join}
            WHERE r.group_row = 1
              AND r.display_tconst IS NOT NULL
              AND COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL
              {watched_filter}
            ORDER BY r.rank NULLS LAST, r.added_at DESC NULLS LAST, COALESCE(r.title, r.parent_title, r.display_tconst)
            LIMIT %s OFFSET %s
        """
        cursor.execute(rows_sql, (list_id, limit, offset))
        rows = cursor.fetchall()
        if rows:
            total = int(rows[0][-1] or 0)
            rows = [row[:-1] for row in rows]
        elif offset > 0:
            cursor.execute(count_sql, (list_id,))
            total = int(cursor.fetchone()[0] or 0)
        else:
            total = 0

    return (
        {
            "id": list_row[0],
            "slug": list_row[1],
            "name": list_row[2],
            "description": list_row[3],
            "list_kind": list_row[4],
        },
        total,
        rows,
    )


def fetch_user_lists() -> list[dict[str, Any]]:
    """Read all user lists from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, slug, name, description, list_kind
            FROM app.user_lists
            ORDER BY list_kind, name
            """
        )
        rows = cursor.fetchall()
    return [
        {
            "id": row[0],
            "slug": row[1],
            "name": row[2],
            "description": row[3],
            "list_kind": row[4],
        }
        for row in rows
    ]


def create_user_list(*, list_id: str, slug: str, name: str, description: str | None, now: str) -> dict[str, Any]:
    """Insert one custom user list in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.user_lists (
                id, slug, name, description, list_kind, source_origin, source_ref, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, 'custom', 'local_app', NULL, %s::timestamp, %s::timestamp)
            RETURNING id, slug, name, description, list_kind
            """,
            (list_id, slug, name, description, now, now),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL user_lists insert nevrátil žádný řádek.")
        conn.commit()
    return {
        "id": row[0],
        "slug": row[1],
        "name": row[2],
        "description": row[3],
        "list_kind": row[4],
    }


def update_user_list_description(list_id: str, description: str | None, now: str) -> dict[str, Any] | None:
    """Update description for one PostgreSQL user list."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE app.user_lists
            SET description = %s, updated_at = %s::timestamp
            WHERE id = %s
            RETURNING id, slug, name, description, list_kind
            """,
            (description, now, list_id),
        )
        row = cursor.fetchone()
        conn.commit()
    if row is None:
        return None
    return {
        "id": row[0],
        "slug": row[1],
        "name": row[2],
        "description": row[3],
        "list_kind": row[4],
    }


def delete_user_list(list_id: str) -> dict[str, Any] | None:
    """Delete one custom user list and its items from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, slug, name, description, list_kind
            FROM app.user_lists
            WHERE id = %s
            """,
            (list_id,),
        )
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            return None
        if row[4] != "custom":
            conn.rollback()
            return {
                "id": row[0],
                "slug": row[1],
                "name": row[2],
                "description": row[3],
                "list_kind": row[4],
            }

        cursor.execute("DELETE FROM app.user_list_items WHERE list_id = %s", (list_id,))
        cursor.execute("DELETE FROM app.user_lists WHERE id = %s", (list_id,))
        conn.commit()
    return {
        "id": row[0],
        "slug": row[1],
        "name": row[2],
        "description": row[3],
        "list_kind": row[4],
    }


def slug_exists(slug: str) -> bool:
    """Return whether one user-list slug already exists in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM app.user_lists WHERE slug = %s LIMIT 1", (slug,))
        return cursor.fetchone() is not None


def upsert_user_list_item(
    *,
    item_id: str,
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
    """Upsert one app.user_list_items row in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.user_list_items (
                id, list_id, canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst,
                parent_title, title, season_number, episode_number, rank, added_at, notes, source_origin,
                source_ref, is_archived, created_at, updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamp, %s, %s, %s, FALSE,
                %s::timestamp, %s::timestamp
            )
            ON CONFLICT (list_id, canonical_key) DO UPDATE SET
                tconst = COALESCE(app.user_list_items.tconst, excluded.tconst),
                imdb_id = COALESCE(app.user_list_items.imdb_id, excluded.imdb_id),
                tmdb_id = COALESCE(app.user_list_items.tmdb_id, excluded.tmdb_id),
                trakt_id = COALESCE(app.user_list_items.trakt_id, excluded.trakt_id),
                parent_tconst = COALESCE(app.user_list_items.parent_tconst, excluded.parent_tconst),
                parent_title = COALESCE(app.user_list_items.parent_title, excluded.parent_title),
                title = COALESCE(app.user_list_items.title, excluded.title),
                rank = COALESCE(app.user_list_items.rank, excluded.rank),
                added_at = CASE
                    WHEN app.user_list_items.is_archived THEN COALESCE(excluded.added_at, app.user_list_items.added_at)
                    ELSE COALESCE(app.user_list_items.added_at, excluded.added_at)
                END,
                notes = COALESCE(app.user_list_items.notes, excluded.notes),
                is_archived = FALSE,
                updated_at = excluded.updated_at
            """,
            (
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
            ),
        )
        conn.commit()


def archive_user_list_item(list_id: str, canonical_key: str, now: str) -> None:
    """Archive one list item in PostgreSQL by list/canonical key."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE app.user_list_items
            SET is_archived = TRUE, updated_at = %s::timestamp
            WHERE list_id = %s AND canonical_key = %s
            """,
            (now, list_id, canonical_key),
        )
        conn.commit()


def fetch_user_list_item_counts() -> dict[str, int]:
    """Return aggregate list and active-item counts from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM app.user_lists")
        lists = int(cursor.fetchone()[0])
        cursor.execute("SELECT COUNT(*) FROM app.user_list_items WHERE is_archived = FALSE")
        list_items = int(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM app.user_list_items AS i
            JOIN app.user_lists AS l ON l.id = i.list_id
            WHERE i.is_archived = FALSE AND l.list_kind = 'watchlist'
            """
        )
        watchlist_items = int(cursor.fetchone()[0])
    return {
        "lists": lists,
        "list_items": list_items,
        "watchlist_items": watchlist_items,
    }


def fetch_active_user_list_items() -> list[dict[str, Any]]:
    """Return active user list items from PostgreSQL for read-model assembly."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                id, list_id, canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id,
                parent_tconst, parent_title, title, season_number, episode_number, rank,
                added_at, notes, source_origin, source_ref, created_at, updated_at
            FROM app.user_list_items
            WHERE is_archived = FALSE
            """
        )
        rows = cursor.fetchall()
    return [
        {
            "id": row[0],
            "list_id": row[1],
            "canonical_key": row[2],
            "tconst": row[3],
            "media_type": row[4],
            "imdb_id": row[5],
            "tmdb_id": row[6],
            "trakt_id": row[7],
            "parent_tconst": row[8],
            "parent_title": row[9],
            "title": row[10],
            "season_number": row[11],
            "episode_number": row[12],
            "rank": row[13],
            "added_at": _parse_optional_timestamp(row[14]),
            "notes": row[15],
            "source_origin": row[16],
            "source_ref": row[17],
            "created_at": _parse_optional_timestamp(row[18]),
            "updated_at": _parse_optional_timestamp(row[19]),
        }
        for row in rows
    ]


def fetch_person_affinity_rating(nconst: str) -> int:
    """Return current affinity rating for one person from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT affinity_rating
            FROM app.user_people
            WHERE nconst = %s
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT 1
            """,
            (nconst,),
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def fetch_positive_person_affinities() -> dict[str, int]:
    """Return positive person affinity ratings from PostgreSQL keyed by nconst."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT nconst, affinity_rating
            FROM app.user_people
            WHERE nconst IS NOT NULL AND affinity_rating > 0
            """
        )
        rows = cursor.fetchall()
    return {
        str(row[0]): int(row[1])
        for row in rows
        if row[0] is not None and row[1] is not None
    }


def upsert_person_affinity(
    *,
    person_key: str,
    nconst: str,
    name: str,
    known_for: str | None,
    birth_date: str | None,
    source_ref: str | None,
    is_favorite: bool,
    affinity_rating: int,
    created_at: str,
    updated_at: str,
) -> None:
    """Upsert one user_people row in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.user_people (
                person_key, nconst, name, known_for, birth_date, source_origin, source_ref,
                is_favorite, affinity_rating, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, 'local_app', %s, %s, %s, %s::timestamp, %s::timestamp)
            ON CONFLICT (person_key) DO UPDATE SET
                nconst = excluded.nconst,
                name = excluded.name,
                known_for = COALESCE(app.user_people.known_for, excluded.known_for),
                birth_date = COALESCE(app.user_people.birth_date, excluded.birth_date),
                is_favorite = excluded.is_favorite,
                affinity_rating = excluded.affinity_rating,
                updated_at = excluded.updated_at
            """,
            (
                person_key,
                nconst,
                name,
                known_for,
                birth_date,
                source_ref,
                is_favorite,
                affinity_rating,
                created_at,
                updated_at,
            ),
        )
        conn.commit()


def fetch_favorite_genres(*, active_only: bool) -> list[dict[str, Any]]:
    """Read favorite genres from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                genre, weight, preference_rank, source_origin, source_ref, notes, is_active, created_at, updated_at
            FROM app.favorite_genres
            WHERE (%s = FALSE OR is_active = TRUE)
            ORDER BY preference_rank ASC NULLS LAST, weight DESC, genre ASC
            """,
            (active_only,),
        )
        rows = cursor.fetchall()
    return [
        {
            "genre": row[0],
            "weight": row[1],
            "preference_rank": row[2],
            "source_origin": row[3],
            "source_ref": row[4],
            "notes": row[5],
            "is_active": row[6],
            "created_at": _parse_optional_timestamp(row[7]),
            "updated_at": _parse_optional_timestamp(row[8]),
        }
        for row in rows
    ]


def replace_favorite_genres(
    *,
    items: list[dict[str, Any]],
    source_origin: str,
    source_ref: str | None,
    archive_missing: bool,
    now: str,
) -> None:
    """Replace favorite genres in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT touched_count, archived_count
            FROM app.replace_favorite_genres(
                %s::jsonb,
                %s,
                %s,
                %s,
                %s::timestamp
            )
            """,
            (
                json.dumps(items),
                source_origin,
                source_ref,
                archive_missing,
                now,
            ),
        )
        conn.commit()


def fetch_favorite_traits(*, active_only: bool) -> list[dict[str, Any]]:
    """Read favorite traits from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                trait, weight, preference_rank, source_origin, source_ref, notes, is_active, created_at, updated_at
            FROM app.favorite_traits
            WHERE (%s = FALSE OR is_active = TRUE)
            ORDER BY preference_rank ASC NULLS LAST, weight DESC, trait ASC
            """,
            (active_only,),
        )
        rows = cursor.fetchall()
    return [
        {
            "trait": row[0],
            "weight": row[1],
            "preference_rank": row[2],
            "source_origin": row[3],
            "source_ref": row[4],
            "notes": row[5],
            "is_active": row[6],
            "created_at": _parse_optional_timestamp(row[7]),
            "updated_at": _parse_optional_timestamp(row[8]),
        }
        for row in rows
    ]


def replace_favorite_traits(
    *,
    items: list[dict[str, Any]],
    source_origin: str,
    source_ref: str | None,
    archive_missing: bool,
    now: str,
) -> None:
    """Replace favorite traits in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT touched_count, archived_count
            FROM app.replace_favorite_traits(
                %s::jsonb,
                %s,
                %s,
                %s,
                %s::timestamp
            )
            """,
            (
                json.dumps(items),
                source_origin,
                source_ref,
                archive_missing,
                now,
            ),
        )
        conn.commit()


def insert_genre_score_snapshot(
    *,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Insert one already-prepared genre score snapshot into PostgreSQL."""

    if not rows:
        raise ValueError("Je potreba dodat alespon jeden zanr se score.")
    with _connect() as conn, conn.cursor() as cursor:
        for item in rows:
            cursor.execute(
                """
                INSERT INTO app.genre_scores (
                    id, genre, generated_at, algorithm_version, score_scope, source_origin, source_ref,
                    titles_considered, watched_titles_considered, rated_titles_considered,
                    contributing_titles_json, excluded_titles_json,
                    favorite_genre_weight, preference_overlap_score, preference_alignment_score, affinity_score,
                    rating_signal_score, watch_signal_score, recency_score, actor_affinity_score, frequency_score, consistency_score,
                    novelty_score, confidence_score, manual_adjustment_score, final_score, normalized_score,
                    rank_in_run, metrics_json, explanation, created_at
                )
                VALUES (
                    %s, %s, %s::timestamp, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamp
                )
                """,
                (
                    item["id"], item["genre"], item["generated_at"], item.get("algorithm_version"), item.get("score_scope"),
                    item["source_origin"], item.get("source_ref"), item.get("titles_considered"),
                    item.get("watched_titles_considered"), item.get("rated_titles_considered"),
                    item.get("contributing_titles_json"), item.get("excluded_titles_json"),
                    item.get("favorite_genre_weight"), item.get("preference_overlap_score"),
                    item.get("preference_alignment_score"), item.get("affinity_score"),
                    item.get("rating_signal_score"), item.get("watch_signal_score"), item.get("recency_score"),
                    item.get("actor_affinity_score"), item.get("frequency_score"), item.get("consistency_score"),
                    item.get("novelty_score"), item.get("confidence_score"), item.get("manual_adjustment_score"),
                    item["final_score"], item.get("normalized_score"), item.get("rank_in_run"),
                    item.get("metrics_json"), item.get("explanation"), item["created_at"],
                ),
            )
        conn.commit()
    first = rows[0]
    return {
        "generated_at": first["generated_at"],
        "score_scope": first.get("score_scope"),
        "algorithm_version": first.get("algorithm_version"),
        "count": len(rows),
    }


def fetch_latest_genre_scores(
    *,
    score_scope: str | None,
    limit: int | None,
) -> dict[str, Any] | None:
    """Read latest genre score snapshot from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        if score_scope is None:
            cursor.execute(
                """
                SELECT generated_at, score_scope
                FROM app.genre_scores
                ORDER BY generated_at DESC, score_scope ASC
                LIMIT 1
                """
            )
        else:
            cursor.execute(
                """
                SELECT generated_at, score_scope
                FROM app.genre_scores
                WHERE score_scope = %s
                ORDER BY generated_at DESC, score_scope ASC
                LIMIT 1
                """,
                (score_scope,),
            )
        latest_row = cursor.fetchone()
        if latest_row is None:
            return None
        generated_at, resolved_scope = latest_row[0], latest_row[1]
        sql = """
            SELECT
                id, genre, generated_at, algorithm_version, score_scope, source_origin, source_ref,
                titles_considered, watched_titles_considered, rated_titles_considered,
                contributing_titles_json, excluded_titles_json,
                favorite_genre_weight, preference_overlap_score, preference_alignment_score, affinity_score,
                rating_signal_score, watch_signal_score, recency_score, actor_affinity_score, frequency_score, consistency_score,
                novelty_score, confidence_score, manual_adjustment_score, final_score, normalized_score,
                rank_in_run, metrics_json, explanation, created_at
            FROM app.genre_scores
            WHERE generated_at = %s::timestamp AND score_scope IS NOT DISTINCT FROM %s
            ORDER BY rank_in_run ASC NULLS LAST, final_score DESC, genre ASC
        """
        params: list[Any] = [generated_at, resolved_scope]
        if limit is not None:
            sql += "\nLIMIT %s"
            params.append(limit)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    items = [
        {
            "id": row[0],
            "genre": row[1],
            "generated_at": _parse_optional_timestamp(row[2]),
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
            "actor_affinity_score": row[19],
            "frequency_score": row[20],
            "consistency_score": row[21],
            "novelty_score": row[22],
            "confidence_score": row[23],
            "manual_adjustment_score": row[24],
            "final_score": row[25],
            "normalized_score": row[26],
            "rank_in_run": row[27],
            "metrics": _loads_json_or_none(row[28]),
            "explanation": row[29],
            "created_at": _parse_optional_timestamp(row[30]),
        }
        for row in rows
    ]
    return {
        "generated_at": _parse_optional_timestamp(generated_at),
        "score_scope": resolved_scope,
        "count": len(items),
        "items": items,
    }


def fetch_catalog_genres() -> list[dict[str, Any]]:
    """Read all distinct catalog genres with title counts from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            WITH exploded AS (
                SELECT trim(unnest(string_to_array(genres, ','))) AS genre
                FROM app.catalog_titles
                WHERE genres IS NOT NULL AND genres <> ''
            )
            SELECT genre, COUNT(*) AS title_count
            FROM exploded
            WHERE genre IS NOT NULL AND genre <> ''
            GROUP BY genre
            ORDER BY genre ASC
            """
        )
        rows = cursor.fetchall()
    return [{"genre": row[0], "title_count": int(row[1])} for row in rows]


def fetch_genre_score_source_rows() -> list[dict[str, Any]]:
    """Read title-level behavioral inputs for genre scoring from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            WITH latest_title_ratings AS (
                SELECT DISTINCT ON (tconst)
                    tconst,
                    rating
                FROM app.user_ratings
                WHERE tconst IS NOT NULL
                ORDER BY tconst, COALESCE(rated_at, updated_at, created_at) DESC, canonical_key
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
            ),
            rated_cast_affinity AS (
                SELECT
                    c.tconst,
                    SUM(CAST(p.affinity_rating AS DOUBLE PRECISION) * CASE
                        WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0
                        ELSE 1.0 / sqrt(CAST(c.ordering AS DOUBLE PRECISION))
                    END)
                    / NULLIF(
                        SUM(CASE
                            WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0
                            ELSE 1.0 / sqrt(CAST(c.ordering AS DOUBLE PRECISION))
                        END),
                        0.0
                    ) AS actor_affinity_rating
                FROM app.title_credits AS c
                JOIN app.user_people AS p ON p.nconst = c.nconst
                WHERE c.credit_group = 'cast'
                  AND p.affinity_rating > 0
                  AND (c.ordering IS NULL OR c.ordering <= 8)
                GROUP BY c.tconst
            )
            SELECT
                t.tconst,
                t.primary_title,
                t.start_year,
                t.genres,
                r.rating,
                w.watch_count,
                w.last_watched_at,
                a.actor_affinity_rating
            FROM app.catalog_titles AS t
            LEFT JOIN latest_title_ratings AS r ON r.tconst = t.tconst
            LEFT JOIN title_watch_stats AS w ON w.tconst = t.tconst
            LEFT JOIN rated_cast_affinity AS a ON a.tconst = t.tconst
            WHERE t.genres IS NOT NULL
              AND t.genres <> ''
              AND (r.rating IS NOT NULL OR w.watch_count IS NOT NULL OR a.actor_affinity_rating IS NOT NULL)
            ORDER BY t.primary_title ASC
            """
        )
        rows = cursor.fetchall()
    return [
        {
            "tconst": row[0],
            "title": row[1],
            "year": row[2],
            "genres": [part.strip() for part in str(row[3] or "").split(",") if part.strip()],
            "rating": row[4],
            "watch_count": int(row[5] or 0),
            "last_watched_at": _parse_optional_timestamp(row[6]),
            "actor_affinity_rating": row[7],
        }
        for row in rows
    ]


def fetch_relevant_people_candidate_rows(*, main_cast_limit: int, limit: int | None) -> list[dict[str, Any]]:
    """Read relevant people candidates for cache refreshes from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            WITH list_titles AS (
                SELECT DISTINCT
                    COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AS tconst
                FROM app.user_list_items AS i
                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
                WHERE i.is_archived = FALSE
                  AND COALESCE(e.series_tconst, i.tconst, i.parent_tconst) IS NOT NULL
            ),
            list_credit_candidates AS (
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
                JOIN list_titles AS lt ON lt.tconst = c.tconst
                JOIN app.catalog_people AS p USING (nconst)
                WHERE c.credit_group = 'director'
                   OR (c.credit_group = 'cast' AND c.ordering <= %s)
                GROUP BY 1, 2, 3, 4
            ),
            affinity_candidates AS (
                SELECT
                    up.nconst,
                    COALESCE(cp.primary_name, up.name) AS primary_name,
                    cp.birth_year,
                    cp.primary_profession,
                    0 AS credit_count,
                    0 AS group_priority
                FROM app.user_people AS up
                LEFT JOIN app.catalog_people AS cp ON cp.nconst = up.nconst
                WHERE up.nconst IS NOT NULL
                  AND (up.affinity_rating > 0 OR up.is_favorite = TRUE)
            ),
            combined AS (
                SELECT * FROM list_credit_candidates
                UNION ALL
                SELECT * FROM affinity_candidates
            )
            SELECT
                nconst,
                primary_name,
                birth_year,
                primary_profession,
                MAX(credit_count) AS credit_count,
                MIN(group_priority) AS group_priority
            FROM combined
            WHERE nconst IS NOT NULL
            GROUP BY 1, 2, 3, 4
            ORDER BY group_priority, credit_count DESC, birth_year DESC NULLS LAST, primary_name
            """
            + ("\nLIMIT %s" if limit is not None else ""),
            ((main_cast_limit, limit) if limit is not None else (main_cast_limit,)),
        )
        rows = cursor.fetchall()
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


def record_search_recall_entry(
    *,
    entry_id: str,
    entity_type: str,
    query_text: str,
    query_text_fold: str,
    query_key: str,
    target_id: str,
    target_label: str | None,
    target_title_type: str | None,
    matched_alias_title: str | None,
    fuzzy_score: float | None,
    now: str,
    recall_limit: int,
) -> None:
    """Upsert one search recall entry in PostgreSQL and prune overflow."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.search_recall (
                id, entity_type, query_text, query_text_fold, query_key, target_id, target_label,
                target_title_type, matched_alias_title, fuzzy_score, first_searched_at, last_searched_at, hit_count
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamp, %s::timestamp, 1)
            ON CONFLICT (id) DO UPDATE SET
                query_text = excluded.query_text,
                query_key = excluded.query_key,
                target_label = excluded.target_label,
                target_title_type = excluded.target_title_type,
                matched_alias_title = excluded.matched_alias_title,
                fuzzy_score = excluded.fuzzy_score,
                last_searched_at = excluded.last_searched_at,
                hit_count = app.search_recall.hit_count + 1
            """,
            (
                entry_id,
                entity_type,
                query_text,
                query_text_fold,
                query_key,
                target_id,
                target_label,
                target_title_type,
                matched_alias_title,
                fuzzy_score,
                now,
                now,
            ),
        )
        cursor.execute(
            """
            DELETE FROM app.search_recall
            WHERE id IN (
                SELECT id
                FROM app.search_recall
                ORDER BY last_searched_at DESC, hit_count DESC, first_searched_at DESC, id DESC
                OFFSET %s
            )
            """,
            (max(recall_limit, 0),),
        )
        conn.commit()


def fetch_search_recall_match(
    *,
    entity_type: str,
    query_key: str,
    query_text_fold: str,
) -> tuple[str, float | None] | None:
    """Return best search recall match for one entity/query pair."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT target_id, fuzzy_score
            FROM app.search_recall
            WHERE entity_type = %s AND query_key = %s
            ORDER BY
                CASE WHEN query_text_fold = %s THEN 0 ELSE 1 END,
                last_searched_at DESC,
                hit_count DESC,
                first_searched_at DESC
            LIMIT 1
            """,
            (entity_type, query_key, query_text_fold),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return str(row[0]), row[1]


def create_import_batch_record(
    *,
    batch_id: str,
    source: str,
    filename: str,
    checksum: str,
    status: str,
    created_at: str,
) -> None:
    """Insert one import batch row into PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.import_batches (id, source, filename, checksum, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s::timestamp)
            """,
            (batch_id, source, filename, checksum, status, created_at),
        )
        conn.commit()


def insert_import_rows(rows: list[dict[str, Any]]) -> None:
    """Insert previewed import rows into PostgreSQL."""

    if not rows:
        return
    with _connect() as conn, conn.cursor() as cursor:
        cursor.executemany(
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
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s::date, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            [
                (
                    row["id"],
                    row["batch_id"],
                    row["source"],
                    row["row_number"],
                    row["raw_json"],
                    row.get("parsed_title"),
                    row.get("parsed_year"),
                    row.get("parsed_watched_on"),
                    row.get("parsed_season_number"),
                    row.get("parsed_episode_number"),
                    row.get("parsed_imdb_id"),
                    row.get("parsed_tmdb_id"),
                    row["resolution_status"],
                    row.get("resolved_tconst"),
                    row.get("resolution_confidence"),
                    row.get("resolution_note"),
                )
                for row in rows
            ],
        )
        conn.commit()


def fetch_import_batch_record(batch_id: str) -> dict[str, Any] | None:
    """Read one import batch row from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, source, filename, checksum, status, created_at
            FROM app.import_batches
            WHERE id = %s
            """,
            (batch_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "source": row[1],
        "filename": row[2],
        "checksum": row[3],
        "status": row[4],
        "created_at": _parse_optional_timestamp(row[5]),
    }


def fetch_import_batch_rows(batch_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """Read preview rows for one import batch from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
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
            WHERE batch_id = %s
            ORDER BY row_number
            LIMIT %s
            """,
            (batch_id, limit),
        )
        rows = cursor.fetchall()
    return [
        {
            "row_number": row[0],
            "parsed_title": row[1],
            "parsed_year": row[2],
            "parsed_watched_on": _parse_optional_date(row[3]),
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
    ]


def fetch_resolved_import_rows(batch_id: str) -> list[dict[str, Any]]:
    """Read resolved import rows for commit from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, source, parsed_watched_on, resolved_tconst, parsed_season_number, parsed_episode_number
            FROM app.import_rows
            WHERE batch_id = %s AND resolution_status = 'resolved' AND resolved_tconst IS NOT NULL
            ORDER BY row_number
            """,
            (batch_id,),
        )
        rows = cursor.fetchall()
    return [
        {
            "id": row[0],
            "source": row[1],
            "parsed_watched_on": _parse_optional_date(row[2]),
            "resolved_tconst": row[3],
            "parsed_season_number": row[4],
            "parsed_episode_number": row[5],
        }
        for row in rows
    ]


def commit_import_batch(
    *,
    batch_id: str,
    committed_at: str,
) -> dict[str, Any]:
    """Commit one resolved import batch through the server-side PostgreSQL function."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT inserted_events, skipped_events, batch_status
            FROM app.commit_import_batch(%s, %s::timestamp)
            """,
            (batch_id, committed_at),
        )
        row = cursor.fetchone()
        conn.commit()
    if row is None:
        raise RuntimeError(f"PostgreSQL commit_import_batch({batch_id}) nevratil vysledek.")
    return {
        "inserted_events": int(row[0] or 0),
        "skipped_events": int(row[1] or 0),
        "batch_status": str(row[2] or "committed"),
    }


def fetch_existing_import_commits(batch_id: str, import_row_ids: list[str]) -> set[str]:
    """Return which import row ids already produced watch events in PostgreSQL."""

    clean_ids = [str(item).strip() for item in import_row_ids if str(item).strip()]
    if not clean_ids:
        return set()
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT import_row_id
            FROM app.watch_events
            WHERE batch_id = %s AND import_row_id = ANY(%s)
            """,
            (batch_id, clean_ids),
        )
        rows = cursor.fetchall()
    return {str(row[0]) for row in rows if row[0] is not None}


def upsert_tmdb_mapping_record(
    *,
    tconst: str,
    tmdb_media_type: str,
    tmdb_id: int,
    matched_by: str,
    sync_status: str,
    matched_at: str,
    last_error: str | None,
) -> None:
    """Upsert one TMDB mapping row in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.tmdb_title_map (
                tconst, tmdb_media_type, tmdb_id, matched_by, matched_at, sync_status, last_error
            )
            VALUES (%s, %s, %s, %s, %s::timestamp, %s, %s)
            ON CONFLICT (tconst) DO UPDATE SET
                tmdb_media_type = excluded.tmdb_media_type,
                tmdb_id = excluded.tmdb_id,
                matched_by = excluded.matched_by,
                matched_at = excluded.matched_at,
                sync_status = excluded.sync_status,
                last_error = excluded.last_error
            """,
            (tconst, tmdb_media_type, tmdb_id, matched_by, matched_at, sync_status, last_error),
        )
        conn.commit()


def fetch_tmdb_mapping_record(tconst: str) -> dict[str, Any] | None:
    """Read one TMDB mapping row from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT tconst, tmdb_media_type, tmdb_id, matched_by, matched_at, sync_status, last_error
            FROM app.tmdb_title_map
            WHERE tconst = %s
            """,
            (tconst,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return {
        "tconst": row[0],
        "tmdb_media_type": row[1],
        "tmdb_id": row[2],
        "matched_by": row[3],
        "matched_at": _parse_optional_timestamp(row[4]),
        "sync_status": row[5],
        "last_error": row[6],
    }


def store_tmdb_payload_bundle(
    *,
    tconst: str,
    locale: str,
    display_title: str | None,
    original_title: str | None,
    overview: str | None,
    poster_path: str | None,
    backdrop_path: str | None,
    release_date: str | None,
    genres_json: str,
    raw_json: str,
    synced_at: str,
    providers: list[dict[str, Any]],
) -> None:
    """Upsert TMDB detail and CZ provider rows in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.tmdb_title_details (
                tconst, locale, display_title, original_title, overview, poster_path, backdrop_path,
                release_date, genres_json, raw_json, synced_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::date, %s, %s, %s::timestamp)
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
            (tconst, locale, display_title, original_title, overview, poster_path, backdrop_path, release_date, genres_json, raw_json, synced_at),
        )
        cursor.execute(
            "DELETE FROM app.tmdb_watch_providers WHERE tconst = %s AND country_code = 'CZ'",
            (tconst,),
        )
        for provider in providers:
            cursor.execute(
                """
                INSERT INTO app.tmdb_watch_providers (
                    tconst, country_code, provider_type, provider_id, provider_name, logo_path, display_priority, synced_at
                )
                VALUES (%s, 'CZ', %s, %s, %s, %s, %s, %s::timestamp)
                """,
                (
                    tconst,
                    provider.get("provider_type"),
                    provider.get("provider_id"),
                    provider.get("provider_name"),
                    provider.get("logo_path"),
                    provider.get("display_priority"),
                    synced_at,
                ),
            )
        conn.commit()


def insert_tmdb_asset_record(
    *,
    asset_id: str,
    tconst: str,
    asset_kind: str,
    relative_path: str,
    local_path: str,
    fetch_reason: str,
    status: str,
    sha256: str | None,
    fetched_at: str,
) -> None:
    """Insert one TMDB asset row into PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.tmdb_assets (
                id, tconst, asset_kind, relative_path, local_path, fetch_reason, status, sha256, fetched_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::timestamp)
            """,
            (asset_id, tconst, asset_kind, relative_path, local_path, fetch_reason, status, sha256, fetched_at),
        )
        conn.commit()


def fetch_latest_tmdb_assets_for_title(tconst: str) -> list[dict[str, Any]]:
    """Read TMDB asset history for one title from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, asset_kind, relative_path, local_path, fetch_reason, status, sha256, fetched_at
            FROM app.tmdb_assets
            WHERE tconst = %s
            ORDER BY fetched_at DESC, id DESC
            """,
            (tconst,),
        )
        rows = cursor.fetchall()
    return [
        {
            "id": row[0],
            "asset_kind": row[1],
            "relative_path": row[2],
            "local_path": row[3],
            "fetch_reason": row[4],
            "status": row[5],
            "sha256": row[6],
            "fetched_at": _parse_optional_timestamp(row[7]),
        }
        for row in rows
    ]


def fetch_tmdb_payload_snapshot(tconst: str, *, primary_locale: str, fallback_locale: str) -> dict[str, Any] | None:
    """Read mapping, best detail, locales and providers for one title from PostgreSQL."""

    mapping = fetch_tmdb_mapping_record(tconst)
    if mapping is None:
        return None
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT locale, display_title, overview, poster_path, backdrop_path, release_date, synced_at
            FROM app.tmdb_title_details
            WHERE tconst = %s
            ORDER BY
                CASE locale
                    WHEN %s THEN 0
                    WHEN %s THEN 1
                    ELSE 2
                END,
                synced_at DESC
            LIMIT 1
            """,
            (tconst, primary_locale, fallback_locale),
        )
        details = cursor.fetchone()
        cursor.execute(
            """
            SELECT locale
            FROM app.tmdb_title_details
            WHERE tconst = %s
            ORDER BY
                CASE locale
                    WHEN %s THEN 0
                    WHEN %s THEN 1
                    ELSE 2
                END,
                synced_at DESC
            """,
            (tconst, primary_locale, fallback_locale),
        )
        detail_locales = cursor.fetchall()
        cursor.execute(
            """
            SELECT provider_type, provider_name, logo_path
            FROM app.tmdb_watch_providers
            WHERE tconst = %s
            ORDER BY provider_type, display_priority NULLS LAST, provider_name
            """,
            (tconst,),
        )
        providers = cursor.fetchall()
    return {
        "mapping": mapping,
        "details": (
            {
                "locale": details[0],
                "display_title": details[1],
                "overview": details[2],
                "poster_path": details[3],
                "backdrop_path": details[4],
                "release_date": _parse_optional_date(details[5]),
                "synced_at": _parse_optional_timestamp(details[6]),
            }
            if details
            else None
        ),
        "detail_locales": [str(row[0]) for row in detail_locales],
        "providers": [{"provider_type": row[0], "provider_name": row[1], "logo_path": row[2]} for row in providers],
        "assets": fetch_latest_tmdb_assets_for_title(tconst),
    }


def fetch_tmdb_completion_flags(
    tconsts: list[str],
    *,
    primary_locale: str,
    fallback_locale: str,
) -> dict[str, dict[str, Any]]:
    """Read TMDB completion-related flags for many titles in one PostgreSQL round-trip."""

    clean_tconsts = [str(tconst).strip() for tconst in tconsts if str(tconst).strip()]
    if not clean_tconsts:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            WITH detail_flags AS (
                SELECT
                    tconst,
                    MAX(CASE WHEN locale = %s THEN 1 ELSE 0 END) AS has_primary,
                    MAX(CASE WHEN locale = %s THEN 1 ELSE 0 END) AS has_fallback,
                    MAX(CASE WHEN locale = %s THEN poster_path WHEN locale = %s THEN poster_path ELSE NULL END) AS poster_path,
                    MAX(CASE WHEN locale = %s THEN backdrop_path WHEN locale = %s THEN backdrop_path ELSE NULL END) AS backdrop_path
                FROM app.tmdb_title_details
                WHERE tconst = ANY(%s)
                GROUP BY tconst
            ),
            asset_flags AS (
                SELECT
                    tconst,
                    MAX(CASE WHEN asset_kind = 'poster' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_poster,
                    MAX(CASE WHEN asset_kind = 'backdrop' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_backdrop
                FROM app.tmdb_assets
                WHERE tconst = ANY(%s)
                GROUP BY tconst
            )
            SELECT
                m.tconst,
                m.sync_status,
                COALESCE(d.has_primary, 0),
                COALESCE(d.has_fallback, 0),
                d.poster_path,
                d.backdrop_path,
                COALESCE(a.has_poster, 0),
                COALESCE(a.has_backdrop, 0)
            FROM app.tmdb_title_map AS m
            LEFT JOIN detail_flags AS d ON d.tconst = m.tconst
            LEFT JOIN asset_flags AS a ON a.tconst = m.tconst
            WHERE m.tconst = ANY(%s)
            """,
            (
                primary_locale,
                fallback_locale,
                primary_locale,
                fallback_locale,
                primary_locale,
                fallback_locale,
                clean_tconsts,
                clean_tconsts,
                clean_tconsts,
            ),
        )
        rows = cursor.fetchall()
    return {
        str(row[0]): {
            "sync_status": row[1],
            "has_primary": bool(row[2]),
            "has_fallback": bool(row[3]),
            "poster_path": row[4],
            "backdrop_path": row[5],
            "has_poster": bool(row[6]),
            "has_backdrop": bool(row[7]),
        }
        for row in rows
        if row[0] is not None
    }


def replace_imdb_manifest_rows(rows: list[dict[str, Any]]) -> None:
    """Replace imdb_file_manifest rows in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("DELETE FROM app.imdb_file_manifest")
        if rows:
            cursor.executemany(
                """
                INSERT INTO app.imdb_file_manifest (
                    source_key, source_path, source_mtime, source_size, source_sha256, recorded_at
                )
                VALUES (%s, %s, %s, %s, %s, %s::timestamp)
                """,
                [
                    (
                        row["source_key"],
                        row["source_path"],
                        row["source_mtime"],
                        row["source_size"],
                        row["source_sha256"],
                        row["recorded_at"],
                    )
                    for row in rows
                ],
            )
        conn.commit()


def replace_catalog_refresh_meta_rows(rows: list[dict[str, Any]]) -> None:
    """Replace catalog_refresh_meta rows in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("DELETE FROM app.catalog_refresh_meta")
        if rows:
            cursor.executemany(
                """
                INSERT INTO app.catalog_refresh_meta (source_key, fingerprint)
                VALUES (%s, %s)
                """,
                [(row["source_key"], row["fingerprint"]) for row in rows],
            )
        conn.commit()


def fetch_imdb_manifest_rows() -> list[dict[str, Any]]:
    """Read imdb_file_manifest rows from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_key, source_path, source_mtime, source_size, source_sha256, recorded_at
            FROM app.imdb_file_manifest
            ORDER BY source_key
            """
        )
        rows = cursor.fetchall()
    return [
        {
            "source_key": row[0],
            "source_path": row[1],
            "source_mtime": row[2],
            "source_size": row[3],
            "source_sha256": row[4],
            "recorded_at": _parse_optional_timestamp(row[5]),
        }
        for row in rows
    ]


def fetch_catalog_refresh_rows() -> list[dict[str, Any]]:
    """Read catalog_refresh_meta rows from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_key, fingerprint
            FROM app.catalog_refresh_meta
            ORDER BY source_key
            """
        )
        rows = cursor.fetchall()
    return [{"source_key": row[0], "fingerprint": row[1]} for row in rows]


def fetch_catalog_refresh_fingerprint() -> str | None:
    """Read combined catalog refresh fingerprint from PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT string_agg(source_key || '=' || fingerprint, '|' ORDER BY source_key)
            FROM app.catalog_refresh_meta
            """
        )
        row = cursor.fetchone()
    return None if row is None else row[0]


def local_seed_exists(seed_name: str) -> bool:
    """Return whether one local seed marker exists in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM app.local_seed_meta WHERE seed_name = %s LIMIT 1",
            (seed_name,),
        )
        return cursor.fetchone() is not None


def record_local_seed_meta(*, seed_name: str, seeded_at: str, note: str | None) -> None:
    """Upsert one local seed marker in PostgreSQL."""

    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app.local_seed_meta (seed_name, seeded_at, note)
            VALUES (%s, %s::timestamp, %s)
            ON CONFLICT (seed_name) DO UPDATE SET
                seeded_at = excluded.seeded_at,
                note = excluded.note
            """,
            (seed_name, seeded_at, note),
        )
        conn.commit()


def _row_to_user_rating(row: tuple[Any, ...] | list[Any]) -> dict[str, Any]:
    return {
        "canonical_key": row[0],
        "tconst": row[1],
        "rating": row[2],
        "liked_notes": row[3],
        "disliked_notes": row[4],
        "rated_at": _parse_optional_timestamp(row[5]),
        "updated_at": _parse_optional_timestamp(row[6]),
    }


def _row_to_watch_event(row: tuple[Any, ...] | list[Any]) -> dict[str, Any]:
    return {
        "id": row[0],
        "tconst": row[1],
        "event_scope": row[2],
        "watched_on": row[3],
        "source": row[4],
        "batch_id": row[5],
        "import_row_id": row[6],
        "rating": row[7],
        "notes": row[8],
        "created_at": _parse_optional_timestamp(row[9]),
    }


def _row_to_content_state(row: tuple[Any, ...] | list[Any]) -> dict[str, Any]:
    return {
        "tconst": row[0],
        "interest_state": row[1],
        "last_previewed_at": _parse_optional_timestamp(row[2]),
        "last_watched_at": _parse_optional_timestamp(row[3]),
        "updated_at": _parse_optional_timestamp(row[4]),
    }


def _parse_optional_timestamp(value: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    stripped = str(value).strip()
    if not stripped:
        return None
    return datetime.fromisoformat(stripped)


def _parse_optional_date(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _loads_json_or_none(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _connect() -> psycopg.Connection:
    config = _load_runtime_postgres_config()
    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.database,
        user=config.user,
        password=config.password,
        connect_timeout=10,
    )


@lru_cache(maxsize=1)
def _load_runtime_postgres_config() -> RuntimePostgresConfig:
    values = dict(dotenv_values(ENV_PATH, interpolate=False))
    password = values.get("POSTGRES_APP_PASSWORD") or ""
    if not password:
        raise RuntimeError("POSTGRES_APP_PASSWORD v .env chybí nebo je prázdné.")
    return RuntimePostgresConfig(
        host=values.get("POSTGRES_APP_HOST") or "/private/tmp",
        port=values.get("POSTGRES_APP_PORT") or "5432",
        database=values.get("POSTGRES_APP_DATABASE") or TARGET_DATABASE,
        user=values.get("POSTGRES_APP_USER") or "filmy_app",
        password=password,
    )
