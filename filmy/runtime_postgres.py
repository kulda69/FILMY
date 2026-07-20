from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import json
from typing import Any
import uuid
from dotenv import dotenv_values
import psycopg
from filmy.config import UiConfig, get_ui_config
from filmy.paths import ENV_PATH
TARGET_DATABASE = 'filmy'

@dataclass(frozen=True)
class RuntimePostgresConfig:
    """Minimal runtime PostgreSQL connection settings."""
    host: str
    port: str
    database: str
    user: str
    password: str

def content_state_uses_postgres(ui_config: UiConfig | None=None) -> bool:
    """Return whether content-state reads/writes use PostgreSQL."""
    return True

def user_ratings_uses_postgres(ui_config: UiConfig | None=None) -> bool:
    """Return whether user-rating reads/writes use PostgreSQL."""
    return True

def watch_events_uses_postgres(ui_config: UiConfig | None=None) -> bool:
    """Return whether watch-event reads/writes use PostgreSQL."""
    return True

def user_lists_uses_postgres(ui_config: UiConfig | None=None) -> bool:
    """Return whether user-list reads/writes use PostgreSQL."""
    return True

def app_state_uses_postgres(ui_config: UiConfig | None=None) -> bool:
    """Return whether small app-state reads/writes use PostgreSQL."""
    return True

def import_backend_uses_postgres(ui_config: UiConfig | None=None) -> bool:
    """Return whether import preview/commit state uses PostgreSQL."""
    return True

def catalog_backend_uses_postgres(ui_config: UiConfig | None=None) -> bool:
    """Return whether catalog read paths use PostgreSQL."""
    return True

def tmdb_backend_uses_postgres(ui_config: UiConfig | None=None) -> bool:
    """Return whether TMDB runtime tables use PostgreSQL."""
    return True

def meta_backend_uses_postgres(ui_config: UiConfig | None=None) -> bool:
    """Return whether catalog metadata/seed guard uses PostgreSQL."""
    return True

def fetch_catalog_search_rows(*, query: str | None, title_type: str | None, limit: int) -> list[tuple[Any, ...]]:
    """Return base catalog search rows from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            SELECT\n                tconst,\n                title_type,\n                primary_title,\n                original_title,\n                start_year,\n                runtime_minutes,\n                genres,\n                average_rating,\n                num_votes\n            FROM app.catalog_titles\n            WHERE (%s::text IS NULL OR primary_title ILIKE '%%' || %s::text || '%%' OR original_title ILIKE '%%' || %s::text || '%%')\n              AND (%s::text IS NULL OR title_type = %s::text)\n            ORDER BY\n                CASE WHEN average_rating IS NULL THEN 1 ELSE 0 END,\n                average_rating DESC,\n                num_votes DESC,\n                start_year DESC NULLS LAST,\n                primary_title\n            LIMIT %s\n            ", (query, query, query, title_type, title_type, limit))
        return cursor.fetchall()

def fetch_catalog_stats_row() -> dict[str, int | None]:
    """Read top-level catalog stats from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            SELECT\n                COUNT(*) AS titles,\n                COUNT(*) FILTER (WHERE title_type = 'movie') AS movies,\n                COUNT(*) FILTER (WHERE title_type IN ('tvSeries', 'tvMiniSeries')) AS series,\n                MIN(start_year) AS oldest_year,\n                MAX(start_year) AS newest_year,\n                (SELECT COUNT(*) FROM app.catalog_episodes) AS episodes,\n                (SELECT COUNT(*) FROM app.title_aliases) AS aliases\n            FROM app.catalog_titles\n            ")
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError('PostgreSQL catalog stats dotaz nevrátil žádný řádek.')
    return {'titles': int(row[0] or 0), 'movies': int(row[1] or 0), 'series': int(row[2] or 0), 'oldest_year': int(row[3]) if row[3] is not None else None, 'newest_year': int(row[4]) if row[4] is not None else None, 'episodes': int(row[5] or 0), 'aliases': int(row[6] or 0)}

def fetch_catalog_title_row(tconst: str) -> tuple[Any, ...] | None:
    """Read one title row from PostgreSQL catalog_titles."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                tconst,\n                title_type,\n                primary_title,\n                original_title,\n                start_year,\n                end_year,\n                runtime_minutes,\n                genres,\n                average_rating,\n                num_votes\n            FROM app.catalog_titles\n            WHERE tconst = %s\n            ', (tconst,))
        return cursor.fetchone()

def fetch_tconst_for_tmdb_id(tmdb_id: int) -> str | None:
    """Resolve one TMDB ID to the newest mapped IMDb tconst."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT tconst\n            FROM app.tmdb_title_map\n            WHERE tmdb_id = %s\n            ORDER BY matched_at DESC, tconst\n            LIMIT 1\n            ', (tmdb_id,))
        row = cursor.fetchone()
    return str(row[0]) if row and row[0] is not None else None

def fetch_primary_title_matches(lower_titles: list[str]) -> dict[str, str]:
    """Map lower(primary_title) values to best matching tconsts."""
    if not lower_titles:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT lowered_title, tconst\n            FROM (\n                SELECT\n                    lower(primary_title) AS lowered_title,\n                    tconst,\n                    row_number() OVER (\n                        PARTITION BY lower(primary_title)\n                        ORDER BY num_votes DESC NULLS LAST, average_rating DESC NULLS LAST, tconst\n                    ) AS rn\n                FROM app.catalog_titles\n                WHERE lower(primary_title) = ANY(%s)\n            ) AS ranked\n            WHERE rn = 1\n            ', (lower_titles,))
        rows = cursor.fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}

def fetch_title_lookup_primary_key_matches(title_keys: list[str]) -> dict[str, str]:
    """Map normalized primary-title keys to best matching tconsts."""
    if not title_keys:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT primary_key, tconst\n            FROM (\n                SELECT\n                    primary_key,\n                    tconst,\n                    row_number() OVER (\n                        PARTITION BY primary_key\n                        ORDER BY num_votes DESC NULLS LAST, average_rating DESC NULLS LAST, tconst\n                    ) AS rn\n                FROM app.title_lookup\n                WHERE primary_key = ANY(%s)\n            ) AS ranked\n            WHERE rn = 1\n            ', (title_keys,))
        rows = cursor.fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}

def fetch_title_alias_lookup_matches(alias_keys: list[str]) -> dict[str, str]:
    """Map normalized alias keys to best matching tconsts."""
    if not alias_keys:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT alias_key, tconst\n            FROM (\n                SELECT\n                    alias_key,\n                    tconst,\n                    row_number() OVER (\n                        PARTITION BY alias_key\n                        ORDER BY alias_priority, num_votes DESC NULLS LAST, average_rating DESC NULLS LAST, tconst\n                    ) AS rn\n                FROM app.title_alias_lookup\n                WHERE alias_key = ANY(%s)\n            ) AS ranked\n            WHERE rn = 1\n            ', (alias_keys,))
        rows = cursor.fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}

def fetch_title_by_primary_title_year(title: str, year: int | None) -> str | None:
    """Resolve exact primary title with optional year to best tconst."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT tconst\n            FROM app.catalog_titles\n            WHERE lower(primary_title) = lower(%s::text)\n              AND (%s::integer IS NULL OR start_year = %s::integer)\n            ORDER BY num_votes DESC NULLS LAST, average_rating DESC NULLS LAST, tconst\n            LIMIT 1\n            ', (title, year, year))
        row = cursor.fetchone()
    return str(row[0]) if row and row[0] is not None else None

def fetch_catalog_primary_title(tconst: str) -> str | None:
    """Read only the primary title for one catalog title."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('SELECT primary_title FROM app.catalog_titles WHERE tconst = %s', (tconst,))
        row = cursor.fetchone()
    return str(row[0]) if row and row[0] is not None else None

def fetch_catalog_episode_row(tconst: str) -> tuple[Any, ...] | None:
    """Read one episode row from PostgreSQL catalog_episodes."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                episode_tconst,\n                series_tconst,\n                season_number,\n                episode_number,\n                primary_title,\n                original_title,\n                start_year,\n                runtime_minutes\n            FROM app.catalog_episodes\n            WHERE episode_tconst = %s\n            ', (tconst,))
        return cursor.fetchone()

def fetch_series_episode_rows(series_tconst: str) -> list[tuple[Any, ...]]:
    """Read ordered episode rows for one series from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                episode_tconst,\n                season_number,\n                episode_number,\n                primary_title,\n                start_year\n            FROM app.catalog_episodes\n            WHERE series_tconst = %s\n            ORDER BY season_number NULLS LAST, episode_number NULLS LAST, episode_tconst\n            ', (series_tconst,))
        return cursor.fetchall()

def fetch_title_alias_rows(tconst: str, *, limit: int=20) -> list[tuple[Any, ...]]:
    """Read ordered alias rows for one title from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT title, region, language, types, is_original_title\n            FROM app.title_aliases\n            WHERE tconst = %s\n            ORDER BY region NULLS LAST, language NULLS LAST, title\n            LIMIT %s\n            ', (tconst, limit))
        return cursor.fetchall()

def fetch_title_people_rows(tconst: str) -> list[tuple[Any, ...]]:
    """Read credit rows with joined people names for one title from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                c.nconst,\n                c.credit_group,\n                c.category,\n                c.job,\n                c.characters,\n                c.ordering,\n                p.primary_name\n            FROM app.title_credits AS c\n            JOIN app.catalog_people AS p USING (nconst)\n            WHERE c.tconst = %s\n            ORDER BY c.ordering, p.primary_name\n            ', (tconst,))
        return cursor.fetchall()

def fetch_title_people_preview_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read lightweight director/cast preview rows for many titles from PostgreSQL."""
    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            SELECT\n                c.tconst,\n                c.credit_group,\n                c.ordering,\n                p.primary_name\n            FROM app.title_credits AS c\n            JOIN app.catalog_people AS p USING (nconst)\n            WHERE c.tconst = ANY(%s)\n              AND c.credit_group IN ('director', 'cast')\n              AND (\n                  c.credit_group <> 'cast'\n                  OR c.ordering IS NULL\n                  OR c.ordering <= 5\n              )\n            ORDER BY\n                c.tconst,\n                CASE c.credit_group\n                    WHEN 'director' THEN 0\n                    WHEN 'cast' THEN 1\n                    ELSE 2\n                END,\n                c.ordering NULLS LAST,\n                p.primary_name\n            ", (tconsts,))
        return cursor.fetchall()

def fetch_person_catalog_row(nconst: str) -> tuple[Any, ...] | None:
    """Read one person row from PostgreSQL catalog_people."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT nconst, primary_name, birth_year, death_year, primary_profession, known_for_titles\n            FROM app.catalog_people\n            WHERE nconst = %s\n            ', (nconst,))
        return cursor.fetchone()

def fetch_person_lookup_row(nconst: str) -> tuple[Any, ...] | None:
    """Read one person row with lookup-oriented credit count from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                p.nconst,\n                p.primary_name,\n                p.birth_year,\n                p.death_year,\n                p.primary_profession,\n                p.known_for_titles,\n                COALESCE((\n                    SELECT COUNT(*)\n                    FROM app.title_credits AS c\n                    WHERE c.nconst = p.nconst\n                ), 0) AS credit_count\n            FROM app.catalog_people AS p\n            WHERE p.nconst = %s\n            ', (nconst,))
        return cursor.fetchone()

def fetch_person_credit_rows(nconst: str, *, limit: int=500) -> list[tuple[Any, ...]]:
    """Read joined person credit rows from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            SELECT\n                c.credit_group,\n                c.category,\n                c.job,\n                c.characters,\n                c.ordering,\n                t.tconst,\n                t.primary_title,\n                t.original_title,\n                t.start_year,\n                t.title_type\n            FROM app.title_credits AS c\n            JOIN app.catalog_titles AS t ON t.tconst = c.tconst\n            WHERE c.nconst = %s\n            ORDER BY\n                CASE c.credit_group\n                    WHEN 'director' THEN 0\n                    WHEN 'creator' THEN 1\n                    WHEN 'writer' THEN 2\n                    WHEN 'cast' THEN 3\n                    ELSE 4\n                END,\n                t.start_year DESC NULLS LAST,\n                c.ordering,\n                t.primary_title\n            LIMIT %s\n            ", (nconst, limit))
        return cursor.fetchall()

def fetch_person_episode_series_credit_rows(nconst: str, *, limit: int=200) -> list[tuple[Any, ...]]:
    """Read aggregated episode-only acting credits to parent series from PostgreSQL raw/app tables."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            WITH existing_series AS (\n                SELECT DISTINCT tconst\n                FROM app.title_credits\n                WHERE nconst = %s AND credit_group = 'cast'\n            )\n            SELECT\n                e.parent_tconst AS series_tconst,\n                s.primary_title,\n                s.original_title,\n                s.start_year,\n                s.title_type,\n                COUNT(*) AS episode_count,\n                MIN(p.ordering) AS best_ordering\n            FROM raw.title_principals AS p\n            JOIN raw.title_episode AS e ON e.tconst = p.tconst\n            JOIN app.catalog_titles AS s ON s.tconst = e.parent_tconst\n            LEFT JOIN existing_series AS x ON x.tconst = e.parent_tconst\n            WHERE p.nconst = %s\n              AND p.category IN ('actor', 'actress')\n              AND x.tconst IS NULL\n            GROUP BY 1, 2, 3, 4, 5\n            ORDER BY s.start_year DESC NULLS LAST, best_ordering, s.primary_title\n            LIMIT %s\n            ", (nconst, nconst, limit))
        return cursor.fetchall()

def fetch_known_for_title_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read lightweight title metadata for known-for rows from PostgreSQL."""
    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                tconst,\n                primary_title,\n                start_year\n            FROM app.catalog_titles\n            WHERE tconst = ANY(%s)\n            ', (tconsts,))
        return cursor.fetchall()

def fetch_people_for_lookup_rows(query: str, limit: int) -> list[tuple[Any, ...]]:
    """Read direct person lookup rows from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            SELECT\n                nconst,\n                primary_name,\n                birth_year,\n                death_year,\n                primary_profession,\n                known_for_titles,\n                credit_count\n            FROM app.person_lookup\n            WHERE primary_name ILIKE '%%' || %s::text || '%%'\n            ORDER BY\n                CASE WHEN lower(primary_name) = lower(%s::text) THEN 0 ELSE 1 END,\n                credit_count DESC,\n                birth_year DESC NULLS LAST,\n                primary_name\n            LIMIT %s\n            ", (query, query, limit))
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
        cursor.execute('\n            SELECT\n                nconst,\n                primary_name,\n                birth_year,\n                death_year,\n                primary_profession,\n                known_for_titles,\n                credit_count\n            FROM app.person_lookup\n            WHERE (\n                name_prefix3 = %s\n                OR first_token_prefix3 = %s\n                OR last_token_prefix3 = %s\n                OR compact_name_prefix3 = %s\n                OR name_prefix2 = %s\n                OR first_token_prefix2 = %s\n                OR last_token_prefix2 = %s\n                OR compact_name_prefix2 = %s\n            )\n              AND (\n                name_length BETWEEN %s AND %s\n                OR last_token_length BETWEEN %s AND %s\n                OR compact_name_length BETWEEN %s AND %s\n              )\n            ORDER BY credit_count DESC, birth_year DESC NULLS LAST, primary_name\n            LIMIT %s\n            ', (prefix3, prefix3, prefix3, prefix3, prefix2, prefix2, prefix2, prefix2, length_floor, length_ceiling, length_floor, length_ceiling, length_floor, length_ceiling, max(limit, 500)))
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
        cursor.execute('\n            SELECT\n                nconst,\n                primary_name,\n                birth_year,\n                death_year,\n                primary_profession,\n                known_for_titles,\n                credit_count,\n                least(\n                    public.levenshtein(%s::text, name_key::text),\n                    public.levenshtein(%s::text, last_token_key::text),\n                    public.levenshtein(%s::text, compact_name_key::text)\n                ) AS edit_distance\n            FROM app.person_lookup\n            WHERE (\n                name_prefix1 = %s\n                OR first_token_prefix1 = %s\n                OR last_token_prefix1 = %s\n                OR compact_name_prefix1 = %s\n            )\n              AND (\n                name_length BETWEEN %s AND %s\n                OR last_token_length BETWEEN %s AND %s\n                OR compact_name_length BETWEEN %s AND %s\n              )\n            ORDER BY edit_distance ASC, credit_count DESC, birth_year DESC NULLS LAST, primary_name\n            LIMIT %s\n            ', (query_key, query_key, query_key, first_letter, first_letter, first_letter, first_letter, length_floor, length_ceiling, length_floor, length_ceiling, length_floor, length_ceiling, max(limit, 500)))
        return cursor.fetchall()

def fetch_episode_series_map(tconsts: list[str]) -> dict[str, str]:
    """Map episode tconsts to parent series tconsts from PostgreSQL."""
    if not tconsts:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT episode_tconst, series_tconst\n            FROM app.catalog_episodes\n            WHERE episode_tconst = ANY(%s)\n              AND series_tconst IS NOT NULL\n            ', (tconsts,))
        rows = cursor.fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}

def fetch_title_card_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read lightweight title-card rows including latest poster from PostgreSQL."""
    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                tconst,\n                title_type,\n                start_year,\n                primary_title,\n                poster_relative_path,\n                poster_local_path\n            FROM app.catalog_title_cards\n            WHERE tconst = ANY(%s)\n            ', (tconsts,))
        return cursor.fetchall()

