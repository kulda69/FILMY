"""Rebuildne IMDb katalog FILMY primo v PostgreSQL.

Flow:
1. aplikuje katalogove PG migrace,
2. porovna lokalni `imdb/*.tsv` proti `app.imdb_file_manifest`,
3. nahraje jen zmenene `imdb/*.tsv` do `raw.*`,
4. kdyz se neco zmenilo, nad `raw.*` prepocte `app.catalog_*`, `app.title_*`, `app.person_lookup`,
5. zapise refresh metadata do PostgreSQL.

Skript je urceny pro vedomy offline rebuild. Nesnazi se delat online swap.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys
from typing import Callable

import psycopg
from filmy.paths import IMDB_DIR, PROJECT_ROOT
from filmy.scripts.bootstrap_postgresql import (
    TARGET_DATABASE,
    _load_config as _load_admin_config,
    _run_psql,
    check as check_bootstrap,
)


SCHEMA_MIGRATION = PROJECT_ROOT / "migrations" / "postgresql" / "004_catalog_schema.sql"
GRANTS_MIGRATION = PROJECT_ROOT / "migrations" / "postgresql" / "005_catalog_grants.sql"


@dataclass(frozen=True)
class RawSource:
    table_name: str
    path: Path
    columns: tuple[str, ...]
    source_key: str

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


RAW_SOURCES = (
    RawSource(
        table_name="title_basics",
        path=IMDB_DIR / "title.basics.tsv",
        columns=("tconst", "title_type", "primary_title", "original_title", "is_adult", "start_year", "end_year", "runtime_minutes", "genres"),
        source_key="title_basics",
    ),
    RawSource(
        table_name="title_ratings",
        path=IMDB_DIR / "title.ratings.tsv",
        columns=("tconst", "average_rating", "num_votes"),
        source_key="title_ratings",
    ),
    RawSource(
        table_name="title_episode",
        path=IMDB_DIR / "title.episode.tsv",
        columns=("tconst", "parent_tconst", "season_number", "episode_number"),
        source_key="title_episode",
    ),
    RawSource(
        table_name="title_akas",
        path=IMDB_DIR / "title.akas.tsv",
        columns=("title_id", "ordering", "title", "region", "language", "types", "attributes", "is_original_title"),
        source_key="title_akas",
    ),
    RawSource(
        table_name="title_crew",
        path=IMDB_DIR / "title.crew.tsv",
        columns=("tconst", "directors", "writers"),
        source_key="title_crew",
    ),
    RawSource(
        table_name="title_principals",
        path=IMDB_DIR / "title.principals.tsv",
        columns=("tconst", "ordering", "nconst", "category", "job", "characters"),
        source_key="title_principals",
    ),
    RawSource(
        table_name="name_basics",
        path=IMDB_DIR / "name.basics.tsv",
        columns=("nconst", "primary_name", "birth_year", "death_year", "primary_profession", "known_for_titles"),
        source_key="name_basics",
    ),
)


def _connect_admin() -> psycopg.Connection:
    config = _load_admin_config()
    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=TARGET_DATABASE,
        user=config.user,
        password=config.password,
        connect_timeout=10,
    )


def _apply_catalog_schema() -> None:
    config = _load_admin_config()
    check_bootstrap(config)
    _run_psql(config, TARGET_DATABASE, "-f", str(SCHEMA_MIGRATION))
    _run_psql(config, TARGET_DATABASE, "-f", str(GRANTS_MIGRATION))


def _copy_tsv_to_raw(cursor: psycopg.Cursor, source: RawSource) -> None:
    column_sql = ", ".join(source.columns)
    cursor.execute(f"TRUNCATE raw.{source.table_name}")
    with cursor.copy(
        f"COPY raw.{source.table_name} ({column_sql}) FROM STDIN WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')"
    ) as copy:
        with source.path.open("r", encoding="utf-8", newline="") as handle:
            handle.readline()
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                copy.write(chunk)


def _fetch_stored_manifest(cursor: psycopg.Cursor) -> dict[str, dict[str, object]]:
    cursor.execute(
        """
        SELECT source_key, source_path, source_mtime, source_size, source_sha256
        FROM app.imdb_file_manifest
        """
    )
    rows = cursor.fetchall()
    return {
        str(row[0]): {
            "source_path": row[1],
            "source_mtime": int(row[2]) if row[2] is not None else None,
            "source_size": int(row[3]) if row[3] is not None else None,
            "source_sha256": row[4],
        }
        for row in rows
    }


def _detect_changed_sources(
    cursor: psycopg.Cursor,
    *,
    force: bool,
) -> tuple[list[RawSource], list[RawSource]]:
    if force:
        return list(RAW_SOURCES), []

    stored = _fetch_stored_manifest(cursor)
    if not stored:
        return list(RAW_SOURCES), []

    changed: list[RawSource] = []
    unchanged: list[RawSource] = []
    for source in RAW_SOURCES:
        stored_row = stored.get(source.source_key)
        if stored_row is None:
            changed.append(source)
            continue
        current_path = source.path.as_posix()
        current_mtime = source.stat_mtime
        current_size = source.stat_size
        if (
            stored_row["source_path"] == current_path
            and stored_row["source_mtime"] == current_mtime
            and stored_row["source_size"] == current_size
        ):
            unchanged.append(source)
            continue
        stored_sha = str(stored_row.get("source_sha256") or "")
        current_sha = source.sha256
        if stored_sha == current_sha:
            unchanged.append(source)
            continue
        changed.append(source)
    return changed, unchanged


def _rebuild_catalog(cursor: psycopg.Cursor) -> None:
    cursor.execute("TRUNCATE app.person_lookup, app.title_lookup, app.title_alias_lookup, app.title_credits, app.catalog_people, app.title_aliases, app.catalog_episodes, app.catalog_titles")

    cursor.execute(
        """
        INSERT INTO app.catalog_titles (
            tconst, title_type, primary_title, original_title, start_year, end_year,
            runtime_minutes, genres, average_rating, num_votes
        )
        SELECT
            b.tconst,
            b.title_type,
            b.primary_title,
            b.original_title,
            NULLIF(b.start_year, '')::integer,
            NULLIF(b.end_year, '')::integer,
            NULLIF(b.runtime_minutes, '')::integer,
            b.genres,
            NULLIF(r.average_rating, '')::double precision,
            NULLIF(r.num_votes, '')::integer
        FROM raw.title_basics AS b
        LEFT JOIN raw.title_ratings AS r USING (tconst)
        WHERE b.title_type IN ('movie', 'tvMovie', 'tvSeries', 'tvMiniSeries')
          AND CASE
                WHEN b.is_adult = '1' THEN TRUE
                WHEN b.is_adult = '0' THEN FALSE
                ELSE FALSE
              END = FALSE
          AND b.primary_title IS NOT NULL
        """
    )

    cursor.execute(
        """
        INSERT INTO app.catalog_episodes (
            episode_tconst, series_tconst, season_number, episode_number,
            primary_title, original_title, start_year, runtime_minutes
        )
        SELECT
            e.tconst,
            e.parent_tconst,
            NULLIF(e.season_number, '')::integer,
            NULLIF(e.episode_number, '')::integer,
            b.primary_title,
            b.original_title,
            NULLIF(b.start_year, '')::integer,
            NULLIF(b.runtime_minutes, '')::integer
        FROM raw.title_episode AS e
        JOIN raw.title_basics AS b ON b.tconst = e.tconst
        WHERE b.title_type = 'tvEpisode'
        """
    )

    cursor.execute(
        """
        INSERT INTO app.title_aliases (tconst, title, region, language, types, is_original_title)
        SELECT DISTINCT
            title_id,
            title,
            region,
            language,
            types,
            CASE
                WHEN is_original_title = '1' THEN TRUE
                WHEN is_original_title = '0' THEN FALSE
                ELSE NULL
            END
        FROM raw.title_akas
        WHERE title IS NOT NULL
        """
    )

    cursor.execute(
        """
        INSERT INTO app.catalog_people (
            nconst, primary_name, birth_year, death_year, primary_profession, known_for_titles
        )
        WITH title_scope AS (
            SELECT tconst FROM app.catalog_titles
        ),
        people_scope AS (
            SELECT DISTINCT nconst
            FROM raw.title_principals
            WHERE tconst IN (SELECT tconst FROM title_scope)

            UNION

            SELECT DISTINCT unnest(string_to_array(directors, ',')) AS nconst
            FROM raw.title_crew
            WHERE directors IS NOT NULL
              AND tconst IN (SELECT tconst FROM title_scope)

            UNION

            SELECT DISTINCT unnest(string_to_array(writers, ',')) AS nconst
            FROM raw.title_crew
            WHERE writers IS NOT NULL
              AND tconst IN (SELECT tconst FROM title_scope)
        )
        SELECT DISTINCT
            nconst,
            primary_name,
            NULLIF(birth_year, '')::integer,
            NULLIF(death_year, '')::integer,
            primary_profession,
            known_for_titles
        FROM raw.name_basics
        WHERE primary_name IS NOT NULL
          AND nconst IN (SELECT nconst FROM people_scope)
        """
    )

    cursor.execute(
        """
        INSERT INTO app.title_credits (
            tconst, nconst, credit_group, category, job, characters, ordering
        )
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
                NULLIF(p.ordering, '')::integer AS ordering
            FROM raw.title_principals AS p
            JOIN title_scope AS s USING (tconst)
        ),
        crew_credits AS (
            SELECT
                c.tconst,
                c.nconst,
                'director' AS credit_group,
                'director' AS category,
                NULL::text AS job,
                NULL::text AS characters,
                1000 + c.ordering AS ordering
            FROM (
                SELECT
                    tconst,
                    nconst,
                    row_number() OVER (PARTITION BY tconst ORDER BY nconst) AS ordering
                FROM (
                    SELECT tconst, unnest(string_to_array(directors, ',')) AS nconst
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
                NULL::text AS job,
                NULL::text AS characters,
                2000 + c.ordering AS ordering
            FROM (
                SELECT
                    tconst,
                    nconst,
                    row_number() OVER (PARTITION BY tconst ORDER BY nconst) AS ordering
                FROM (
                    SELECT tconst, unnest(string_to_array(writers, ',')) AS nconst
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

    cursor.execute(
        """
        INSERT INTO app.title_alias_lookup (
            tconst, title_type, primary_title, original_title, start_year, runtime_minutes,
            genres, average_rating, num_votes, title, region, language, alias_priority,
            alias_key, alias_key_articleless, alias_length, alias_length_articleless,
            alias_prefix1_articleless, alias_prefix2_articleless, alias_prefix3_articleless
        )
        SELECT
            a.tconst,
            t.title_type,
            t.primary_title,
            t.original_title,
            t.start_year,
            t.runtime_minutes,
            t.genres,
            t.average_rating,
            t.num_votes,
            a.title,
            a.region,
            a.language,
            app.alias_priority(a.region, a.language),
            app.normalize_match_key(a.title, FALSE),
            app.normalize_match_key(a.title, TRUE),
            length(app.normalize_match_key(a.title, FALSE)),
            length(app.normalize_match_key(a.title, TRUE)),
            left(app.normalize_match_key(a.title, TRUE), 1),
            left(app.normalize_match_key(a.title, TRUE), 2),
            left(app.normalize_match_key(a.title, TRUE), 3)
        FROM app.title_aliases AS a
        JOIN app.catalog_titles AS t ON t.tconst = a.tconst
        WHERE a.title IS NOT NULL
        """
    )

    cursor.execute(
        """
        INSERT INTO app.title_lookup (
            tconst, title_type, primary_title, original_title, start_year, runtime_minutes,
            genres, average_rating, num_votes, primary_key, original_key, primary_length,
            original_length, primary_prefix1, primary_prefix2, primary_prefix3,
            original_prefix1, original_prefix2, original_prefix3
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
            app.normalize_match_key(primary_title, TRUE),
            app.normalize_match_key(original_title, TRUE),
            length(app.normalize_match_key(primary_title, TRUE)),
            length(app.normalize_match_key(original_title, TRUE)),
            left(app.normalize_match_key(primary_title, TRUE), 1),
            left(app.normalize_match_key(primary_title, TRUE), 2),
            left(app.normalize_match_key(primary_title, TRUE), 3),
            left(app.normalize_match_key(original_title, TRUE), 1),
            left(app.normalize_match_key(original_title, TRUE), 2),
            left(app.normalize_match_key(original_title, TRUE), 3)
        FROM app.catalog_titles
        """
    )

    cursor.execute(
        """
        INSERT INTO app.person_lookup (
            nconst, primary_name, birth_year, death_year, primary_profession, known_for_titles, credit_count,
            name_key, first_token_key, last_token_key, compact_name_key,
            name_length, last_token_length, compact_name_length,
            name_prefix1, name_prefix2, name_prefix3,
            first_token_prefix1, first_token_prefix2, first_token_prefix3,
            last_token_prefix1, last_token_prefix2, last_token_prefix3,
            compact_name_prefix1, compact_name_prefix2, compact_name_prefix3
        )
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
            COALESCE(c.credit_count, 0),
            app.normalize_match_key(p.primary_name, FALSE),
            substring(app.normalize_match_key(p.primary_name, FALSE) FROM '^([a-z0-9]+)'),
            substring(app.normalize_match_key(p.primary_name, FALSE) FROM '([a-z0-9]+)$'),
            replace(app.normalize_match_key(p.primary_name, FALSE), ' ', ''),
            length(app.normalize_match_key(p.primary_name, FALSE)),
            length(substring(app.normalize_match_key(p.primary_name, FALSE) FROM '([a-z0-9]+)$')),
            length(replace(app.normalize_match_key(p.primary_name, FALSE), ' ', '')),
            left(app.normalize_match_key(p.primary_name, FALSE), 1),
            left(app.normalize_match_key(p.primary_name, FALSE), 2),
            left(app.normalize_match_key(p.primary_name, FALSE), 3),
            left(substring(app.normalize_match_key(p.primary_name, FALSE) FROM '^([a-z0-9]+)'), 1),
            left(substring(app.normalize_match_key(p.primary_name, FALSE) FROM '^([a-z0-9]+)'), 2),
            left(substring(app.normalize_match_key(p.primary_name, FALSE) FROM '^([a-z0-9]+)'), 3),
            left(substring(app.normalize_match_key(p.primary_name, FALSE) FROM '([a-z0-9]+)$'), 1),
            left(substring(app.normalize_match_key(p.primary_name, FALSE) FROM '([a-z0-9]+)$'), 2),
            left(substring(app.normalize_match_key(p.primary_name, FALSE) FROM '([a-z0-9]+)$'), 3),
            left(replace(app.normalize_match_key(p.primary_name, FALSE), ' ', ''), 1),
            left(replace(app.normalize_match_key(p.primary_name, FALSE), ' ', ''), 2),
            left(replace(app.normalize_match_key(p.primary_name, FALSE), ' ', ''), 3)
        FROM app.catalog_people AS p
        LEFT JOIN credit_counts AS c USING (nconst)
        WHERE p.primary_name IS NOT NULL
        """
    )

    cursor.execute(
        """
        DELETE FROM app.imdb_file_manifest;
        DELETE FROM app.catalog_refresh_meta;
        """
    )
    now = _now_iso()
    cursor.executemany(
        """
        INSERT INTO app.imdb_file_manifest (
            source_key, source_path, source_mtime, source_size, source_sha256, recorded_at
        )
        VALUES (%s, %s, %s, %s, %s, %s::timestamp)
        """,
        [
            (
                source.source_key,
                source.path.as_posix(),
                source.stat_mtime,
                source.stat_size,
                source.sha256,
                now,
            )
            for source in RAW_SOURCES
        ],
    )
    cursor.executemany(
        """
        INSERT INTO app.catalog_refresh_meta (source_key, fingerprint)
        VALUES (%s, %s)
        """,
        [
            (
                source.source_key,
                f"{source.stat_mtime}:{source.stat_size}",
            )
            for source in RAW_SOURCES
        ],
    )
    cursor.execute(
        """
        ANALYZE raw.title_basics;
        ANALYZE raw.title_ratings;
        ANALYZE raw.title_episode;
        ANALYZE raw.title_akas;
        ANALYZE raw.title_crew;
        ANALYZE raw.title_principals;
        ANALYZE raw.name_basics;
        ANALYZE app.catalog_titles;
        ANALYZE app.catalog_episodes;
        ANALYZE app.title_aliases;
        ANALYZE app.catalog_people;
        ANALYZE app.title_credits;
        ANALYZE app.title_alias_lookup;
        ANALYZE app.title_lookup;
        ANALYZE app.person_lookup;
        """
    )


def _collect_catalog_stats(cursor: psycopg.Cursor) -> dict[str, int]:
    stats: dict[str, int] = {}
    for key, table_name in (
        ("titles", "app.catalog_titles"),
        ("episodes", "app.catalog_episodes"),
        ("aliases", "app.title_aliases"),
        ("people", "app.catalog_people"),
        ("credits", "app.title_credits"),
    ):
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        stats[key] = int(cursor.fetchone()[0] or 0)
    return stats


def rebuild_catalog_from_current_imdb(
    *,
    force: bool = False,
    progress: Callable[..., None] | None = None,
) -> dict[str, int]:
    missing = [str(source.path) for source in RAW_SOURCES if not source.path.exists()]
    if missing:
        missing_paths = ", ".join(missing)
        raise RuntimeError(f"Chybi IMDb TSV soubory: {missing_paths}")

    if progress is not None:
        progress(stage="prepare", message="Aplikuji PostgreSQL katalogove schema.")
    _apply_catalog_schema()

    with _connect_admin() as conn:
        with conn.cursor() as cursor:
            changed_sources, unchanged_sources = _detect_changed_sources(cursor, force=force)
        if progress is not None and unchanged_sources:
            unchanged_names = ", ".join(source.path.name for source in unchanged_sources)
            progress(
                stage="refresh_catalog",
                message=f"Beze zmeny uz jsou: {unchanged_names}.",
            )
        if not changed_sources:
            with conn.cursor() as cursor:
                stats = _collect_catalog_stats(cursor)
            return stats

        total_sources = len(changed_sources)
        for index, source in enumerate(changed_sources, start=1):
            if progress is not None:
                progress(
                    stage="refresh_catalog",
                    message=f"Nahravam do PostgreSQL {source.path.name} ({index}/{total_sources}).",
                    current_file=source.path.name,
                )
            with conn.cursor() as cursor:
                _copy_tsv_to_raw(cursor, source)
            conn.commit()

        if progress is not None:
            progress(
                stage="refresh_catalog",
                message="Prestavim PostgreSQL katalog a lookup tabulky.",
                current_file=None,
            )
        with conn.cursor() as cursor:
            _rebuild_catalog(cursor)
        conn.commit()

        with conn.cursor() as cursor:
            stats = _collect_catalog_stats(cursor)
        return stats


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incremental IMDb TSV sync + catalog rebuild for PostgreSQL.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reload all raw IMDb TSV sources and rebuild catalog even if manifest says nothing changed.",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args(sys.argv[1:])
    try:
        last_message: str | None = None

        def _progress(**payload: object) -> None:
            nonlocal last_message
            message = str(payload.get("message") or "").strip()
            if message:
                last_message = message
                print(message, flush=True)

        stats = rebuild_catalog_from_current_imdb(force=args.force, progress=_progress)
        summary = ", ".join(f"{key}={value}" for key, value in stats.items())
        if last_message:
            print(f"{last_message} Hotovo: {summary}", flush=True)
        else:
            print(f"PostgreSQL katalog rebuild dokonceny. {summary}", flush=True)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