def fetch_title_card_detail_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read richer lightweight title-card rows for many titles from PostgreSQL."""
    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                t.tconst,\n                t.title_type,\n                t.start_year,\n                t.primary_title,\n                t.original_title,\n                t.runtime_minutes,\n                t.genres,\n                t.average_rating,\n                t.num_votes,\n                c.poster_relative_path,\n                c.poster_local_path\n            FROM app.catalog_titles AS t\n            LEFT JOIN app.catalog_title_cards AS c ON c.tconst = t.tconst\n            WHERE t.tconst = ANY(%s)\n            ', (tconsts,))
        return cursor.fetchall()

def fetch_catalog_brief_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read lightweight catalog rows for many tconsts from PostgreSQL."""
    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT tconst, title_type, primary_title, start_year\n            FROM app.catalog_titles\n            WHERE tconst = ANY(%s)\n            ', (tconsts,))
        return cursor.fetchall()

def fetch_title_overviews(tconsts: list[str], *, primary_locale: str='cs-CZ', fallback_locale: str='en-US') -> dict[str, str]:
    """Read best available TMDB overview text for many titles from PostgreSQL."""
    if not tconsts:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT ranked.tconst, ranked.overview\n            FROM (\n                SELECT\n                    d.tconst,\n                    d.overview,\n                    row_number() OVER (\n                        PARTITION BY d.tconst\n                        ORDER BY\n                            CASE d.locale\n                                WHEN %s THEN 0\n                                WHEN %s THEN 1\n                                ELSE 2\n                            END,\n                            d.synced_at DESC\n                    ) AS rn\n                FROM app.tmdb_title_details AS d\n                WHERE d.tconst = ANY(%s)\n                  AND COALESCE(length(trim(d.overview)), 0) > 0\n            ) AS ranked\n            WHERE ranked.rn = 1\n            ', (primary_locale, fallback_locale, tconsts))
        rows = cursor.fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}

def fetch_continue_watching_catalog_rows(tconsts: list[str]) -> list[tuple[Any, ...]]:
    """Read continue-watching catalog/title+episode rows with latest posters from PostgreSQL."""
    if not tconsts:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            SELECT\n                COALESCE(t.tconst, e.episode_tconst) AS target_tconst,\n                COALESCE(t.title_type, 'tvEpisode') AS title_type,\n                COALESCE(t.primary_title, e.primary_title) AS primary_title,\n                COALESCE(t.original_title, e.original_title) AS original_title,\n                COALESCE(t.start_year, e.start_year) AS start_year,\n                COALESCE(t.end_year, NULL) AS end_year,\n                COALESCE(t.runtime_minutes, e.runtime_minutes) AS runtime_minutes,\n                t.genres,\n                t.average_rating,\n                t.num_votes,\n                e.series_tconst,\n                e.season_number,\n                e.episode_number,\n                s.primary_title AS series_title,\n                COALESCE(p.poster_relative_path, sp.poster_relative_path) AS poster_relative_path,\n                COALESCE(p.poster_local_path, sp.poster_local_path) AS poster_local_path\n            FROM app.catalog_titles AS t\n            LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = t.tconst\n            LEFT JOIN app.catalog_titles AS s ON s.tconst = e.series_tconst\n            LEFT JOIN app.latest_title_posters AS p ON p.tconst = t.tconst\n            LEFT JOIN app.latest_title_posters AS sp ON sp.tconst = e.series_tconst\n            WHERE t.tconst = ANY(%s)\n\n            UNION ALL\n\n            SELECT\n                e.episode_tconst AS target_tconst,\n                'tvEpisode' AS title_type,\n                e.primary_title AS primary_title,\n                e.original_title AS original_title,\n                e.start_year AS start_year,\n                NULL AS end_year,\n                e.runtime_minutes AS runtime_minutes,\n                NULL AS genres,\n                NULL AS average_rating,\n                NULL AS num_votes,\n                e.series_tconst,\n                e.season_number,\n                e.episode_number,\n                s.primary_title AS series_title,\n                COALESCE(p.poster_relative_path, sp.poster_relative_path) AS poster_relative_path,\n                COALESCE(p.poster_local_path, sp.poster_local_path) AS poster_local_path\n            FROM app.catalog_episodes AS e\n            LEFT JOIN app.catalog_titles AS s ON s.tconst = e.series_tconst\n            LEFT JOIN app.latest_title_posters AS p ON p.tconst = e.episode_tconst\n            LEFT JOIN app.latest_title_posters AS sp ON sp.tconst = e.series_tconst\n            WHERE e.episode_tconst = ANY(%s)\n            ", (tconsts, tconsts))
        return cursor.fetchall()

def fetch_watch_view_page_rows(*, limit: int, offset: int, cutoff_days: int | None) -> tuple[int, list[tuple[Any, ...]]]:
    """Read grouped watched/recently-watched rows directly from PostgreSQL."""
    cutoff_filter = ''
    params: list[Any] = []
    if cutoff_days is not None:
        cutoff_filter = "AND w.latest_watched_on >= current_date - (%s * INTERVAL '1 day')"
        params.append(cutoff_days)
    total_sql = f'\n        WITH grouped AS (\n            SELECT\n                w.display_tconst,\n                w.latest_created_at,\n                w.latest_watched_on\n            FROM app.watched_display_rollup AS w\n            WHERE w.display_tconst IS NOT NULL\n              {cutoff_filter}\n        )\n        SELECT COUNT(*)\n        FROM grouped AS g\n        JOIN app.catalog_title_cards AS c ON c.tconst = g.display_tconst\n        WHERE COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL\n    '
    rows_sql = f'\n        WITH grouped AS (\n            SELECT\n                w.display_tconst,\n                w.latest_created_at,\n                w.latest_watched_on\n            FROM app.watched_display_rollup AS w\n            WHERE w.display_tconst IS NOT NULL\n              {cutoff_filter}\n        )\n        SELECT\n            c.tconst,\n            c.title_type,\n            c.primary_title,\n            c.start_year,\n            c.poster_relative_path,\n            c.poster_local_path,\n            g.latest_watched_on,\n            g.latest_created_at\n        FROM grouped AS g\n        JOIN app.catalog_title_cards AS c ON c.tconst = g.display_tconst\n        WHERE COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL\n        ORDER BY g.latest_created_at DESC NULLS LAST, c.primary_title\n        LIMIT %s OFFSET %s\n    '
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(total_sql, params)
        total = int(cursor.fetchone()[0] or 0)
        cursor.execute(rows_sql, [*params, limit, offset])
        rows = cursor.fetchall()
    return (total, rows)

def fetch_hot_watchlist_page_rows(*, hot_limit: int, limit: int, offset: int) -> tuple[int, list[tuple[Any, ...]]]:
    """Read grouped hot-watchlist rows directly from PostgreSQL."""
    total_sql = "\n        WITH watched_titles AS (\n            SELECT display_tconst\n            FROM app.watched_display_rollup\n            WHERE display_tconst IS NOT NULL\n        ),\n        ranked_items AS (\n            SELECT\n                i.display_tconst,\n                row_number() OVER (\n                    PARTITION BY i.display_tconst\n                    ORDER BY i.added_at DESC NULLS LAST, i.updated_at DESC, COALESCE(i.title, i.parent_title, i.tconst)\n                ) AS group_row\n            FROM app.active_user_list_display_items AS i\n            WHERE i.list_id = 'watchlist'\n        )\n        SELECT COUNT(*)\n        FROM (\n            SELECT DISTINCT r.display_tconst\n            FROM ranked_items AS r\n            JOIN app.catalog_title_cards AS c ON c.tconst = r.display_tconst\n            LEFT JOIN watched_titles AS wt ON wt.display_tconst = r.display_tconst\n            WHERE r.group_row = 1\n              AND r.display_tconst IS NOT NULL\n              AND COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL\n              AND wt.display_tconst IS NULL\n            LIMIT %s\n        ) AS grouped\n    "
    rows_sql = "\n        WITH watched_titles AS (\n            SELECT display_tconst\n            FROM app.watched_display_rollup\n            WHERE display_tconst IS NOT NULL\n        ),\n        ranked_items AS (\n            SELECT\n                i.display_tconst,\n                i.media_type,\n                i.title,\n                i.parent_title,\n                i.rank,\n                i.added_at,\n                i.notes,\n                row_number() OVER (\n                    PARTITION BY i.display_tconst\n                    ORDER BY i.added_at DESC NULLS LAST, i.updated_at DESC, COALESCE(i.title, i.parent_title, i.tconst)\n                ) AS group_row\n            FROM app.active_user_list_display_items AS i\n            WHERE i.list_id = 'watchlist'\n        ),\n        grouped_items AS (\n            SELECT\n                r.display_tconst,\n                r.media_type,\n                r.title,\n                r.parent_title,\n                NULL AS season_number,\n                NULL AS episode_number,\n                r.rank,\n                r.added_at,\n                r.notes\n            FROM ranked_items AS r\n            JOIN app.catalog_title_cards AS c ON c.tconst = r.display_tconst\n            LEFT JOIN watched_titles AS wt ON wt.display_tconst = r.display_tconst\n            WHERE r.group_row = 1\n              AND r.display_tconst IS NOT NULL\n              AND COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL\n              AND wt.display_tconst IS NULL\n            ORDER BY r.added_at DESC NULLS LAST, COALESCE(r.title, r.parent_title, r.display_tconst)\n            LIMIT %s\n        )\n        SELECT\n            g.display_tconst,\n            g.media_type,\n            g.title,\n            g.parent_title,\n            g.season_number,\n            g.episode_number,\n            g.rank,\n            g.added_at,\n            g.notes,\n            'Hot Watchlist' AS name,\n            'view' AS list_kind,\n            c.title_type,\n            c.start_year,\n            c.poster_relative_path,\n            c.poster_local_path,\n            c.primary_title\n        FROM grouped_items AS g\n        JOIN app.catalog_title_cards AS c ON c.tconst = g.display_tconst\n        ORDER BY g.added_at DESC NULLS LAST, COALESCE(g.title, g.parent_title, g.display_tconst)\n        LIMIT %s OFFSET %s\n    "
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(total_sql, (hot_limit,))
        total = int(cursor.fetchone()[0] or 0)
        cursor.execute(rows_sql, (hot_limit, limit, offset))
        rows = cursor.fetchall()
    return (total, rows)

def fetch_library_status_projection(*, recently_watched_days: int, hot_watchlist_limit: int) -> dict[str, int]:
    """Read grouped library-status counts directly from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            WITH grouped_list_items AS (\n                SELECT i.list_id, i.display_tconst\n                FROM app.active_user_list_display_items AS i\n            ),\n            list_counts AS (\n                SELECT list_id, COUNT(DISTINCT display_tconst) AS item_count\n                FROM grouped_list_items\n                WHERE display_tconst IS NOT NULL\n                GROUP BY list_id\n            ),\n            poster_tconsts AS (\n                SELECT tconst\n                FROM app.catalog_title_cards\n                WHERE COALESCE(poster_relative_path, poster_local_path) IS NOT NULL\n            ),\n            watchlist_unwatched AS (\n                SELECT DISTINCT g.display_tconst\n                FROM grouped_list_items AS g\n                LEFT JOIN app.watched_display_rollup AS w ON w.display_tconst = g.display_tconst\n                WHERE g.list_id = 'watchlist'\n                  AND g.display_tconst IS NOT NULL\n                  AND w.display_tconst IS NULL\n            )\n            SELECT\n                COALESCE((SELECT item_count FROM list_counts WHERE list_id = 'watchlist'), 0) AS watchlist_item_count,\n                COALESCE((\n                    SELECT COUNT(*)\n                    FROM watchlist_unwatched AS wu\n                    JOIN poster_tconsts AS p ON p.tconst = wu.display_tconst\n                ), 0) AS watchlist_unwatched_with_poster_count,\n                COALESCE((\n                    SELECT COUNT(*)\n                    FROM app.watched_display_rollup AS w\n                    JOIN poster_tconsts AS p ON p.tconst = w.display_tconst\n                ), 0) AS watched_with_poster_count,\n                COALESCE((\n                    SELECT COUNT(*)\n                    FROM app.watched_display_rollup AS w\n                    JOIN poster_tconsts AS p ON p.tconst = w.display_tconst\n                    WHERE w.latest_watched_on >= current_date - (%s * INTERVAL '1 day')\n                ), 0) AS recently_watched_with_poster_count\n            ", (recently_watched_days,))
        row = cursor.fetchone()
    watchlist_count = int(row[0] or 0)
    hot_watchlist_with_poster_count = int(row[1] or 0)
    watched_count = int(row[2] or 0)
    recently_watched_count = int(row[3] or 0)
    return {'watchlist_count': watchlist_count, 'hot_watchlist_count': min(hot_watchlist_with_poster_count, hot_watchlist_limit), 'watched_count': watched_count, 'recently_watched_count': recently_watched_count}

def fetch_library_status_snapshot(*, recently_watched_days: int, hot_watchlist_limit: int) -> dict[str, Any]:
    """Read homepage/library summary counts and base-list item counts directly from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            WITH grouped_list_items AS (\n                SELECT i.list_id, i.display_tconst\n                FROM app.active_user_list_display_items AS i\n            ),\n            list_counts AS (\n                SELECT list_id, COUNT(DISTINCT display_tconst) AS item_count\n                FROM grouped_list_items\n                WHERE display_tconst IS NOT NULL\n                GROUP BY list_id\n            ),\n            poster_tconsts AS (\n                SELECT tconst\n                FROM app.catalog_title_cards\n                WHERE COALESCE(poster_relative_path, poster_local_path) IS NOT NULL\n            ),\n            watchlist_unwatched AS (\n                SELECT DISTINCT g.display_tconst\n                FROM grouped_list_items AS g\n                LEFT JOIN app.watched_display_rollup AS w ON w.display_tconst = g.display_tconst\n                WHERE g.list_id = 'watchlist'\n                  AND g.display_tconst IS NOT NULL\n                  AND w.display_tconst IS NULL\n            )\n            SELECT\n                (SELECT COUNT(*) FROM app.user_lists) AS lists_count,\n                (SELECT COUNT(*) FROM app.user_list_items WHERE is_archived = FALSE) AS list_items_count,\n                (SELECT COUNT(*) FROM app.user_ratings) AS ratings_count,\n                (SELECT COUNT(*) FROM app.user_people WHERE is_favorite = TRUE) AS favorite_people_count,\n                (SELECT COUNT(*) FROM app.watch_events) AS watch_events_count,\n                COALESCE((SELECT item_count FROM list_counts WHERE list_id = 'watchlist'), 0) AS watchlist_count,\n                COALESCE((\n                    SELECT COUNT(*)\n                    FROM watchlist_unwatched AS wu\n                    JOIN poster_tconsts AS p ON p.tconst = wu.display_tconst\n                ), 0) AS hot_watchlist_candidate_count,\n                COALESCE((\n                    SELECT COUNT(*)\n                    FROM app.watched_display_rollup AS w\n                    JOIN poster_tconsts AS p ON p.tconst = w.display_tconst\n                ), 0) AS watched_count,\n                COALESCE((\n                    SELECT COUNT(*)\n                    FROM app.watched_display_rollup AS w\n                    JOIN poster_tconsts AS p ON p.tconst = w.display_tconst\n                    WHERE w.latest_watched_on >= current_date - (%s * INTERVAL '1 day')\n                ), 0) AS recently_watched_count\n            ", (recently_watched_days,))
        counts_row = cursor.fetchone()
        cursor.execute('\n            WITH grouped_list_items AS (\n                SELECT\n                    i.list_id,\n                    COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AS display_tconst\n                FROM app.user_list_items AS i\n                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst\n                WHERE i.is_archived = FALSE\n            ),\n            list_counts AS (\n                SELECT list_id, COUNT(DISTINCT display_tconst) AS item_count\n                FROM grouped_list_items\n                WHERE display_tconst IS NOT NULL\n                GROUP BY list_id\n            )\n            SELECT\n                l.id,\n                l.slug,\n                l.name,\n                l.description,\n                l.list_kind,\n                l.ai_input_role,\n                COALESCE(c.item_count, 0) AS item_count\n            FROM app.user_lists AS l\n            LEFT JOIN list_counts AS c ON c.list_id = l.id\n            ORDER BY l.list_kind, l.name\n            ')
        list_rows = cursor.fetchall()
    return {'counts': {'lists': int(counts_row[0] or 0), 'list_items': int(counts_row[1] or 0), 'ratings': int(counts_row[2] or 0), 'favorite_people': int(counts_row[3] or 0), 'watch_events': int(counts_row[4] or 0), 'watchlist_items': int(counts_row[5] or 0)}, 'watchlist_count': int(counts_row[5] or 0), 'hot_watchlist_count': min(int(counts_row[6] or 0), hot_watchlist_limit), 'watched_count': int(counts_row[7] or 0), 'recently_watched_count': int(counts_row[8] or 0), 'base_lists': [{'id': row[0], 'slug': row[1], 'name': row[2], 'description': row[3], 'list_kind': row[4], 'ai_input_role': row[5], 'item_count': int(row[6] or 0), 'item_type': 'list'} for row in list_rows]}

def fetch_home_suggestion_candidate_rows(*, min_start_year: int, primary_locale: str='cs-CZ', fallback_locale: str='en-US') -> list[tuple[Any, ...]]:
    """Read compact unwatched homepage suggestion candidates directly from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            WITH latest_tmdb_details AS (\n                SELECT ranked.tconst, ranked.overview, ranked.release_date\n                FROM (\n                    SELECT\n                        d.tconst,\n                        d.overview,\n                        d.release_date,\n                        row_number() OVER (\n                        PARTITION BY d.tconst\n                        ORDER BY\n                            CASE d.locale\n                                WHEN %s THEN 0\n                                WHEN %s THEN 1\n                                ELSE 2\n                            END,\n                                d.synced_at DESC\n                        ) AS rn\n                    FROM app.tmdb_title_details AS d\n                ) AS ranked\n                WHERE ranked.rn = 1\n            ),\n            cz_provider_stats AS (\n                SELECT tconst, COUNT(*) AS cz_provider_count\n                FROM app.tmdb_watch_providers\n                WHERE country_code = 'CZ'\n                GROUP BY tconst\n            ),\n            title_watch_events AS (\n                SELECT\n                    COALESCE(e.series_tconst, w.tconst) AS tconst,\n                    COALESCE(w.created_at, w.watched_on::timestamp) AS watched_at\n                FROM app.watch_events AS w\n                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst\n                WHERE w.tconst IS NOT NULL\n            ),\n            title_watch_stats AS (\n                SELECT\n                    tconst,\n                    COUNT(*) AS watch_count\n                FROM title_watch_events\n                GROUP BY tconst\n            ),\n            rated_cast_affinity AS (\n                SELECT\n                    c.tconst,\n                    SUM(p.affinity_rating::double precision * CASE\n                        WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0\n                        ELSE 1.0 / sqrt(c.ordering::double precision)\n                    END)\n                    / NULLIF(\n                        SUM(CASE\n                            WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0\n                            ELSE 1.0 / sqrt(c.ordering::double precision)\n                        END),\n                        0.0\n                    ) AS actor_affinity_rating\n                FROM app.title_credits AS c\n                JOIN app.user_people AS p ON p.nconst = c.nconst\n                WHERE c.credit_group = 'cast'\n                  AND p.affinity_rating > 0\n                  AND (c.ordering IS NULL OR c.ordering <= 8)\n                GROUP BY c.tconst\n            )\n            SELECT\n                t.tconst,\n                t.title_type,\n                t.primary_title,\n                t.start_year,\n                t.genres,\n                t.average_rating,\n                t.num_votes,\n                d.overview,\n                d.release_date,\n                COALESCE(p.cz_provider_count, 0) AS cz_provider_count,\n                COALESCE(w.watch_count, 0) AS watch_count,\n                a.actor_affinity_rating\n            FROM app.catalog_titles AS t\n            LEFT JOIN latest_tmdb_details AS d ON d.tconst = t.tconst\n            LEFT JOIN cz_provider_stats AS p ON p.tconst = t.tconst\n            LEFT JOIN title_watch_stats AS w ON w.tconst = t.tconst\n            LEFT JOIN rated_cast_affinity AS a ON a.tconst = t.tconst\n            WHERE COALESCE(w.watch_count, 0) = 0\n              AND (\n                    COALESCE(length(trim(d.overview)), 0) > 0\n                    OR COALESCE(NULLIF(d.release_date::text, '')::date >= current_date - INTERVAL '540 day', FALSE)\n                    OR COALESCE(t.start_year, 0) >= %s\n                  )\n            ORDER BY\n                COALESCE(t.start_year, 0) DESC,\n                COALESCE(t.num_votes, 0) DESC,\n                COALESCE(t.average_rating, 0.0) DESC,\n                t.primary_title\n            LIMIT 3000\n            ", (primary_locale, fallback_locale, min_start_year))
        return cursor.fetchall()

def fetch_content_state(tconst: str) -> dict[str, Any] | None:
    """Read one content_state row from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                tconst,\n                interest_state,\n                last_previewed_at,\n                last_watched_at,\n                updated_at\n            FROM app.content_state\n            WHERE tconst = %s\n            ', (tconst,))
        row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_content_state(row)

def update_content_state(tconst: str, interest_state: str, now: str) -> dict[str, Any]:
    """Upsert one content_state row in PostgreSQL and return the stored state."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            INSERT INTO app.content_state (tconst, interest_state, last_previewed_at, last_watched_at, updated_at)\n            VALUES (\n                %s,\n                %s,\n                CASE WHEN %s = 'previewed' THEN %s::timestamp ELSE NULL END,\n                CASE WHEN %s = 'watched' THEN %s::timestamp ELSE NULL END,\n                %s::timestamp\n            )\n            ON CONFLICT (tconst) DO UPDATE SET\n                interest_state = excluded.interest_state,\n                updated_at = excluded.updated_at,\n                last_previewed_at = CASE\n                    WHEN excluded.interest_state = 'previewed' THEN excluded.updated_at\n                    ELSE app.content_state.last_previewed_at\n                END,\n                last_watched_at = CASE\n                    WHEN excluded.interest_state = 'watched' THEN excluded.updated_at\n                    ELSE app.content_state.last_watched_at\n                END\n            RETURNING\n                tconst,\n                interest_state,\n                last_previewed_at,\n                last_watched_at,\n                updated_at\n            ", (tconst, interest_state, interest_state, now, interest_state, now, now))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError('PostgreSQL content_state upsert nevrátil žádný řádek.')
        conn.commit()
    return _row_to_content_state(row)

def list_in_progress_content_states(limit: int | None=None) -> list[dict[str, Any]]:
    """Return ordered in-progress content_state rows from PostgreSQL."""
    sql = "\n        SELECT\n            tconst,\n            interest_state,\n            last_previewed_at,\n            last_watched_at,\n            updated_at\n        FROM app.content_state\n        WHERE interest_state = 'in_progress'\n        ORDER BY COALESCE(last_previewed_at, updated_at) DESC, COALESCE(last_watched_at, updated_at) DESC\n    "
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += '\nLIMIT %s'
        params = (limit,)
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    return [_row_to_content_state(row) for row in rows]

def upsert_user_rating(*, canonical_key: str, tconst: str | None, media_type: str, imdb_id: str | None, tmdb_id: int | None, trakt_id: int | None, parent_tconst: str | None, parent_title: str | None, title: str | None, season_number: int | None, episode_number: int | None, rating: int, rated_at: str | None, source_origin: str, source_ref: str | None, now: str, liked_notes: str | None=None, disliked_notes: str | None=None) -> dict[str, Any]:
    """Upsert one app.user_ratings row in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            INSERT INTO app.user_ratings (\n                canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title, title,\n                season_number, episode_number, rating, liked_notes, disliked_notes, rated_at,\n                source_origin, source_ref, created_at, updated_at\n            )\n            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamp, %s, %s, %s::timestamp, %s::timestamp)\n            ON CONFLICT (canonical_key) DO UPDATE SET\n                tconst = COALESCE(app.user_ratings.tconst, excluded.tconst),\n                imdb_id = COALESCE(app.user_ratings.imdb_id, excluded.imdb_id),\n                tmdb_id = COALESCE(app.user_ratings.tmdb_id, excluded.tmdb_id),\n                trakt_id = COALESCE(app.user_ratings.trakt_id, excluded.trakt_id),\n                parent_tconst = COALESCE(app.user_ratings.parent_tconst, excluded.parent_tconst),\n                parent_title = COALESCE(app.user_ratings.parent_title, excluded.parent_title),\n                title = COALESCE(app.user_ratings.title, excluded.title),\n                rating = excluded.rating,\n                liked_notes = excluded.liked_notes,\n                disliked_notes = excluded.disliked_notes,\n                rated_at = COALESCE(excluded.rated_at, app.user_ratings.rated_at),\n                updated_at = excluded.updated_at\n            RETURNING\n                canonical_key,\n                tconst,\n                rating,\n                liked_notes,\n                disliked_notes,\n                rated_at,\n                updated_at\n            ', (canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title, title, season_number, episode_number, rating, liked_notes, disliked_notes, rated_at, source_origin, source_ref, now, now))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError('PostgreSQL user_ratings upsert nevrátil žádný řádek.')
        conn.commit()
    return _row_to_user_rating(row)

def delete_user_rating(canonical_key: str) -> None:
    """Delete one app.user_ratings row by canonical key in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('DELETE FROM app.user_ratings WHERE canonical_key = %s', (canonical_key,))
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
        cursor.execute('\n            SELECT DISTINCT ON (tconst)\n                tconst,\n                rating,\n                liked_notes,\n                disliked_notes,\n                rated_at,\n                updated_at\n            FROM app.user_ratings\n            WHERE tconst = ANY(%s)\n              AND tconst IS NOT NULL\n            ORDER BY tconst, rated_at DESC NULLS LAST, updated_at DESC, created_at DESC\n            ', (clean_tconsts,))
        rows = cursor.fetchall()
    return {str(row[0]): {'tconst': row[0], 'rating': row[1], 'liked_notes': row[2], 'disliked_notes': row[3], 'rated_at': _parse_optional_timestamp(row[4]), 'updated_at': _parse_optional_timestamp(row[5])} for row in rows if row[0] is not None}

def fetch_ai_taste_seed_rows(*, source_list: str, limit: int) -> dict[str, Any]:
    """Return compact user-taste examples for an external AI recommender.

    The source list can be a user-list id, slug, or exact name. The payload is
    intentionally read-only and carries local signals; it does not decide new
    recommendations inside this app.
    """
    normalized_source = str(source_list or '').strip()
    if not normalized_source:
        normalized_source = 'kouknout-znovu'
    source_aliases = [normalized_source]
    if normalized_source.casefold() == 'kouknout-znovu':
        source_aliases.append('kouknout-znou')
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT id, slug, name, description, list_kind, ai_input_role\n            FROM app.user_lists\n            WHERE id = ANY(%s)\n               OR slug = ANY(%s)\n               OR lower(name) = ANY(%s)\n            ORDER BY\n                CASE\n                    WHEN id = %s THEN 0\n                    WHEN slug = %s THEN 1\n                    ELSE 2\n                END\n            LIMIT 1\n            ', (source_aliases, source_aliases, [item.casefold().replace('-', ' ') for item in source_aliases], normalized_source, normalized_source))
        list_row = cursor.fetchone()
        if list_row is None:
            return {'source_list': {'query': normalized_source, 'found': False}, 'items': [], 'limit': limit}
        cursor.execute("\n            WITH ranked_items AS (\n                SELECT\n                    i.display_tconst,\n                    i.tmdb_id,\n                    i.added_at,\n                    i.rank,\n                    row_number() OVER (\n                        PARTITION BY i.display_tconst\n                        ORDER BY i.rank NULLS LAST, i.added_at DESC NULLS LAST, COALESCE(i.title, i.parent_title, i.tconst)\n                    ) AS group_row\n                FROM app.active_user_list_display_items AS i\n                WHERE i.list_id = %s\n                  AND i.display_tconst IS NOT NULL\n            ),\n            latest_ratings AS (\n                SELECT DISTINCT ON (tconst)\n                    tconst,\n                    rating,\n                    liked_notes,\n                    disliked_notes,\n                    rated_at,\n                    updated_at\n                FROM app.user_ratings\n                WHERE tconst IS NOT NULL\n                ORDER BY tconst, rated_at DESC NULLS LAST, updated_at DESC, created_at DESC\n            ),\n            latest_scores AS (\n                SELECT DISTINCT ON (genre)\n                    genre,\n                    final_score,\n                    rating_signal_score,\n                    watch_signal_score,\n                    actor_affinity_score,\n                    generated_at\n                FROM app.genre_scores\n                WHERE score_scope = 'default'\n                ORDER BY genre, generated_at DESC, rank_in_run ASC\n            ),\n            title_people_affinity AS (\n                SELECT\n                    c.tconst,\n                    ROUND(\n                        AVG(p.affinity_rating) FILTER (\n                            WHERE c.credit_group = 'cast'\n                              AND (c.ordering IS NULL OR c.ordering <= 8)\n                        )::numeric,\n                        3\n                    )::double precision AS actor_affinity_rating,\n                    jsonb_agg(\n                        jsonb_build_object(\n                            'nconst', c.nconst,\n                            'name', person.primary_name,\n                            'credit_group', c.credit_group,\n                            'ordering', c.ordering,\n                            'affinity_rating', p.affinity_rating,\n                            'is_favorite', p.is_favorite\n                        )\n                        ORDER BY\n                            CASE c.credit_group\n                                WHEN 'director' THEN 0\n                                WHEN 'creator' THEN 1\n                                WHEN 'writer' THEN 2\n                                WHEN 'cast' THEN 3\n                                ELSE 4\n                            END,\n                            c.ordering NULLS LAST,\n                            person.primary_name\n                    ) AS people_affinity\n                FROM app.title_credits AS c\n                JOIN app.user_people AS p ON p.nconst = c.nconst\n                JOIN app.catalog_people AS person ON person.nconst = c.nconst\n                WHERE p.affinity_rating > 0\n                GROUP BY c.tconst\n            ),\n            title_role_signals AS (\n                SELECT\n                    s.tconst,\n                    jsonb_agg(\n                        jsonb_build_object(\n                            'signal_key', s.signal_key,\n                            'nconst', s.nconst,\n                            'person_name', person.primary_name,\n                            'character_name', s.character_name,\n                            'signal_type', s.signal_type,\n                            'polarity', s.polarity,\n                            'strength', s.strength,\n                            'notes', s.notes,\n                            'source_origin', s.source_origin,\n                            'source_ref', s.source_ref,\n                            'updated_at', s.updated_at\n                        )\n                        ORDER BY s.strength DESC, s.updated_at DESC, s.character_name NULLS LAST, s.signal_type\n                    ) AS title_role_signals\n                FROM app.user_title_role_signals AS s\n                LEFT JOIN app.catalog_people AS person ON person.nconst = s.nconst\n                GROUP BY s.tconst\n            )\n            SELECT\n                r.display_tconst,\n                t.primary_title,\n                t.original_title,\n                t.title_type,\n                t.start_year,\n                t.genres,\n                t.average_rating,\n                t.num_votes,\n                COALESCE(map.tmdb_id, r.tmdb_id),\n                lr.rating,\n                lr.liked_notes,\n                lr.disliked_notes,\n                lr.rated_at,\n                pa.actor_affinity_rating,\n                COALESCE(pa.people_affinity, '[]'::jsonb) AS people_affinity,\n                COALESCE(trs.title_role_signals, '[]'::jsonb) AS title_role_signals,\n                COALESCE(\n                    jsonb_agg(\n                        DISTINCT jsonb_build_object(\n                            'genre', score.genre,\n                            'final_score', score.final_score,\n                            'rating_signal_score', score.rating_signal_score,\n                            'watch_signal_score', score.watch_signal_score,\n                            'actor_affinity_score', score.actor_affinity_score\n                        )\n                    ) FILTER (WHERE score.genre IS NOT NULL),\n                    '[]'::jsonb\n                ) AS genre_score_signals\n            FROM ranked_items AS r\n            JOIN app.catalog_titles AS t ON t.tconst = r.display_tconst\n            LEFT JOIN app.tmdb_title_map AS map ON map.tconst = r.display_tconst\n            LEFT JOIN latest_ratings AS lr ON lr.tconst = r.display_tconst\n            LEFT JOIN title_people_affinity AS pa ON pa.tconst = r.display_tconst\n            LEFT JOIN title_role_signals AS trs ON trs.tconst = r.display_tconst\n            LEFT JOIN latest_scores AS score ON score.genre = ANY(string_to_array(COALESCE(t.genres, ''), ','))\n            WHERE r.group_row = 1\n            GROUP BY\n                r.display_tconst,\n                t.primary_title,\n                t.original_title,\n                t.title_type,\n                t.start_year,\n                t.genres,\n                t.average_rating,\n                t.num_votes,\n                map.tmdb_id,\n                r.tmdb_id,\n                lr.rating,\n                lr.liked_notes,\n                lr.disliked_notes,\n                lr.rated_at,\n                pa.actor_affinity_rating,\n                pa.people_affinity,\n                trs.title_role_signals,\n                r.rank,\n                r.added_at\n            ORDER BY r.rank NULLS LAST, r.added_at DESC NULLS LAST, t.primary_title\n            LIMIT %s\n            ", (list_row[0], limit))
        rows = cursor.fetchall()
    return {'source_list': {'query': normalized_source, 'found': True, 'id': list_row[0], 'slug': list_row[1], 'name': list_row[2], 'description': list_row[3], 'list_kind': list_row[4], 'ai_input_role': list_row[5]}, 'limit': limit, 'items': [{'imdb_id': row[0], 'tconst': row[0], 'tmdb_id': row[8], 'title': row[1], 'original_title': row[2], 'title_type': row[3], 'year': row[4], 'genres': [genre for genre in str(row[5] or '').split(',') if genre], 'imdb_rating': row[6], 'imdb_votes': row[7], 'user_rating': row[9], 'liked_notes': row[10], 'disliked_notes': row[11], 'rated_at': _parse_optional_timestamp(row[12]), 'actor_affinity_rating': row[13], 'people_affinity': row[14] or [], 'title_role_signals': row[15] or [], 'genre_score_signals': row[16] or []} for row in rows]}

def fetch_ai_rated_title_rows(*, min_user_rating: int, limit: int, title_type: str | None=None) -> dict[str, Any]:
    """Return locally rated titles for an external AI recommender."""
    type_filter = ''
    params: list[Any] = [min_user_rating]
    if title_type:
        type_filter = 'AND t.title_type = %s'
        params.append(title_type)
    params.append(limit)
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(f"\n            WITH latest_ratings AS (\n                SELECT DISTINCT ON (tconst)\n                    tconst,\n                    rating,\n                    liked_notes,\n                    disliked_notes,\n                    rated_at,\n                    updated_at\n                FROM app.user_ratings\n                WHERE tconst IS NOT NULL\n                ORDER BY tconst, rated_at DESC NULLS LAST, updated_at DESC, created_at DESC\n            ),\n            latest_scores AS (\n                SELECT DISTINCT ON (genre)\n                    genre,\n                    final_score,\n                    rating_signal_score,\n                    watch_signal_score,\n                    actor_affinity_score,\n                    generated_at\n                FROM app.genre_scores\n                WHERE score_scope = 'default'\n                ORDER BY genre, generated_at DESC, rank_in_run ASC\n            ),\n            title_people_affinity AS (\n                SELECT\n                    c.tconst,\n                    ROUND(\n                        AVG(p.affinity_rating) FILTER (\n                            WHERE c.credit_group = 'cast'\n                              AND (c.ordering IS NULL OR c.ordering <= 8)\n                        )::numeric,\n                        3\n                    )::double precision AS actor_affinity_rating,\n                    jsonb_agg(\n                        jsonb_build_object(\n                            'nconst', c.nconst,\n                            'name', person.primary_name,\n                            'credit_group', c.credit_group,\n                            'ordering', c.ordering,\n                            'affinity_rating', p.affinity_rating,\n                            'is_favorite', p.is_favorite\n                        )\n                        ORDER BY\n                            CASE c.credit_group\n                                WHEN 'director' THEN 0\n                                WHEN 'creator' THEN 1\n                                WHEN 'writer' THEN 2\n                                WHEN 'cast' THEN 3\n                                ELSE 4\n                            END,\n                            c.ordering NULLS LAST,\n                            person.primary_name\n                    ) AS people_affinity\n                FROM app.title_credits AS c\n                JOIN app.user_people AS p ON p.nconst = c.nconst\n                JOIN app.catalog_people AS person ON person.nconst = c.nconst\n                WHERE p.affinity_rating > 0\n                GROUP BY c.tconst\n            ),\n            title_role_signals AS (\n                SELECT\n                    s.tconst,\n                    jsonb_agg(\n                        jsonb_build_object(\n                            'signal_key', s.signal_key,\n                            'nconst', s.nconst,\n                            'person_name', person.primary_name,\n                            'character_name', s.character_name,\n                            'signal_type', s.signal_type,\n                            'polarity', s.polarity,\n                            'strength', s.strength,\n                            'notes', s.notes,\n                            'source_origin', s.source_origin,\n                            'source_ref', s.source_ref,\n                            'updated_at', s.updated_at\n                        )\n                        ORDER BY s.strength DESC, s.updated_at DESC, s.character_name NULLS LAST, s.signal_type\n                    ) AS title_role_signals\n                FROM app.user_title_role_signals AS s\n                LEFT JOIN app.catalog_people AS person ON person.nconst = s.nconst\n                GROUP BY s.tconst\n            )\n            SELECT\n                t.tconst,\n                t.primary_title,\n                t.original_title,\n                t.title_type,\n                t.start_year,\n                t.genres,\n                t.average_rating,\n                t.num_votes,\n                map.tmdb_id,\n                lr.rating,\n                lr.liked_notes,\n                lr.disliked_notes,\n                lr.rated_at,\n                pa.actor_affinity_rating,\n                COALESCE(pa.people_affinity, '[]'::jsonb) AS people_affinity,\n                COALESCE(trs.title_role_signals, '[]'::jsonb) AS title_role_signals,\n                COALESCE(\n                    jsonb_agg(\n                        DISTINCT jsonb_build_object(\n                            'genre', score.genre,\n                            'final_score', score.final_score,\n                            'rating_signal_score', score.rating_signal_score,\n                            'watch_signal_score', score.watch_signal_score,\n                            'actor_affinity_score', score.actor_affinity_score\n                        )\n                    ) FILTER (WHERE score.genre IS NOT NULL),\n                    '[]'::jsonb\n                ) AS genre_score_signals\n            FROM latest_ratings AS lr\n            JOIN app.catalog_titles AS t ON t.tconst = lr.tconst\n            LEFT JOIN app.tmdb_title_map AS map ON map.tconst = t.tconst\n            LEFT JOIN title_people_affinity AS pa ON pa.tconst = t.tconst\n            LEFT JOIN title_role_signals AS trs ON trs.tconst = t.tconst\n            LEFT JOIN latest_scores AS score ON score.genre = ANY(string_to_array(COALESCE(t.genres, ''), ','))\n            WHERE lr.rating >= %s\n              {type_filter}\n            GROUP BY\n                t.tconst,\n                t.primary_title,\n                t.original_title,\n                t.title_type,\n                t.start_year,\n                t.genres,\n                t.average_rating,\n                t.num_votes,\n                map.tmdb_id,\n                lr.rating,\n                lr.liked_notes,\n                lr.disliked_notes,\n                lr.rated_at,\n                pa.actor_affinity_rating,\n                pa.people_affinity,\n                trs.title_role_signals\n            ORDER BY lr.rating DESC, lr.rated_at DESC NULLS LAST, t.primary_title\n            LIMIT %s\n            ", params)
        rows = cursor.fetchall()
    return {'filters': {'min_user_rating': min_user_rating, 'title_type': title_type}, 'limit': limit, 'items': [{'imdb_id': row[0], 'tconst': row[0], 'tmdb_id': row[8], 'title': row[1], 'original_title': row[2], 'title_type': row[3], 'year': row[4], 'genres': [genre for genre in str(row[5] or '').split(',') if genre], 'imdb_rating': row[6], 'imdb_votes': row[7], 'user_rating': row[9], 'liked_notes': row[10], 'disliked_notes': row[11], 'rated_at': _parse_optional_timestamp(row[12]), 'actor_affinity_rating': row[13], 'people_affinity': row[14] or [], 'title_role_signals': row[15] or [], 'genre_score_signals': row[16] or []} for row in rows]}

def fetch_ai_noted_title_rows(*, notes: str, min_user_rating: int | None, limit: int) -> dict[str, Any]:
    """Return locally noted titles for an external AI recommender."""
    note_mode = notes if notes in {'any', 'liked', 'disliked'} else 'any'
    note_filters = {'any': "(NULLIF(BTRIM(COALESCE(lr.liked_notes, '')), '') IS NOT NULL OR NULLIF(BTRIM(COALESCE(lr.disliked_notes, '')), '') IS NOT NULL)", 'liked': "NULLIF(BTRIM(COALESCE(lr.liked_notes, '')), '') IS NOT NULL", 'disliked': "NULLIF(BTRIM(COALESCE(lr.disliked_notes, '')), '') IS NOT NULL"}
    rating_filter = ''
    params: list[Any] = []
    if min_user_rating is not None:
        rating_filter = 'AND lr.rating >= %s'
        params.append(min_user_rating)
    params.append(limit)
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(f"\n            WITH latest_ratings AS (\n                SELECT DISTINCT ON (tconst)\n                    tconst,\n                    rating,\n                    liked_notes,\n                    disliked_notes,\n                    rated_at,\n                    updated_at\n                FROM app.user_ratings\n                WHERE tconst IS NOT NULL\n                ORDER BY tconst, rated_at DESC NULLS LAST, updated_at DESC, created_at DESC\n            ),\n            latest_scores AS (\n                SELECT DISTINCT ON (genre)\n                    genre,\n                    final_score,\n                    rating_signal_score,\n                    watch_signal_score,\n                    actor_affinity_score,\n                    generated_at\n                FROM app.genre_scores\n                WHERE score_scope = 'default'\n                ORDER BY genre, generated_at DESC, rank_in_run ASC\n            ),\n            title_people_affinity AS (\n                SELECT\n                    c.tconst,\n                    ROUND(\n                        AVG(p.affinity_rating) FILTER (\n                            WHERE c.credit_group = 'cast'\n                              AND (c.ordering IS NULL OR c.ordering <= 8)\n                        )::numeric,\n                        3\n                    )::double precision AS actor_affinity_rating,\n                    jsonb_agg(\n                        jsonb_build_object(\n                            'nconst', c.nconst,\n                            'name', person.primary_name,\n                            'credit_group', c.credit_group,\n                            'ordering', c.ordering,\n                            'affinity_rating', p.affinity_rating,\n                            'is_favorite', p.is_favorite\n                        )\n                        ORDER BY\n                            CASE c.credit_group\n                                WHEN 'director' THEN 0\n                                WHEN 'creator' THEN 1\n                                WHEN 'writer' THEN 2\n                                WHEN 'cast' THEN 3\n                                ELSE 4\n                            END,\n                            c.ordering NULLS LAST,\n                            person.primary_name\n                    ) AS people_affinity\n                FROM app.title_credits AS c\n                JOIN app.user_people AS p ON p.nconst = c.nconst\n                JOIN app.catalog_people AS person ON person.nconst = c.nconst\n                WHERE p.affinity_rating > 0\n                GROUP BY c.tconst\n            ),\n            title_role_signals AS (\n                SELECT\n                    s.tconst,\n                    jsonb_agg(\n                        jsonb_build_object(\n                            'signal_key', s.signal_key,\n                            'nconst', s.nconst,\n                            'person_name', person.primary_name,\n                            'character_name', s.character_name,\n                            'signal_type', s.signal_type,\n                            'polarity', s.polarity,\n                            'strength', s.strength,\n                            'notes', s.notes,\n                            'source_origin', s.source_origin,\n                            'source_ref', s.source_ref,\n                            'updated_at', s.updated_at\n                        )\n                        ORDER BY s.strength DESC, s.updated_at DESC, s.character_name NULLS LAST, s.signal_type\n                    ) AS title_role_signals\n                FROM app.user_title_role_signals AS s\n                LEFT JOIN app.catalog_people AS person ON person.nconst = s.nconst\n                GROUP BY s.tconst\n            )\n            SELECT\n                t.tconst,\n                t.primary_title,\n                t.original_title,\n                t.title_type,\n                t.start_year,\n                t.genres,\n                t.average_rating,\n                t.num_votes,\n                map.tmdb_id,\n                lr.rating,\n                lr.liked_notes,\n                lr.disliked_notes,\n                lr.rated_at,\n                pa.actor_affinity_rating,\n                COALESCE(pa.people_affinity, '[]'::jsonb) AS people_affinity,\n                COALESCE(trs.title_role_signals, '[]'::jsonb) AS title_role_signals,\n                COALESCE(\n                    jsonb_agg(\n                        DISTINCT jsonb_build_object(\n                            'genre', score.genre,\n                            'final_score', score.final_score,\n                            'rating_signal_score', score.rating_signal_score,\n                            'watch_signal_score', score.watch_signal_score,\n                            'actor_affinity_score', score.actor_affinity_score\n                        )\n                    ) FILTER (WHERE score.genre IS NOT NULL),\n                    '[]'::jsonb\n                ) AS genre_score_signals\n            FROM latest_ratings AS lr\n            JOIN app.catalog_titles AS t ON t.tconst = lr.tconst\n            LEFT JOIN app.tmdb_title_map AS map ON map.tconst = t.tconst\n            LEFT JOIN title_people_affinity AS pa ON pa.tconst = t.tconst\n            LEFT JOIN title_role_signals AS trs ON trs.tconst = t.tconst\n            LEFT JOIN latest_scores AS score ON score.genre = ANY(string_to_array(COALESCE(t.genres, ''), ','))\n            WHERE {note_filters[note_mode]}\n              {rating_filter}\n            GROUP BY\n                t.tconst,\n                t.primary_title,\n                t.original_title,\n                t.title_type,\n                t.start_year,\n                t.genres,\n                t.average_rating,\n                t.num_votes,\n                map.tmdb_id,\n                lr.rating,\n                lr.liked_notes,\n                lr.disliked_notes,\n                lr.rated_at,\n                lr.updated_at,\n                pa.actor_affinity_rating,\n                pa.people_affinity,\n                trs.title_role_signals\n            ORDER BY\n                (\n                    NULLIF(BTRIM(COALESCE(lr.liked_notes, '')), '') IS NOT NULL\n                    AND NULLIF(BTRIM(COALESCE(lr.disliked_notes, '')), '') IS NOT NULL\n                ) DESC,\n                lr.rated_at DESC NULLS LAST,\n                lr.updated_at DESC NULLS LAST,\n                lr.rating DESC,\n                t.primary_title\n            LIMIT %s\n            ", params)
        rows = cursor.fetchall()
    return {'filters': {'notes': note_mode, 'min_user_rating': min_user_rating}, 'limit': limit, 'items': [{'imdb_id': row[0], 'tconst': row[0], 'tmdb_id': row[8], 'title': row[1], 'original_title': row[2], 'title_type': row[3], 'year': row[4], 'genres': [genre for genre in str(row[5] or '').split(',') if genre], 'imdb_rating': row[6], 'imdb_votes': row[7], 'user_rating': row[9], 'liked_notes': row[10], 'disliked_notes': row[11], 'rated_at': _parse_optional_timestamp(row[12]), 'actor_affinity_rating': row[13], 'people_affinity': row[14] or [], 'title_role_signals': row[15] or [], 'genre_score_signals': row[16] or []} for row in rows]}

def insert_watch_event(*, event_id: str, tconst: str, event_scope: str, watched_on: str, notes: str | None, created_at: str) -> dict[str, Any]:
    """Insert one local watch event and sync content_state in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            INSERT INTO app.watch_events (\n                id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at\n            )\n            VALUES (%s, %s, %s, %s::date, 'local_app', NULL, NULL, NULL, %s, %s::timestamp)\n            RETURNING id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at\n            ", (event_id, tconst, event_scope, watched_on, notes, created_at))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError('PostgreSQL watch_events insert nevrátil žádný řádek.')
        cursor.execute("\n            INSERT INTO app.content_state (tconst, interest_state, last_previewed_at, last_watched_at, updated_at)\n            VALUES (%s, 'watched', NULL, %s::timestamp, %s::timestamp)\n            ON CONFLICT (tconst) DO UPDATE SET\n                interest_state = 'watched',\n                last_watched_at = excluded.last_watched_at,\n                updated_at = excluded.updated_at\n            ", (tconst, created_at, created_at))
        conn.commit()
    return _row_to_watch_event(row)

def record_watched(*, event_id: str, tconst: str, event_scope: str, watched_on: str, notes: str | None, created_at: str, archive_from_list_id: str | None=None, archive_canonical_key: str | None=None, archive_display_tconst: str | None=None) -> dict[str, Any]:
    """Run the server-side watched action in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT event_id, content_state_changed, archived_items\n            FROM app.record_watched(\n                %s,\n                %s,\n                %s,\n                %s::date,\n                %s,\n                %s::timestamp,\n                %s,\n                %s,\n                %s\n            )\n            ', (event_id, tconst, event_scope, watched_on, notes, created_at, archive_from_list_id, archive_canonical_key, archive_display_tconst))
        row = cursor.fetchone()
        conn.commit()
    if row is None:
        raise RuntimeError('PostgreSQL record_watched nevratil vysledek.')
    return {'event_id': str(row[0]), 'content_state_changed': bool(row[1]), 'archived_items': int(row[2] or 0)}

def insert_watch_events(events: list[dict[str, Any]], *, created_at: str) -> list[dict[str, Any]]:
    """Insert multiple local watch events and sync their content_state rows."""
    if not events:
        return []
    with _connect() as conn, conn.cursor() as cursor:
        inserted: list[dict[str, Any]] = []
        for event in events:
            cursor.execute("\n                INSERT INTO app.watch_events (\n                    id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at\n                )\n                VALUES (%s, %s, %s, %s::date, 'local_app', NULL, NULL, NULL, %s, %s::timestamp)\n                RETURNING id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at\n                ", (event['id'], event['tconst'], event['event_scope'], event['watched_on'], event.get('notes'), created_at))
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError('PostgreSQL watch_events batch insert nevrátil řádek.')
            inserted.append(_row_to_watch_event(row))
        cursor.executemany("\n            INSERT INTO app.content_state (tconst, interest_state, last_previewed_at, last_watched_at, updated_at)\n            VALUES (%s, 'watched', NULL, %s::timestamp, %s::timestamp)\n            ON CONFLICT (tconst) DO UPDATE SET\n                interest_state = 'watched',\n                last_watched_at = excluded.last_watched_at,\n                updated_at = excluded.updated_at\n            ", [(event['tconst'], created_at, created_at) for event in events])
        conn.commit()
    return inserted

def fetch_watch_history(limit: int=100, source: str | None=None) -> list[dict[str, Any]]:
    """Read latest watch history rows from PostgreSQL."""
    sql = '\n        SELECT id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at\n        FROM app.watch_events\n        {where_clause}\n        ORDER BY watched_on DESC, created_at DESC\n        LIMIT %s\n    '
    where_clause = '' if source is None else 'WHERE source = %s'
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
        cursor.execute('SELECT DISTINCT tconst FROM app.watch_events WHERE tconst = ANY(%s)', (clean_tconsts,))
        rows = cursor.fetchall()
    return {str(row[0]) for row in rows if row[0] is not None}

def fetch_watch_stats_for_tconsts(tconsts: list[str]) -> dict[str, dict[str, Any]]:
    """Return count and last watch timestamp for the given raw event tconsts."""
    clean_tconsts = [str(tconst).strip() for tconst in tconsts if str(tconst).strip()]
    if not clean_tconsts:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                tconst,\n                COUNT(*) AS watched_count,\n                MAX(created_at) AS last_watched_at\n            FROM app.watch_events\n            WHERE tconst = ANY(%s)\n            GROUP BY tconst\n            ', (clean_tconsts,))
        rows = cursor.fetchall()
    return {str(row[0]): {'tconst': row[0], 'watched_count': int(row[1]), 'last_watched_at': _parse_optional_timestamp(row[2])} for row in rows if row[0] is not None}

def fetch_library_summary_snapshot(tconst: str, title_type: str | None) -> dict[str, Any]:
    """Return one PostgreSQL-backed library summary for a title or episode."""
    if title_type in ('tvSeries', 'tvMiniSeries'):
        watch_sql = '\n            SELECT COUNT(*), MAX(w.created_at)\n            FROM app.watch_events AS w\n            JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst\n            WHERE e.series_tconst = %s\n        '
    else:
        watch_sql = '\n            SELECT COUNT(*), MAX(created_at)\n            FROM app.watch_events\n            WHERE tconst = %s\n        '
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(watch_sql, (tconst,))
        watch_row = cursor.fetchone()
        watched_count = int((watch_row[0] if watch_row is not None else 0) or 0)
        last_watched_at = _parse_optional_timestamp(watch_row[1] if watch_row is not None else None)
        cursor.execute("\n            SELECT l.name, l.list_kind, i.rank, i.added_at\n            FROM app.user_list_items AS i\n            JOIN app.user_lists AS l ON l.id = i.list_id\n            WHERE i.tconst = %s\n              AND i.is_archived = FALSE\n              AND l.list_kind <> 'watchlist'\n            ORDER BY\n                CASE WHEN i.rank IS NULL THEN 1 ELSE 0 END,\n                i.rank,\n                i.added_at DESC NULLS LAST,\n                l.name\n            LIMIT 20\n            ", (tconst,))
        list_rows = cursor.fetchall()
        cursor.execute("\n            SELECT EXISTS(\n                SELECT 1\n                FROM app.user_list_items AS i\n                JOIN app.user_lists AS l ON l.id = i.list_id\n                WHERE i.tconst = %s\n                  AND i.is_archived = FALSE\n                  AND l.list_kind = 'watchlist'\n            )\n            ", (tconst,))
        watchlist_row = cursor.fetchone()
        raw_in_watchlist = bool(watchlist_row[0]) if watchlist_row is not None else False
        cursor.execute('\n            SELECT rating, liked_notes, disliked_notes, rated_at\n            FROM app.user_ratings\n            WHERE tconst = %s\n            ORDER BY rated_at DESC NULLS LAST, updated_at DESC, created_at DESC\n            LIMIT 1\n            ', (tconst,))
        rating_row = cursor.fetchone()
    return {'watched_count': watched_count, 'last_watched_at': last_watched_at, 'in_watchlist': raw_in_watchlist and watched_count == 0, 'rating': {'value': rating_row[0], 'liked_notes': rating_row[1], 'disliked_notes': rating_row[2], 'rated_at': _parse_optional_timestamp(rating_row[3])} if rating_row is not None else None, 'lists': [{'name': row[0], 'kind': row[1], 'rank': row[2], 'added_at': _parse_optional_timestamp(row[3])} for row in list_rows]}

def fetch_all_watch_events(source: str | None=None) -> list[dict[str, Any]]:
    """Return all watch events ordered newest-first for overlay/aggregation use."""
    sql = '\n        SELECT id, tconst, event_scope, watched_on, source, batch_id, import_row_id, rating, notes, created_at\n        FROM app.watch_events\n        {where_clause}\n        ORDER BY watched_on DESC, created_at DESC\n    '
    where_clause = '' if source is None else 'WHERE source = %s'
    params: tuple[Any, ...] = () if source is None else (source,)
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(sql.format(where_clause=where_clause), params)
        rows = cursor.fetchall()
    return [_row_to_watch_event(row) for row in rows]

def fetch_user_list(list_id: str) -> dict[str, Any] | None:
    """Read one user list row from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT id, slug, name, description, list_kind, ai_input_role\n            FROM app.user_lists\n            WHERE id = %s\n            ', (list_id,))
        row = cursor.fetchone()
    if row is None:
        return None
    return {'id': row[0], 'slug': row[1], 'name': row[2], 'description': row[3], 'list_kind': row[4], 'ai_input_role': row[5]}

def fetch_user_list_page_rows(*, list_id: str, limit: int, offset: int, exclude_watched: bool) -> tuple[dict[str, Any] | None, int, list[tuple[Any, ...]]]:
    """Read one grouped user-list page directly from PostgreSQL.

    The normal path gets page rows and the filtered total in one scan. A small
    count fallback is kept only for out-of-range offsets where a window count
    cannot be returned with the empty page.
    """
    watched_cte = ''
    watched_join = ''
    watched_filter = ''
    if exclude_watched:
        watched_cte = '\n            ,\n            watched_titles AS (\n                SELECT display_tconst\n                FROM app.watched_display_rollup\n                WHERE display_tconst IS NOT NULL\n            )\n        '
        watched_join = 'LEFT JOIN watched_titles AS wt ON wt.display_tconst = r.display_tconst'
        watched_filter = 'AND wt.display_tconst IS NULL'
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT id, slug, name, description, list_kind, ai_input_role\n            FROM app.user_lists\n            WHERE id = %s\n            ', (list_id,))
        list_row = cursor.fetchone()
        if list_row is None:
            return (None, 0, [])
        count_sql = f'\n            WITH ranked_items AS (\n                SELECT\n                    i.display_tconst,\n                    row_number() OVER (\n                        PARTITION BY i.display_tconst\n                        ORDER BY i.rank NULLS LAST, i.added_at DESC NULLS LAST, COALESCE(i.title, i.parent_title, i.tconst)\n                    ) AS group_row\n                FROM app.active_user_list_display_items AS i\n                WHERE i.list_id = %s\n            )\n            {watched_cte}\n            SELECT COUNT(*)\n            FROM ranked_items AS r\n            JOIN app.catalog_title_cards AS c ON c.tconst = r.display_tconst\n            {watched_join}\n            WHERE r.group_row = 1\n              AND r.display_tconst IS NOT NULL\n              AND COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL\n              {watched_filter}\n        '
        rows_sql = f'\n            WITH ranked_items AS (\n                SELECT\n                    i.display_tconst,\n                    i.media_type,\n                    i.title,\n                    i.parent_title,\n                    i.rank,\n                    i.added_at,\n                    i.notes,\n                    i.list_name,\n                    i.list_kind,\n                    row_number() OVER (\n                        PARTITION BY i.display_tconst\n                        ORDER BY i.rank NULLS LAST, i.added_at DESC NULLS LAST, COALESCE(i.title, i.parent_title, i.tconst)\n                    ) AS group_row\n                FROM app.active_user_list_display_items AS i\n                WHERE i.list_id = %s\n            )\n            {watched_cte}\n            SELECT\n                r.display_tconst,\n                r.media_type,\n                r.title,\n                r.parent_title,\n                NULL AS season_number,\n                NULL AS episode_number,\n                r.rank,\n                r.added_at,\n                r.notes,\n                r.list_name,\n                r.list_kind,\n                c.title_type,\n                c.start_year,\n                c.poster_relative_path,\n                c.poster_local_path,\n                c.primary_title,\n                COUNT(*) OVER () AS filtered_total\n            FROM ranked_items AS r\n            JOIN app.catalog_title_cards AS c ON c.tconst = r.display_tconst\n            {watched_join}\n            WHERE r.group_row = 1\n              AND r.display_tconst IS NOT NULL\n              AND COALESCE(c.poster_relative_path, c.poster_local_path) IS NOT NULL\n              {watched_filter}\n            ORDER BY r.rank NULLS LAST, r.added_at DESC NULLS LAST, COALESCE(r.title, r.parent_title, r.display_tconst)\n            LIMIT %s OFFSET %s\n        '
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
    return ({'id': list_row[0], 'slug': list_row[1], 'name': list_row[2], 'description': list_row[3], 'list_kind': list_row[4], 'ai_input_role': list_row[5]}, total, rows)

def fetch_user_lists() -> list[dict[str, Any]]:
    """Read all user lists from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT id, slug, name, description, list_kind, ai_input_role\n            FROM app.user_lists\n            ORDER BY list_kind, name\n            ')
        rows = cursor.fetchall()
    return [{'id': row[0], 'slug': row[1], 'name': row[2], 'description': row[3], 'list_kind': row[4], 'ai_input_role': row[5]} for row in rows]

def create_user_list(*, list_id: str, slug: str, name: str, description: str | None, now: str) -> dict[str, Any]:
    """Insert one custom user list in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            INSERT INTO app.user_lists (\n                id, slug, name, description, list_kind, ai_input_role, source_origin, source_ref, created_at, updated_at\n            )\n            VALUES (%s, %s, %s, %s, 'custom', 'ignore', 'local_app', NULL, %s::timestamp, %s::timestamp)\n            RETURNING id, slug, name, description, list_kind, ai_input_role\n            ", (list_id, slug, name, description, now, now))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError('PostgreSQL user_lists insert nevrátil žádný řádek.')
        conn.commit()
    return {'id': row[0], 'slug': row[1], 'name': row[2], 'description': row[3], 'list_kind': row[4], 'ai_input_role': row[5]}

def update_user_list_description(list_id: str, description: str | None, ai_input_role: str | None, now: str) -> dict[str, Any] | None:
    """Update editable metadata for one PostgreSQL user list."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            UPDATE app.user_lists\n            SET\n                description = %s,\n                ai_input_role = COALESCE(%s, ai_input_role),\n                updated_at = %s::timestamp\n            WHERE id = %s\n            RETURNING id, slug, name, description, list_kind, ai_input_role\n            ', (description, ai_input_role, now, list_id))
        row = cursor.fetchone()
        conn.commit()
    if row is None:
        return None
    return {'id': row[0], 'slug': row[1], 'name': row[2], 'description': row[3], 'list_kind': row[4], 'ai_input_role': row[5]}

def delete_user_list(list_id: str) -> dict[str, Any] | None:
    """Delete one custom user list and its items from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT id, slug, name, description, list_kind, ai_input_role\n            FROM app.user_lists\n            WHERE id = %s\n            ', (list_id,))
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            return None
        if row[4] != 'custom':
            conn.rollback()
            return {'id': row[0], 'slug': row[1], 'name': row[2], 'description': row[3], 'list_kind': row[4], 'ai_input_role': row[5]}
        cursor.execute('DELETE FROM app.user_list_items WHERE list_id = %s', (list_id,))
        cursor.execute('DELETE FROM app.user_lists WHERE id = %s', (list_id,))
        conn.commit()
    return {'id': row[0], 'slug': row[1], 'name': row[2], 'description': row[3], 'list_kind': row[4], 'ai_input_role': row[5]}

def slug_exists(slug: str) -> bool:
    """Return whether one user-list slug already exists in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('SELECT 1 FROM app.user_lists WHERE slug = %s LIMIT 1', (slug,))
        return cursor.fetchone() is not None

def upsert_user_list_item(*, item_id: str, list_id: str, canonical_key: str, tconst: str | None, media_type: str, imdb_id: str | None, tmdb_id: int | None, trakt_id: int | None, parent_tconst: str | None, parent_title: str | None, title: str | None, season_number: int | None, episode_number: int | None, rank: int | None, added_at: str | None, notes: str | None, source_origin: str, source_ref: str | None, now: str) -> None:
    """Upsert one app.user_list_items row in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            INSERT INTO app.user_list_items (\n                id, list_id, canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst,\n                parent_title, title, season_number, episode_number, rank, added_at, notes, source_origin,\n                source_ref, is_archived, created_at, updated_at\n            )\n            VALUES (\n                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamp, %s, %s, %s, FALSE,\n                %s::timestamp, %s::timestamp\n            )\n            ON CONFLICT (list_id, canonical_key) DO UPDATE SET\n                tconst = COALESCE(app.user_list_items.tconst, excluded.tconst),\n                imdb_id = COALESCE(app.user_list_items.imdb_id, excluded.imdb_id),\n                tmdb_id = COALESCE(app.user_list_items.tmdb_id, excluded.tmdb_id),\n                trakt_id = COALESCE(app.user_list_items.trakt_id, excluded.trakt_id),\n                parent_tconst = COALESCE(app.user_list_items.parent_tconst, excluded.parent_tconst),\n                parent_title = COALESCE(app.user_list_items.parent_title, excluded.parent_title),\n                title = COALESCE(app.user_list_items.title, excluded.title),\n                rank = COALESCE(app.user_list_items.rank, excluded.rank),\n                added_at = CASE\n                    WHEN app.user_list_items.is_archived THEN COALESCE(excluded.added_at, app.user_list_items.added_at)\n                    ELSE COALESCE(app.user_list_items.added_at, excluded.added_at)\n                END,\n                notes = COALESCE(app.user_list_items.notes, excluded.notes),\n                is_archived = FALSE,\n                updated_at = excluded.updated_at\n            ', (item_id, list_id, canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id, parent_tconst, parent_title, title, season_number, episode_number, rank, added_at, notes, source_origin, source_ref, now, now))
        conn.commit()

def archive_user_list_item(list_id: str, canonical_key: str, now: str) -> None:
    """Archive one list item in PostgreSQL by list/canonical key."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            UPDATE app.user_list_items\n            SET is_archived = TRUE, updated_at = %s::timestamp\n            WHERE list_id = %s AND canonical_key = %s\n            ', (now, list_id, canonical_key))
        conn.commit()

def fetch_user_list_item_counts() -> dict[str, int]:
    """Return aggregate list and active-item counts from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('SELECT COUNT(*) FROM app.user_lists')
        lists = int(cursor.fetchone()[0])
        cursor.execute('SELECT COUNT(*) FROM app.user_list_items WHERE is_archived = FALSE')
        list_items = int(cursor.fetchone()[0])
        cursor.execute("\n            SELECT COUNT(*)\n            FROM app.user_list_items AS i\n            JOIN app.user_lists AS l ON l.id = i.list_id\n            WHERE i.is_archived = FALSE AND l.list_kind = 'watchlist'\n            ")
        watchlist_items = int(cursor.fetchone()[0])
    return {'lists': lists, 'list_items': list_items, 'watchlist_items': watchlist_items}

def fetch_active_user_list_items() -> list[dict[str, Any]]:
    """Return active user list items from PostgreSQL for read-model assembly."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                id, list_id, canonical_key, tconst, media_type, imdb_id, tmdb_id, trakt_id,\n                parent_tconst, parent_title, title, season_number, episode_number, rank,\n                added_at, notes, source_origin, source_ref, created_at, updated_at\n            FROM app.user_list_items\n            WHERE is_archived = FALSE\n            ')
        rows = cursor.fetchall()
    return [{'id': row[0], 'list_id': row[1], 'canonical_key': row[2], 'tconst': row[3], 'media_type': row[4], 'imdb_id': row[5], 'tmdb_id': row[6], 'trakt_id': row[7], 'parent_tconst': row[8], 'parent_title': row[9], 'title': row[10], 'season_number': row[11], 'episode_number': row[12], 'rank': row[13], 'added_at': _parse_optional_timestamp(row[14]), 'notes': row[15], 'source_origin': row[16], 'source_ref': row[17], 'created_at': _parse_optional_timestamp(row[18]), 'updated_at': _parse_optional_timestamp(row[19])} for row in rows]

def import_ai_recommendation_batch(*, run_id: str, source_path: str, source_filename: str, source_checksum: str, contract_version: int, intent: str, status: str, payload_created_at: str | None, imported_at: str, source_inputs_json: str, method_notes_json: str, deprioritized_candidates_json: str, notes: str | None, raw_json: str, recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    """Import one stable AI recommendation JSON into audit tables and AI suggestions list."""
    target_list_slug = 'ai-navrhy'
    target_list_id = 'ai-suggestions'
    imported_count = 0
    resolved_count = 0
    list_inserted = 0
    list_updated = 0
    unresolved: list[dict[str, Any]] = []
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            INSERT INTO app.user_lists (\n                id, slug, name, description, list_kind, ai_input_role,\n                source_origin, source_ref, created_at, updated_at\n            )\n            VALUES (\n                %s, %s, 'AI návrhy', 'Doporučení importovaná z externí AI vrstvy.',\n                'custom', 'external_suggestion', 'ai_import', 'ensure_ai_suggestions',\n                %s::timestamp, %s::timestamp\n            )\n            ON CONFLICT (slug) DO UPDATE SET\n                ai_input_role = 'external_suggestion',\n                updated_at = excluded.updated_at\n            RETURNING id\n            ", (target_list_id, target_list_slug, imported_at, imported_at))
        target_list_id = cursor.fetchone()[0]
        cursor.execute('\n            SELECT id\n            FROM app.ai_recommendation_runs\n            WHERE source_checksum = %s\n            ', (source_checksum,))
        existing_run = cursor.fetchone()
        if existing_run is not None:
            conn.rollback()
            return {'run_id': existing_run[0], 'source_filename': source_filename, 'target_list_id': target_list_id, 'recommendations': 0, 'resolved': 0, 'unresolved': 0, 'list_inserted': 0, 'list_updated': 0, 'unresolved_items': [], 'already_imported': True}
        cursor.execute('\n            INSERT INTO app.ai_recommendation_runs (\n                id, source_path, source_filename, source_checksum, contract_version,\n                intent, status, payload_created_at, imported_at, source_inputs_json,\n                method_notes_json, deprioritized_candidates_json, notes, raw_json\n            )\n            VALUES (\n                %s, %s, %s, %s, %s, %s, %s, %s::timestamp, %s::timestamp,\n                %s, %s, %s, %s, %s\n            )\n            ', (run_id, source_path, source_filename, source_checksum, contract_version, intent, status, payload_created_at, imported_at, source_inputs_json, method_notes_json, deprioritized_candidates_json, notes, raw_json))
        for row_number, recommendation in enumerate(recommendations, start=1):
            candidate_id = f'ai-rec-candidate-{uuid.uuid4()}'
            imdb_id = recommendation.get('imdb_id')
            tmdb_id = recommendation.get('tmdb_id')
            title = str(recommendation['title'])
            year = recommendation.get('year')
            media_type = recommendation.get('media_type')
            resolved_tconst = None
            resolution_status = 'unresolved_missing_imdb'
            title_type = None
            catalog_title = None
            catalog_tmdb_id = None
            ai_list_item_id = None
            if isinstance(imdb_id, str) and imdb_id.startswith('tt'):
                cursor.execute('\n                    SELECT t.tconst, t.title_type, t.primary_title, map.tmdb_id\n                    FROM app.catalog_titles AS t\n                    LEFT JOIN app.tmdb_title_map AS map ON map.tconst = t.tconst\n                    WHERE t.tconst = %s\n                    ', (imdb_id,))
                catalog_row = cursor.fetchone()
                if catalog_row is not None:
                    resolved_tconst = catalog_row[0]
                    title_type = catalog_row[1]
                    catalog_title = catalog_row[2]
                    catalog_tmdb_id = catalog_row[3]
                    resolution_status = 'resolved'
                else:
                    resolution_status = 'unresolved_catalog_miss'
            if resolved_tconst is not None:
                resolved_count += 1
                canonical_key = f'title:tconst:{resolved_tconst}'
                list_title = catalog_title or title
                list_tmdb_id = tmdb_id if tmdb_id is not None else catalog_tmdb_id
                list_notes = _ai_recommendation_list_notes(recommendation)
                cursor.execute('\n                    SELECT id\n                    FROM app.user_list_items\n                    WHERE list_id = %s AND canonical_key = %s\n                    ', (target_list_id, canonical_key))
                existing_item = cursor.fetchone()
                was_existing = existing_item is not None
                item_id = existing_item[0] if existing_item is not None else f'ai-suggestion-{uuid.uuid4()}'
                cursor.execute("\n                    INSERT INTO app.user_list_items (\n                        id, list_id, canonical_key, tconst, media_type, imdb_id, tmdb_id,\n                        trakt_id, parent_tconst, parent_title, title, season_number,\n                        episode_number, rank, added_at, notes, source_origin, source_ref,\n                        is_archived, created_at, updated_at\n                    )\n                    VALUES (\n                        %s, %s, %s, %s, 'title', %s, %s, NULL, NULL, NULL, %s,\n                        NULL, NULL, %s, %s::timestamp, %s, 'ai_import', %s,\n                        FALSE, %s::timestamp, %s::timestamp\n                    )\n                    ON CONFLICT (list_id, canonical_key) DO UPDATE SET\n                        tconst = excluded.tconst,\n                        imdb_id = excluded.imdb_id,\n                        tmdb_id = COALESCE(excluded.tmdb_id, app.user_list_items.tmdb_id),\n                        title = COALESCE(excluded.title, app.user_list_items.title),\n                        rank = excluded.rank,\n                        notes = excluded.notes,\n                        source_origin = excluded.source_origin,\n                        source_ref = excluded.source_ref,\n                        is_archived = FALSE,\n                        updated_at = excluded.updated_at\n                    RETURNING id\n                    ", (item_id, target_list_id, canonical_key, resolved_tconst, imdb_id, list_tmdb_id, list_title, recommendation.get('priority'), imported_at, list_notes, f'ai_recommendation:{run_id}:{row_number}', imported_at, imported_at))
                ai_list_item_id = cursor.fetchone()[0]
                if was_existing:
                    list_updated += 1
                else:
                    list_inserted += 1
            else:
                unresolved.append({'row_number': row_number, 'title': title, 'year': year, 'imdb_id': imdb_id, 'resolution_status': resolution_status})
            cursor.execute('\n                INSERT INTO app.ai_recommendation_candidates (\n                    id, run_id, row_number, title, year, imdb_id, tmdb_id, media_type,\n                    confidence, recommendation_status, priority, fit_reasons_json,\n                    risk_reasons_json, source_signal_refs_json, notes, raw_json,\n                    resolved_tconst, resolution_status, ai_list_item_id, created_at, updated_at\n                )\n                VALUES (\n                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,\n                    %s, %s, %s, %s, %s, %s::timestamp, %s::timestamp\n                )\n                ', (candidate_id, run_id, row_number, title, year, imdb_id, tmdb_id, media_type, recommendation.get('confidence'), recommendation.get('status'), recommendation.get('priority'), json.dumps(recommendation.get('fit_reasons') or [], ensure_ascii=False), json.dumps(recommendation.get('risk_reasons') or [], ensure_ascii=False), json.dumps(recommendation.get('source_signal_refs') or [], ensure_ascii=False), recommendation.get('notes'), json.dumps(recommendation, ensure_ascii=False, sort_keys=True), resolved_tconst, resolution_status, ai_list_item_id, imported_at, imported_at))
            imported_count += 1
        conn.commit()
    return {'run_id': run_id, 'source_filename': source_filename, 'target_list_id': target_list_id, 'recommendations': imported_count, 'resolved': resolved_count, 'unresolved': len(unresolved), 'list_inserted': list_inserted, 'list_updated': list_updated, 'unresolved_items': unresolved}

def fetch_ai_recommendation_run_checksums() -> dict[str, dict[str, Any]]:
    """Return imported AI recommendation runs keyed by source checksum."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT source_checksum, id, source_filename, imported_at\n            FROM app.ai_recommendation_runs\n            ORDER BY imported_at DESC\n            ')
        rows = cursor.fetchall()
    return {row[0]: {'run_id': row[1], 'source_filename': row[2], 'imported_at': _parse_optional_timestamp(row[3])} for row in rows}

def fetch_latest_ai_recommendation_for_title(tconst: str) -> dict[str, Any] | None:
    """Return the latest imported AI recommendation explanation for one title."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                c.id,\n                c.run_id,\n                r.source_filename,\n                r.intent,\n                r.imported_at,\n                c.title,\n                c.year,\n                c.imdb_id,\n                c.tmdb_id,\n                c.media_type,\n                c.confidence,\n                c.recommendation_status,\n                c.priority,\n                c.fit_reasons_json,\n                c.risk_reasons_json,\n                c.source_signal_refs_json,\n                c.notes\n            FROM app.ai_recommendation_candidates AS c\n            JOIN app.ai_recommendation_runs AS r ON r.id = c.run_id\n            WHERE c.resolved_tconst = %s\n            ORDER BY r.imported_at DESC, c.priority NULLS LAST, c.row_number\n            LIMIT 1\n            ', (tconst,))
        row = cursor.fetchone()
    if row is None:
        return None
    return {'candidate_id': row[0], 'run_id': row[1], 'source_filename': row[2], 'intent': row[3], 'imported_at': _parse_optional_timestamp(row[4]), 'title': row[5], 'year': row[6], 'imdb_id': row[7], 'tmdb_id': row[8], 'media_type': row[9], 'confidence': row[10], 'status': row[11], 'priority': row[12], 'fit_reasons': json.loads(row[13] or '[]'), 'risk_reasons': json.loads(row[14] or '[]'), 'source_signal_refs': json.loads(row[15] or '[]'), 'notes': row[16]}

def _ai_recommendation_list_notes(recommendation: dict[str, Any]) -> str:
    lines = [f"AI import: confidence={recommendation.get('confidence') or 'unknown'}, status={recommendation.get('status') or 'unknown'}"]
    fit_reasons = [str(item) for item in recommendation.get('fit_reasons') or [] if str(item).strip()]
    risk_reasons = [str(item) for item in recommendation.get('risk_reasons') or [] if str(item).strip()]
    if fit_reasons:
        lines.append('Klady:')
        lines.extend((f'- {item}' for item in fit_reasons[:4]))
    if risk_reasons:
        lines.append('Rizika:')
        lines.extend((f'- {item}' for item in risk_reasons[:4]))
    if recommendation.get('notes'):
        lines.append(f"Poznamka: {recommendation['notes']}")
    return '\n'.join(lines)

def fetch_person_affinity_rating(nconst: str) -> int:
    """Return current affinity rating for one person from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT affinity_rating\n            FROM app.user_people\n            WHERE nconst = %s\n            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST\n            LIMIT 1\n            ', (nconst,))
        row = cursor.fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])

def fetch_positive_person_affinities() -> dict[str, int]:
    """Return positive person affinity ratings from PostgreSQL keyed by nconst."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT nconst, affinity_rating\n            FROM app.user_people\n            WHERE nconst IS NOT NULL AND affinity_rating > 0\n            ')
        rows = cursor.fetchall()
    return {str(row[0]): int(row[1]) for row in rows if row[0] is not None and row[1] is not None}

def upsert_person_affinity(*, person_key: str, nconst: str, name: str, known_for: str | None, birth_date: str | None, source_ref: str | None, is_favorite: bool, affinity_rating: int, created_at: str, updated_at: str) -> None:
    """Upsert one user_people row in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            INSERT INTO app.user_people (\n                person_key, nconst, name, known_for, birth_date, source_origin, source_ref,\n                is_favorite, affinity_rating, created_at, updated_at\n            )\n            VALUES (%s, %s, %s, %s, %s, 'local_app', %s, %s, %s, %s::timestamp, %s::timestamp)\n            ON CONFLICT (person_key) DO UPDATE SET\n                nconst = excluded.nconst,\n                name = excluded.name,\n                known_for = COALESCE(app.user_people.known_for, excluded.known_for),\n                birth_date = COALESCE(app.user_people.birth_date, excluded.birth_date),\n                is_favorite = excluded.is_favorite,\n                affinity_rating = excluded.affinity_rating,\n                updated_at = excluded.updated_at\n            ", (person_key, nconst, name, known_for, birth_date, source_ref, is_favorite, affinity_rating, created_at, updated_at))
        conn.commit()

def upsert_title_role_signal(*, signal_key: str, tconst: str, nconst: str | None, character_name: str | None, signal_type: str, polarity: str, strength: int, notes: str | None, source_ref: str | None, now: str) -> dict[str, Any]:
    """Upsert one title-specific role/character signal in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            INSERT INTO app.user_title_role_signals (\n                signal_key, tconst, nconst, character_name, signal_type, polarity,\n                strength, notes, source_origin, source_ref, created_at, updated_at\n            )\n            VALUES (\n                %s, %s, %s, %s, %s, %s,\n                %s, %s, 'local_app', %s, %s::timestamp, %s::timestamp\n            )\n            ON CONFLICT (signal_key) DO UPDATE SET\n                tconst = excluded.tconst,\n                nconst = excluded.nconst,\n                character_name = excluded.character_name,\n                signal_type = excluded.signal_type,\n                polarity = excluded.polarity,\n                strength = excluded.strength,\n                notes = excluded.notes,\n                source_ref = excluded.source_ref,\n                updated_at = excluded.updated_at\n            RETURNING\n                signal_key, tconst, nconst, character_name, signal_type, polarity,\n                strength, notes, source_origin, source_ref, created_at, updated_at\n            ", (signal_key, tconst, nconst, character_name, signal_type, polarity, strength, notes, source_ref, now, now))
        row = cursor.fetchone()
        conn.commit()
    if row is None:
        raise RuntimeError('PostgreSQL role signal upsert nevrátil žádný řádek.')
    return _row_to_title_role_signal(row)

def fetch_title_role_signals(tconst: str) -> list[dict[str, Any]]:
    """Read role/character signals for one title from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                signal_key, tconst, nconst, character_name, signal_type, polarity,\n                strength, notes, source_origin, source_ref, created_at, updated_at\n            FROM app.user_title_role_signals\n            WHERE tconst = %s\n            ORDER BY strength DESC, updated_at DESC, character_name NULLS LAST, signal_type\n            ', (tconst,))
        rows = cursor.fetchall()
    return [_row_to_title_role_signal(row) for row in rows]

def delete_title_role_signals(*, tconst: str, nconst: str | None, character_name: str | None, signal_types: list[str] | None=None) -> int:
    """Delete title-specific role/character signals for one role identity."""
    type_filter = ''
    params: list[Any] = [tconst]
    if nconst:
        identity_filter = 'nconst = %s'
        params.append(nconst)
    else:
        identity_filter = 'nconst IS NULL AND character_name = %s'
        params.append(character_name)
    if signal_types is not None:
        type_filter = 'AND signal_type = ANY(%s)'
        params.append(signal_types)
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(f'\n            DELETE FROM app.user_title_role_signals\n            WHERE tconst = %s\n              AND {identity_filter}\n              {type_filter}\n            ', params)
        deleted = cursor.rowcount
        conn.commit()
    return int(deleted or 0)

def fetch_favorite_genres(*, active_only: bool) -> list[dict[str, Any]]:
    """Read favorite genres from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                genre, weight, preference_rank, source_origin, source_ref, notes, is_active, created_at, updated_at\n            FROM app.favorite_genres\n            WHERE (%s = FALSE OR is_active = TRUE)\n            ORDER BY preference_rank ASC NULLS LAST, weight DESC, genre ASC\n            ', (active_only,))
        rows = cursor.fetchall()
    return [{'genre': row[0], 'weight': row[1], 'preference_rank': row[2], 'source_origin': row[3], 'source_ref': row[4], 'notes': row[5], 'is_active': row[6], 'created_at': _parse_optional_timestamp(row[7]), 'updated_at': _parse_optional_timestamp(row[8])} for row in rows]

def replace_favorite_genres(*, items: list[dict[str, Any]], source_origin: str, source_ref: str | None, archive_missing: bool, now: str) -> None:
    """Replace favorite genres in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT touched_count, archived_count\n            FROM app.replace_favorite_genres(\n                %s::jsonb,\n                %s,\n                %s,\n                %s,\n                %s::timestamp\n            )\n            ', (json.dumps(items), source_origin, source_ref, archive_missing, now))
        conn.commit()

def fetch_favorite_traits(*, active_only: bool) -> list[dict[str, Any]]:
    """Read favorite traits from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                trait, weight, preference_rank, source_origin, source_ref, notes, is_active, created_at, updated_at\n            FROM app.favorite_traits\n            WHERE (%s = FALSE OR is_active = TRUE)\n            ORDER BY preference_rank ASC NULLS LAST, weight DESC, trait ASC\n            ', (active_only,))
        rows = cursor.fetchall()
    return [{'trait': row[0], 'weight': row[1], 'preference_rank': row[2], 'source_origin': row[3], 'source_ref': row[4], 'notes': row[5], 'is_active': row[6], 'created_at': _parse_optional_timestamp(row[7]), 'updated_at': _parse_optional_timestamp(row[8])} for row in rows]

def replace_favorite_traits(*, items: list[dict[str, Any]], source_origin: str, source_ref: str | None, archive_missing: bool, now: str) -> None:
    """Replace favorite traits in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT touched_count, archived_count\n            FROM app.replace_favorite_traits(\n                %s::jsonb,\n                %s,\n                %s,\n                %s,\n                %s::timestamp\n            )\n            ', (json.dumps(items), source_origin, source_ref, archive_missing, now))
        conn.commit()

def insert_genre_score_snapshot(*, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Insert one already-prepared genre score snapshot into PostgreSQL."""
    if not rows:
        raise ValueError('Je potreba dodat alespon jeden zanr se score.')
    with _connect() as conn, conn.cursor() as cursor:
        for item in rows:
            cursor.execute('\n                INSERT INTO app.genre_scores (\n                    id, genre, generated_at, algorithm_version, score_scope, source_origin, source_ref,\n                    titles_considered, watched_titles_considered, rated_titles_considered,\n                    contributing_titles_json, excluded_titles_json,\n                    favorite_genre_weight, preference_overlap_score, preference_alignment_score, affinity_score,\n                    rating_signal_score, watch_signal_score, recency_score, actor_affinity_score, frequency_score, consistency_score,\n                    novelty_score, confidence_score, manual_adjustment_score, final_score, normalized_score,\n                    rank_in_run, metrics_json, explanation, created_at\n                )\n                VALUES (\n                    %s, %s, %s::timestamp, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,\n                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamp\n                )\n                ', (item['id'], item['genre'], item['generated_at'], item.get('algorithm_version'), item.get('score_scope'), item['source_origin'], item.get('source_ref'), item.get('titles_considered'), item.get('watched_titles_considered'), item.get('rated_titles_considered'), item.get('contributing_titles_json'), item.get('excluded_titles_json'), item.get('favorite_genre_weight'), item.get('preference_overlap_score'), item.get('preference_alignment_score'), item.get('affinity_score'), item.get('rating_signal_score'), item.get('watch_signal_score'), item.get('recency_score'), item.get('actor_affinity_score'), item.get('frequency_score'), item.get('consistency_score'), item.get('novelty_score'), item.get('confidence_score'), item.get('manual_adjustment_score'), item['final_score'], item.get('normalized_score'), item.get('rank_in_run'), item.get('metrics_json'), item.get('explanation'), item['created_at']))
        conn.commit()
    first = rows[0]
    return {'generated_at': first['generated_at'], 'score_scope': first.get('score_scope'), 'algorithm_version': first.get('algorithm_version'), 'count': len(rows)}

def fetch_latest_genre_scores(*, score_scope: str | None, limit: int | None) -> dict[str, Any] | None:
    """Read latest genre score snapshot from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        if score_scope is None:
            cursor.execute('\n                SELECT generated_at, score_scope\n                FROM app.genre_scores\n                ORDER BY generated_at DESC, score_scope ASC\n                LIMIT 1\n                ')
        else:
            cursor.execute('\n                SELECT generated_at, score_scope\n                FROM app.genre_scores\n                WHERE score_scope = %s\n                ORDER BY generated_at DESC, score_scope ASC\n                LIMIT 1\n                ', (score_scope,))
        latest_row = cursor.fetchone()
        if latest_row is None:
            return None
        generated_at, resolved_scope = (latest_row[0], latest_row[1])
        sql = '\n            SELECT\n                id, genre, generated_at, algorithm_version, score_scope, source_origin, source_ref,\n                titles_considered, watched_titles_considered, rated_titles_considered,\n                contributing_titles_json, excluded_titles_json,\n                favorite_genre_weight, preference_overlap_score, preference_alignment_score, affinity_score,\n                rating_signal_score, watch_signal_score, recency_score, actor_affinity_score, frequency_score, consistency_score,\n                novelty_score, confidence_score, manual_adjustment_score, final_score, normalized_score,\n                rank_in_run, metrics_json, explanation, created_at\n            FROM app.genre_scores\n            WHERE generated_at = %s::timestamp AND score_scope IS NOT DISTINCT FROM %s\n            ORDER BY rank_in_run ASC NULLS LAST, final_score DESC, genre ASC\n        '
        params: list[Any] = [generated_at, resolved_scope]
        if limit is not None:
            sql += '\nLIMIT %s'
            params.append(limit)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    items = [{'id': row[0], 'genre': row[1], 'generated_at': _parse_optional_timestamp(row[2]), 'algorithm_version': row[3], 'score_scope': row[4], 'source_origin': row[5], 'source_ref': row[6], 'titles_considered': row[7], 'watched_titles_considered': row[8], 'rated_titles_considered': row[9], 'contributing_titles': _loads_json_or_none(row[10]), 'excluded_titles': _loads_json_or_none(row[11]), 'favorite_genre_weight': row[12], 'preference_overlap_score': row[13], 'preference_alignment_score': row[14], 'affinity_score': row[15], 'rating_signal_score': row[16], 'watch_signal_score': row[17], 'recency_score': row[18], 'actor_affinity_score': row[19], 'frequency_score': row[20], 'consistency_score': row[21], 'novelty_score': row[22], 'confidence_score': row[23], 'manual_adjustment_score': row[24], 'final_score': row[25], 'normalized_score': row[26], 'rank_in_run': row[27], 'metrics': _loads_json_or_none(row[28]), 'explanation': row[29], 'created_at': _parse_optional_timestamp(row[30])} for row in rows]
    return {'generated_at': _parse_optional_timestamp(generated_at), 'score_scope': resolved_scope, 'count': len(items), 'items': items}

def fetch_catalog_genres() -> list[dict[str, Any]]:
    """Read all distinct catalog genres with title counts from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            WITH exploded AS (\n                SELECT trim(unnest(string_to_array(genres, ','))) AS genre\n                FROM app.catalog_titles\n                WHERE genres IS NOT NULL AND genres <> ''\n            )\n            SELECT genre, COUNT(*) AS title_count\n            FROM exploded\n            WHERE genre IS NOT NULL AND genre <> ''\n            GROUP BY genre\n            ORDER BY genre ASC\n            ")
        rows = cursor.fetchall()
    return [{'genre': row[0], 'title_count': int(row[1])} for row in rows]

def fetch_genre_score_source_rows() -> list[dict[str, Any]]:
    """Read title-level behavioral inputs for genre scoring from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            WITH latest_title_ratings AS (\n                SELECT DISTINCT ON (tconst)\n                    tconst,\n                    rating\n                FROM app.user_ratings\n                WHERE tconst IS NOT NULL\n                ORDER BY tconst, COALESCE(rated_at, updated_at, created_at) DESC, canonical_key\n            ),\n            title_watch_events AS (\n                SELECT\n                    w.tconst,\n                    COALESCE(w.created_at, CAST(w.watched_on AS TIMESTAMP)) AS watched_at\n                FROM app.watch_events AS w\n                WHERE w.tconst IN (SELECT tconst FROM app.catalog_titles)\n\n                UNION ALL\n\n                SELECT\n                    e.series_tconst AS tconst,\n                    COALESCE(w.created_at, CAST(w.watched_on AS TIMESTAMP)) AS watched_at\n                FROM app.watch_events AS w\n                JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst\n                WHERE e.series_tconst IS NOT NULL\n            ),\n            title_watch_stats AS (\n                SELECT\n                    tconst,\n                    COUNT(*) AS watch_count,\n                    MAX(watched_at) AS last_watched_at\n                FROM title_watch_events\n                GROUP BY tconst\n            ),\n            rated_cast_affinity AS (\n                SELECT\n                    c.tconst,\n                    SUM(CAST(p.affinity_rating AS DOUBLE PRECISION) * CASE\n                        WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0\n                        ELSE 1.0 / sqrt(CAST(c.ordering AS DOUBLE PRECISION))\n                    END)\n                    / NULLIF(\n                        SUM(CASE\n                            WHEN c.ordering IS NULL OR c.ordering <= 0 THEN 1.0\n                            ELSE 1.0 / sqrt(CAST(c.ordering AS DOUBLE PRECISION))\n                        END),\n                        0.0\n                    ) AS actor_affinity_rating\n                FROM app.title_credits AS c\n                JOIN app.user_people AS p ON p.nconst = c.nconst\n                WHERE c.credit_group = 'cast'\n                  AND p.affinity_rating > 0\n                  AND (c.ordering IS NULL OR c.ordering <= 8)\n                GROUP BY c.tconst\n            )\n            SELECT\n                t.tconst,\n                t.primary_title,\n                t.start_year,\n                t.genres,\n                r.rating,\n                w.watch_count,\n                w.last_watched_at,\n                a.actor_affinity_rating\n            FROM app.catalog_titles AS t\n            LEFT JOIN latest_title_ratings AS r ON r.tconst = t.tconst\n            LEFT JOIN title_watch_stats AS w ON w.tconst = t.tconst\n            LEFT JOIN rated_cast_affinity AS a ON a.tconst = t.tconst\n            WHERE t.genres IS NOT NULL\n              AND t.genres <> ''\n              AND (r.rating IS NOT NULL OR w.watch_count IS NOT NULL OR a.actor_affinity_rating IS NOT NULL)\n            ORDER BY t.primary_title ASC\n            ")
        rows = cursor.fetchall()
    return [{'tconst': row[0], 'title': row[1], 'year': row[2], 'genres': [part.strip() for part in str(row[3] or '').split(',') if part.strip()], 'rating': row[4], 'watch_count': int(row[5] or 0), 'last_watched_at': _parse_optional_timestamp(row[6]), 'actor_affinity_rating': row[7]} for row in rows]

def fetch_relevant_people_candidate_rows(*, main_cast_limit: int, limit: int | None) -> list[dict[str, Any]]:
    """Read relevant people candidates for cache refreshes from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            WITH list_titles AS (\n                SELECT DISTINCT\n                    COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AS tconst\n                FROM app.user_list_items AS i\n                LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst\n                WHERE i.is_archived = FALSE\n                  AND COALESCE(e.series_tconst, i.tconst, i.parent_tconst) IS NOT NULL\n            ),\n            list_credit_candidates AS (\n                SELECT\n                    c.nconst,\n                    p.primary_name,\n                    p.birth_year,\n                    p.primary_profession,\n                    COUNT(DISTINCT c.tconst) AS credit_count,\n                    MIN(\n                        CASE c.credit_group\n                            WHEN 'director' THEN 1\n                            WHEN 'cast' THEN 2\n                            ELSE 5\n                        END\n                    ) AS group_priority\n                FROM app.title_credits AS c\n                JOIN list_titles AS lt ON lt.tconst = c.tconst\n                JOIN app.catalog_people AS p USING (nconst)\n                WHERE c.credit_group = 'director'\n                   OR (c.credit_group = 'cast' AND c.ordering <= %s)\n                GROUP BY 1, 2, 3, 4\n            ),\n            affinity_candidates AS (\n                SELECT\n                    up.nconst,\n                    COALESCE(cp.primary_name, up.name) AS primary_name,\n                    cp.birth_year,\n                    cp.primary_profession,\n                    0 AS credit_count,\n                    0 AS group_priority\n                FROM app.user_people AS up\n                LEFT JOIN app.catalog_people AS cp ON cp.nconst = up.nconst\n                WHERE up.nconst IS NOT NULL\n                  AND (up.affinity_rating > 0 OR up.is_favorite = TRUE)\n            ),\n            combined AS (\n                SELECT * FROM list_credit_candidates\n                UNION ALL\n                SELECT * FROM affinity_candidates\n            )\n            SELECT\n                nconst,\n                primary_name,\n                birth_year,\n                primary_profession,\n                MAX(credit_count) AS credit_count,\n                MIN(group_priority) AS group_priority\n            FROM combined\n            WHERE nconst IS NOT NULL\n            GROUP BY 1, 2, 3, 4\n            ORDER BY group_priority, credit_count DESC, birth_year DESC NULLS LAST, primary_name\n            " + ('\nLIMIT %s' if limit is not None else ''), (main_cast_limit, limit) if limit is not None else (main_cast_limit,))
        rows = cursor.fetchall()
    return [{'nconst': str(row[0]), 'name': row[1], 'birth_year': row[2], 'primary_profession': row[3], 'credit_count': row[4], 'group_priority': row[5]} for row in rows]

def record_search_recall_entry(*, entry_id: str, entity_type: str, query_text: str, query_text_fold: str, query_key: str, target_id: str, target_label: str | None, target_title_type: str | None, matched_alias_title: str | None, fuzzy_score: float | None, now: str, recall_limit: int) -> None:
    """Upsert one search recall entry in PostgreSQL and prune overflow."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            INSERT INTO app.search_recall (\n                id, entity_type, query_text, query_text_fold, query_key, target_id, target_label,\n                target_title_type, matched_alias_title, fuzzy_score, first_searched_at, last_searched_at, hit_count\n            )\n            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamp, %s::timestamp, 1)\n            ON CONFLICT (id) DO UPDATE SET\n                query_text = excluded.query_text,\n                query_key = excluded.query_key,\n                target_label = excluded.target_label,\n                target_title_type = excluded.target_title_type,\n                matched_alias_title = excluded.matched_alias_title,\n                fuzzy_score = excluded.fuzzy_score,\n                last_searched_at = excluded.last_searched_at,\n                hit_count = app.search_recall.hit_count + 1\n            ', (entry_id, entity_type, query_text, query_text_fold, query_key, target_id, target_label, target_title_type, matched_alias_title, fuzzy_score, now, now))
        cursor.execute('\n            DELETE FROM app.search_recall\n            WHERE id IN (\n                SELECT id\n                FROM app.search_recall\n                ORDER BY last_searched_at DESC, hit_count DESC, first_searched_at DESC, id DESC\n                OFFSET %s\n            )\n            ', (max(recall_limit, 0),))
        conn.commit()

def fetch_search_recall_match(*, entity_type: str, query_key: str, query_text_fold: str) -> tuple[str, float | None] | None:
    """Return best search recall match for one entity/query pair."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT target_id, fuzzy_score\n            FROM app.search_recall\n            WHERE entity_type = %s AND query_key = %s\n            ORDER BY\n                CASE WHEN query_text_fold = %s THEN 0 ELSE 1 END,\n                last_searched_at DESC,\n                hit_count DESC,\n                first_searched_at DESC\n            LIMIT 1\n            ', (entity_type, query_key, query_text_fold))
        row = cursor.fetchone()
    if row is None:
        return None
    return (str(row[0]), row[1])

def create_import_batch_record(*, batch_id: str, source: str, filename: str, checksum: str, status: str, created_at: str) -> None:
    """Insert one import batch row into PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            INSERT INTO app.import_batches (id, source, filename, checksum, status, created_at)\n            VALUES (%s, %s, %s, %s, %s, %s::timestamp)\n            ', (batch_id, source, filename, checksum, status, created_at))
        conn.commit()

def insert_import_rows(rows: list[dict[str, Any]]) -> None:
    """Insert previewed import rows into PostgreSQL."""
    if not rows:
        return
    with _connect() as conn, conn.cursor() as cursor:
        cursor.executemany('\n            INSERT INTO app.import_rows (\n                id,\n                batch_id,\n                source,\n                row_number,\n                raw_json,\n                parsed_title,\n                parsed_year,\n                parsed_watched_on,\n                parsed_season_number,\n                parsed_episode_number,\n                parsed_imdb_id,\n                parsed_tmdb_id,\n                resolution_status,\n                resolved_tconst,\n                resolution_confidence,\n                resolution_note\n            )\n            VALUES (\n                %s, %s, %s, %s, %s, %s, %s, %s::date, %s, %s, %s, %s, %s, %s, %s, %s\n            )\n            ', [(row['id'], row['batch_id'], row['source'], row['row_number'], row['raw_json'], row.get('parsed_title'), row.get('parsed_year'), row.get('parsed_watched_on'), row.get('parsed_season_number'), row.get('parsed_episode_number'), row.get('parsed_imdb_id'), row.get('parsed_tmdb_id'), row['resolution_status'], row.get('resolved_tconst'), row.get('resolution_confidence'), row.get('resolution_note')) for row in rows])
        conn.commit()

def fetch_import_batch_record(batch_id: str) -> dict[str, Any] | None:
    """Read one import batch row from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT id, source, filename, checksum, status, created_at\n            FROM app.import_batches\n            WHERE id = %s\n            ', (batch_id,))
        row = cursor.fetchone()
    if row is None:
        return None
    return {'id': row[0], 'source': row[1], 'filename': row[2], 'checksum': row[3], 'status': row[4], 'created_at': _parse_optional_timestamp(row[5])}

def fetch_import_batch_rows(batch_id: str, *, limit: int=100) -> list[dict[str, Any]]:
    """Read preview rows for one import batch from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT\n                row_number,\n                parsed_title,\n                parsed_year,\n                parsed_watched_on,\n                parsed_season_number,\n                parsed_episode_number,\n                parsed_imdb_id,\n                parsed_tmdb_id,\n                resolution_status,\n                resolved_tconst,\n                resolution_confidence,\n                resolution_note\n            FROM app.import_rows\n            WHERE batch_id = %s\n            ORDER BY row_number\n            LIMIT %s\n            ', (batch_id, limit))
        rows = cursor.fetchall()
    return [{'row_number': row[0], 'parsed_title': row[1], 'parsed_year': row[2], 'parsed_watched_on': _parse_optional_date(row[3]), 'parsed_season_number': row[4], 'parsed_episode_number': row[5], 'parsed_imdb_id': row[6], 'parsed_tmdb_id': row[7], 'resolution_status': row[8], 'resolved_tconst': row[9], 'resolution_confidence': row[10], 'resolution_note': row[11]} for row in rows]

def fetch_resolved_import_rows(batch_id: str) -> list[dict[str, Any]]:
    """Read resolved import rows for commit from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            SELECT id, source, parsed_watched_on, resolved_tconst, parsed_season_number, parsed_episode_number\n            FROM app.import_rows\n            WHERE batch_id = %s AND resolution_status = 'resolved' AND resolved_tconst IS NOT NULL\n            ORDER BY row_number\n            ", (batch_id,))
        rows = cursor.fetchall()
    return [{'id': row[0], 'source': row[1], 'parsed_watched_on': _parse_optional_date(row[2]), 'resolved_tconst': row[3], 'parsed_season_number': row[4], 'parsed_episode_number': row[5]} for row in rows]

def commit_import_batch(*, batch_id: str, committed_at: str) -> dict[str, Any]:
    """Commit one resolved import batch through the server-side PostgreSQL function."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT inserted_events, skipped_events, batch_status\n            FROM app.commit_import_batch(%s, %s::timestamp)\n            ', (batch_id, committed_at))
        row = cursor.fetchone()
        conn.commit()
    if row is None:
        raise RuntimeError(f'PostgreSQL commit_import_batch({batch_id}) nevratil vysledek.')
    return {'inserted_events': int(row[0] or 0), 'skipped_events': int(row[1] or 0), 'batch_status': str(row[2] or 'committed')}

def fetch_existing_import_commits(batch_id: str, import_row_ids: list[str]) -> set[str]:
    """Return which import row ids already produced watch events in PostgreSQL."""
    clean_ids = [str(item).strip() for item in import_row_ids if str(item).strip()]
    if not clean_ids:
        return set()
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT import_row_id\n            FROM app.watch_events\n            WHERE batch_id = %s AND import_row_id = ANY(%s)\n            ', (batch_id, clean_ids))
        rows = cursor.fetchall()
    return {str(row[0]) for row in rows if row[0] is not None}

def upsert_tmdb_mapping_record(*, tconst: str, tmdb_media_type: str, tmdb_id: int, matched_by: str, sync_status: str, matched_at: str, last_error: str | None) -> None:
    """Upsert one TMDB mapping row in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            INSERT INTO app.tmdb_title_map (\n                tconst, tmdb_media_type, tmdb_id, matched_by, matched_at, sync_status, last_error\n            )\n            VALUES (%s, %s, %s, %s, %s::timestamp, %s, %s)\n            ON CONFLICT (tconst) DO UPDATE SET\n                tmdb_media_type = excluded.tmdb_media_type,\n                tmdb_id = excluded.tmdb_id,\n                matched_by = excluded.matched_by,\n                matched_at = excluded.matched_at,\n                sync_status = excluded.sync_status,\n                last_error = excluded.last_error\n            ', (tconst, tmdb_media_type, tmdb_id, matched_by, matched_at, sync_status, last_error))
        conn.commit()

def fetch_tmdb_mapping_record(tconst: str) -> dict[str, Any] | None:
    """Read one TMDB mapping row from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT tconst, tmdb_media_type, tmdb_id, matched_by, matched_at, sync_status, last_error\n            FROM app.tmdb_title_map\n            WHERE tconst = %s\n            ', (tconst,))
        row = cursor.fetchone()
    if row is None:
        return None
    return {'tconst': row[0], 'tmdb_media_type': row[1], 'tmdb_id': row[2], 'matched_by': row[3], 'matched_at': _parse_optional_timestamp(row[4]), 'sync_status': row[5], 'last_error': row[6]}

def store_tmdb_payload_bundle(*, tconst: str, locale: str, display_title: str | None, original_title: str | None, overview: str | None, poster_path: str | None, backdrop_path: str | None, release_date: str | None, genres_json: str, raw_json: str, synced_at: str, providers: list[dict[str, Any]]) -> None:
    """Upsert TMDB detail and CZ provider rows in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            INSERT INTO app.tmdb_title_details (\n                tconst, locale, display_title, original_title, overview, poster_path, backdrop_path,\n                release_date, genres_json, raw_json, synced_at\n            )\n            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::date, %s, %s, %s::timestamp)\n            ON CONFLICT (tconst, locale) DO UPDATE SET\n                display_title = excluded.display_title,\n                original_title = excluded.original_title,\n                overview = excluded.overview,\n                poster_path = excluded.poster_path,\n                backdrop_path = excluded.backdrop_path,\n                release_date = excluded.release_date,\n                genres_json = excluded.genres_json,\n                raw_json = excluded.raw_json,\n                synced_at = excluded.synced_at\n            ', (tconst, locale, display_title, original_title, overview, poster_path, backdrop_path, release_date, genres_json, raw_json, synced_at))
        cursor.execute("DELETE FROM app.tmdb_watch_providers WHERE tconst = %s AND country_code = 'CZ'", (tconst,))
        for provider in providers:
            cursor.execute("\n                INSERT INTO app.tmdb_watch_providers (\n                    tconst, country_code, provider_type, provider_id, provider_name, logo_path, display_priority, synced_at\n                )\n                VALUES (%s, 'CZ', %s, %s, %s, %s, %s, %s::timestamp)\n                ", (tconst, provider.get('provider_type'), provider.get('provider_id'), provider.get('provider_name'), provider.get('logo_path'), provider.get('display_priority'), synced_at))
        conn.commit()

def insert_tmdb_asset_record(*, asset_id: str, tconst: str, asset_kind: str, relative_path: str, local_path: str, fetch_reason: str, status: str, sha256: str | None, fetched_at: str) -> None:
    """Insert one TMDB asset row into PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            INSERT INTO app.tmdb_assets (\n                id, tconst, asset_kind, relative_path, local_path, fetch_reason, status, sha256, fetched_at\n            )\n            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::timestamp)\n            ', (asset_id, tconst, asset_kind, relative_path, local_path, fetch_reason, status, sha256, fetched_at))
        conn.commit()

def fetch_latest_tmdb_assets_for_title(tconst: str) -> list[dict[str, Any]]:
    """Read TMDB asset history for one title from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT id, asset_kind, relative_path, local_path, fetch_reason, status, sha256, fetched_at\n            FROM app.tmdb_assets\n            WHERE tconst = %s\n            ORDER BY fetched_at DESC, id DESC\n            ', (tconst,))
        rows = cursor.fetchall()
    return [{'id': row[0], 'asset_kind': row[1], 'relative_path': row[2], 'local_path': row[3], 'fetch_reason': row[4], 'status': row[5], 'sha256': row[6], 'fetched_at': _parse_optional_timestamp(row[7])} for row in rows]

def fetch_tmdb_payload_snapshot(tconst: str, *, primary_locale: str, fallback_locale: str) -> dict[str, Any] | None:
    """Read mapping, best detail, locales and providers for one title from PostgreSQL."""
    mapping = fetch_tmdb_mapping_record(tconst)
    if mapping is None:
        return None
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT locale, display_title, overview, poster_path, backdrop_path, release_date, synced_at\n            FROM app.tmdb_title_details\n            WHERE tconst = %s\n            ORDER BY\n                CASE locale\n                    WHEN %s THEN 0\n                    WHEN %s THEN 1\n                    ELSE 2\n                END,\n                synced_at DESC\n            LIMIT 1\n            ', (tconst, primary_locale, fallback_locale))
        details = cursor.fetchone()
        cursor.execute('\n            SELECT locale\n            FROM app.tmdb_title_details\n            WHERE tconst = %s\n            ORDER BY\n                CASE locale\n                    WHEN %s THEN 0\n                    WHEN %s THEN 1\n                    ELSE 2\n                END,\n                synced_at DESC\n            ', (tconst, primary_locale, fallback_locale))
        detail_locales = cursor.fetchall()
        cursor.execute('\n            SELECT provider_type, provider_name, logo_path\n            FROM app.tmdb_watch_providers\n            WHERE tconst = %s\n            ORDER BY provider_type, display_priority NULLS LAST, provider_name\n            ', (tconst,))
        providers = cursor.fetchall()
    return {'mapping': mapping, 'details': {'locale': details[0], 'display_title': details[1], 'overview': details[2], 'poster_path': details[3], 'backdrop_path': details[4], 'release_date': _parse_optional_date(details[5]), 'synced_at': _parse_optional_timestamp(details[6])} if details else None, 'detail_locales': [str(row[0]) for row in detail_locales], 'providers': [{'provider_type': row[0], 'provider_name': row[1], 'logo_path': row[2]} for row in providers], 'assets': fetch_latest_tmdb_assets_for_title(tconst)}

def fetch_tmdb_completion_flags(tconsts: list[str], *, primary_locale: str, fallback_locale: str) -> dict[str, dict[str, Any]]:
    """Read TMDB completion-related flags for many titles in one PostgreSQL round-trip."""
    clean_tconsts = [str(tconst).strip() for tconst in tconsts if str(tconst).strip()]
    if not clean_tconsts:
        return {}
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            WITH detail_flags AS (\n                SELECT\n                    tconst,\n                    MAX(CASE WHEN locale = %s THEN 1 ELSE 0 END) AS has_primary,\n                    MAX(CASE WHEN locale = %s THEN 1 ELSE 0 END) AS has_fallback,\n                    MAX(CASE WHEN locale = %s THEN poster_path WHEN locale = %s THEN poster_path ELSE NULL END) AS poster_path,\n                    MAX(CASE WHEN locale = %s THEN backdrop_path WHEN locale = %s THEN backdrop_path ELSE NULL END) AS backdrop_path\n                FROM app.tmdb_title_details\n                WHERE tconst = ANY(%s)\n                GROUP BY tconst\n            ),\n            asset_flags AS (\n                SELECT\n                    tconst,\n                    MAX(CASE WHEN asset_kind = 'poster' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_poster,\n                    MAX(CASE WHEN asset_kind = 'backdrop' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_backdrop\n                FROM app.tmdb_assets\n                WHERE tconst = ANY(%s)\n                GROUP BY tconst\n            )\n            SELECT\n                m.tconst,\n                m.sync_status,\n                COALESCE(d.has_primary, 0),\n                COALESCE(d.has_fallback, 0),\n                d.poster_path,\n                d.backdrop_path,\n                COALESCE(a.has_poster, 0),\n                COALESCE(a.has_backdrop, 0)\n            FROM app.tmdb_title_map AS m\n            LEFT JOIN detail_flags AS d ON d.tconst = m.tconst\n            LEFT JOIN asset_flags AS a ON a.tconst = m.tconst\n            WHERE m.tconst = ANY(%s)\n            ", (primary_locale, fallback_locale, primary_locale, fallback_locale, primary_locale, fallback_locale, clean_tconsts, clean_tconsts, clean_tconsts))
        rows = cursor.fetchall()
    return {str(row[0]): {'sync_status': row[1], 'has_primary': bool(row[2]), 'has_fallback': bool(row[3]), 'poster_path': row[4], 'backdrop_path': row[5], 'has_poster': bool(row[6]), 'has_backdrop': bool(row[7])} for row in rows if row[0] is not None}

def replace_imdb_manifest_rows(rows: list[dict[str, Any]]) -> None:
    """Replace imdb_file_manifest rows in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('DELETE FROM app.imdb_file_manifest')
        if rows:
            cursor.executemany('\n                INSERT INTO app.imdb_file_manifest (\n                    source_key, source_path, source_mtime, source_size, source_sha256, recorded_at\n                )\n                VALUES (%s, %s, %s, %s, %s, %s::timestamp)\n                ', [(row['source_key'], row['source_path'], row['source_mtime'], row['source_size'], row['source_sha256'], row['recorded_at']) for row in rows])
        conn.commit()

def replace_catalog_refresh_meta_rows(rows: list[dict[str, Any]]) -> None:
    """Replace catalog_refresh_meta rows in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('DELETE FROM app.catalog_refresh_meta')
        if rows:
            cursor.executemany('\n                INSERT INTO app.catalog_refresh_meta (source_key, fingerprint)\n                VALUES (%s, %s)\n                ', [(row['source_key'], row['fingerprint']) for row in rows])
        conn.commit()

def fetch_imdb_manifest_rows() -> list[dict[str, Any]]:
    """Read imdb_file_manifest rows from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT source_key, source_path, source_mtime, source_size, source_sha256, recorded_at\n            FROM app.imdb_file_manifest\n            ORDER BY source_key\n            ')
        rows = cursor.fetchall()
    return [{'source_key': row[0], 'source_path': row[1], 'source_mtime': row[2], 'source_size': row[3], 'source_sha256': row[4], 'recorded_at': _parse_optional_timestamp(row[5])} for row in rows]

def fetch_catalog_refresh_rows() -> list[dict[str, Any]]:
    """Read catalog_refresh_meta rows from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            SELECT source_key, fingerprint\n            FROM app.catalog_refresh_meta\n            ORDER BY source_key\n            ')
        rows = cursor.fetchall()
    return [{'source_key': row[0], 'fingerprint': row[1]} for row in rows]

def fetch_catalog_refresh_fingerprint() -> str | None:
    """Read combined catalog refresh fingerprint from PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("\n            SELECT string_agg(source_key || '=' || fingerprint, '|' ORDER BY source_key)\n            FROM app.catalog_refresh_meta\n            ")
        row = cursor.fetchone()
    return None if row is None else row[0]

def local_seed_exists(seed_name: str) -> bool:
    """Return whether one local seed marker exists in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('SELECT 1 FROM app.local_seed_meta WHERE seed_name = %s LIMIT 1', (seed_name,))
        return cursor.fetchone() is not None

def record_local_seed_meta(*, seed_name: str, seeded_at: str, note: str | None) -> None:
    """Upsert one local seed marker in PostgreSQL."""
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute('\n            INSERT INTO app.local_seed_meta (seed_name, seeded_at, note)\n            VALUES (%s, %s::timestamp, %s)\n            ON CONFLICT (seed_name) DO UPDATE SET\n                seeded_at = excluded.seeded_at,\n                note = excluded.note\n            ', (seed_name, seeded_at, note))
        conn.commit()

def _row_to_user_rating(row: tuple[Any, ...] | list[Any]) -> dict[str, Any]:
    return {'canonical_key': row[0], 'tconst': row[1], 'rating': row[2], 'liked_notes': row[3], 'disliked_notes': row[4], 'rated_at': _parse_optional_timestamp(row[5]), 'updated_at': _parse_optional_timestamp(row[6])}

def _row_to_watch_event(row: tuple[Any, ...] | list[Any]) -> dict[str, Any]:
    return {'id': row[0], 'tconst': row[1], 'event_scope': row[2], 'watched_on': row[3], 'source': row[4], 'batch_id': row[5], 'import_row_id': row[6], 'rating': row[7], 'notes': row[8], 'created_at': _parse_optional_timestamp(row[9])}

def _row_to_content_state(row: tuple[Any, ...] | list[Any]) -> dict[str, Any]:
    return {'tconst': row[0], 'interest_state': row[1], 'last_previewed_at': _parse_optional_timestamp(row[2]), 'last_watched_at': _parse_optional_timestamp(row[3]), 'updated_at': _parse_optional_timestamp(row[4])}

def _row_to_title_role_signal(row: tuple[Any, ...] | list[Any]) -> dict[str, Any]:
    return {'signal_key': row[0], 'tconst': row[1], 'nconst': row[2], 'character_name': row[3], 'signal_type': row[4], 'polarity': row[5], 'strength': row[6], 'notes': row[7], 'source_origin': row[8], 'source_ref': row[9], 'created_at': _parse_optional_timestamp(row[10]), 'updated_at': _parse_optional_timestamp(row[11])}

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
    return psycopg.connect(host=config.host, port=config.port, dbname=config.database, user=config.user, password=config.password, connect_timeout=10)

@lru_cache(maxsize=1)
def _load_runtime_postgres_config() -> RuntimePostgresConfig:
    values = dict(dotenv_values(ENV_PATH, interpolate=False))
    password = values.get('POSTGRES_APP_PASSWORD') or ''
    if not password:
        raise RuntimeError('POSTGRES_APP_PASSWORD v .env chybí nebo je prázdné.')
    return RuntimePostgresConfig(host=values.get('POSTGRES_APP_HOST') or '/private/tmp', port=values.get('POSTGRES_APP_PORT') or '5432', database=values.get('POSTGRES_APP_DATABASE') or TARGET_DATABASE, user=values.get('POSTGRES_APP_USER') or 'filmy_app', password=password)
