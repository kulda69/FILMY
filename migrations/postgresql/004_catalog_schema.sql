-- PostgreSQL katalogova vrstva FILMY: raw IMDb TSV + odvozene app katalogy a lookupy.
-- Skript je idempotentni a vytvari objekty vlastnene administratorem.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'filmy' THEN
        RAISE EXCEPTION 'Catalog schema must be applied to database filmy, got %',
            current_database();
    END IF;
    IF pg_get_userbyid((SELECT nspowner FROM pg_namespace WHERE nspname = 'app'))
       IS DISTINCT FROM current_user THEN
        RAISE EXCEPTION 'Schema app must be owned by current administrator %', current_user;
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS raw;

CREATE OR REPLACE FUNCTION app.normalize_match_key(raw_text text, strip_leading_articles boolean DEFAULT false)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN strip_leading_articles THEN trim(regexp_replace(cleaned, '^(the|a|an)\s+', '', 'g'))
        ELSE cleaned
    END
    FROM (
        SELECT trim(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(
                                regexp_replace(
                                    regexp_replace(
                                        lower(unaccent(COALESCE(raw_text, ''))),
                                        '%', ' percent ', 'g'
                                    ),
                                    '\bper\s+cent\b', ' percent ', 'g'
                                ),
                                '\bpct\b', ' percent ', 'g'
                            ),
                            '\bprocent[a-z]*\b', ' percent ', 'g'
                        ),
                        '\(\s*[0-9]{4}\s*\)', ' ', 'g'
                    ),
                    '[^a-z0-9]+', ' ', 'g'
                ),
                '\s+', ' ', 'g'
            )
        ) AS cleaned
    ) AS normalized
$$;

CREATE OR REPLACE FUNCTION app.alias_priority(alias_region text, alias_language text)
RETURNS integer
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN lower(COALESCE(alias_language, '')) = 'cs' OR upper(COALESCE(alias_region, '')) = 'CZ' THEN 0
        WHEN lower(COALESCE(alias_language, '')) = 'en'
             OR upper(COALESCE(alias_region, '')) IN ('US', 'GB', 'CA', 'IE', 'AU', 'NZ', 'IN') THEN 1
        ELSE 2
    END
$$;

CREATE TABLE IF NOT EXISTS raw.title_basics (
    tconst text,
    title_type text,
    primary_title text,
    original_title text,
    is_adult text,
    start_year text,
    end_year text,
    runtime_minutes text,
    genres text
);

CREATE TABLE IF NOT EXISTS raw.title_ratings (
    tconst text,
    average_rating text,
    num_votes text
);

CREATE TABLE IF NOT EXISTS raw.title_episode (
    tconst text,
    parent_tconst text,
    season_number text,
    episode_number text
);

CREATE TABLE IF NOT EXISTS raw.title_akas (
    title_id text,
    ordering text,
    title text,
    region text,
    language text,
    types text,
    attributes text,
    is_original_title text
);

CREATE TABLE IF NOT EXISTS raw.title_crew (
    tconst text,
    directors text,
    writers text
);

CREATE TABLE IF NOT EXISTS raw.title_principals (
    tconst text,
    ordering text,
    nconst text,
    category text,
    job text,
    characters text
);

CREATE TABLE IF NOT EXISTS raw.name_basics (
    nconst text,
    primary_name text,
    birth_year text,
    death_year text,
    primary_profession text,
    known_for_titles text
);

CREATE TABLE IF NOT EXISTS app.catalog_titles (
    tconst text PRIMARY KEY,
    title_type text NOT NULL,
    primary_title text NOT NULL,
    original_title text,
    start_year integer,
    end_year integer,
    runtime_minutes integer,
    genres text,
    average_rating double precision,
    num_votes integer
);

CREATE TABLE IF NOT EXISTS app.catalog_episodes (
    episode_tconst text PRIMARY KEY,
    series_tconst text,
    season_number integer,
    episode_number integer,
    primary_title text,
    original_title text,
    start_year integer,
    runtime_minutes integer
);

CREATE TABLE IF NOT EXISTS app.title_aliases (
    tconst text NOT NULL,
    title text NOT NULL,
    region text,
    language text,
    types text,
    is_original_title boolean
);

CREATE TABLE IF NOT EXISTS app.catalog_people (
    nconst text PRIMARY KEY,
    primary_name text NOT NULL,
    birth_year integer,
    death_year integer,
    primary_profession text,
    known_for_titles text
);

CREATE TABLE IF NOT EXISTS app.title_credits (
    tconst text NOT NULL,
    nconst text NOT NULL,
    credit_group text NOT NULL,
    category text,
    job text,
    characters text,
    ordering integer
);

CREATE TABLE IF NOT EXISTS app.title_alias_lookup (
    tconst text NOT NULL,
    title_type text,
    primary_title text,
    original_title text,
    start_year integer,
    runtime_minutes integer,
    genres text,
    average_rating double precision,
    num_votes integer,
    title text,
    region text,
    language text,
    alias_priority integer NOT NULL,
    alias_key text NOT NULL,
    alias_key_articleless text NOT NULL,
    alias_length integer NOT NULL,
    alias_length_articleless integer NOT NULL,
    alias_prefix1_articleless text,
    alias_prefix2_articleless text,
    alias_prefix3_articleless text
);

CREATE TABLE IF NOT EXISTS app.title_lookup (
    tconst text PRIMARY KEY,
    title_type text,
    primary_title text,
    original_title text,
    start_year integer,
    runtime_minutes integer,
    genres text,
    average_rating double precision,
    num_votes integer,
    primary_key text NOT NULL,
    original_key text NOT NULL,
    primary_length integer NOT NULL,
    original_length integer NOT NULL,
    primary_prefix1 text,
    primary_prefix2 text,
    primary_prefix3 text,
    original_prefix1 text,
    original_prefix2 text,
    original_prefix3 text
);

CREATE TABLE IF NOT EXISTS app.person_lookup (
    nconst text PRIMARY KEY,
    primary_name text,
    birth_year integer,
    death_year integer,
    primary_profession text,
    known_for_titles text,
    credit_count integer NOT NULL,
    name_key text NOT NULL,
    first_token_key text,
    last_token_key text,
    compact_name_key text NOT NULL,
    name_length integer NOT NULL,
    last_token_length integer NOT NULL,
    compact_name_length integer NOT NULL,
    name_prefix1 text,
    name_prefix2 text,
    name_prefix3 text,
    first_token_prefix1 text,
    first_token_prefix2 text,
    first_token_prefix3 text,
    last_token_prefix1 text,
    last_token_prefix2 text,
    last_token_prefix3 text,
    compact_name_prefix1 text,
    compact_name_prefix2 text,
    compact_name_prefix3 text
);

CREATE INDEX IF NOT EXISTS idx_raw_title_basics_tconst ON raw.title_basics(tconst);
CREATE INDEX IF NOT EXISTS idx_raw_title_ratings_tconst ON raw.title_ratings(tconst);
CREATE INDEX IF NOT EXISTS idx_raw_title_episode_tconst ON raw.title_episode(tconst);
CREATE INDEX IF NOT EXISTS idx_raw_title_episode_parent_tconst ON raw.title_episode(parent_tconst);
CREATE INDEX IF NOT EXISTS idx_raw_title_akas_title_id ON raw.title_akas(title_id);
CREATE INDEX IF NOT EXISTS idx_raw_title_crew_tconst ON raw.title_crew(tconst);
CREATE INDEX IF NOT EXISTS idx_raw_title_principals_tconst ON raw.title_principals(tconst);
CREATE INDEX IF NOT EXISTS idx_raw_title_principals_nconst ON raw.title_principals(nconst);
CREATE INDEX IF NOT EXISTS idx_raw_name_basics_nconst ON raw.name_basics(nconst);

CREATE INDEX IF NOT EXISTS idx_catalog_titles_title_type ON app.catalog_titles(title_type);
CREATE INDEX IF NOT EXISTS idx_catalog_titles_start_year ON app.catalog_titles(start_year);
CREATE INDEX IF NOT EXISTS idx_catalog_episodes_series_tconst ON app.catalog_episodes(series_tconst);
CREATE INDEX IF NOT EXISTS idx_catalog_people_primary_name ON app.catalog_people(primary_name);
CREATE INDEX IF NOT EXISTS idx_title_aliases_tconst ON app.title_aliases(tconst);
CREATE INDEX IF NOT EXISTS idx_title_credits_tconst_group_ordering ON app.title_credits(tconst, credit_group, ordering);
CREATE INDEX IF NOT EXISTS idx_title_credits_nconst ON app.title_credits(nconst);

CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_tconst ON app.title_alias_lookup(tconst);
CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_alias_key ON app.title_alias_lookup(alias_key);
CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_alias_key_articleless ON app.title_alias_lookup(alias_key_articleless);
CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_prefix3 ON app.title_alias_lookup(alias_prefix3_articleless);
CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_prefix2 ON app.title_alias_lookup(alias_prefix2_articleless);
CREATE INDEX IF NOT EXISTS idx_title_alias_lookup_prefix1_len ON app.title_alias_lookup(alias_prefix1_articleless, alias_length_articleless);

CREATE INDEX IF NOT EXISTS idx_title_lookup_primary_key ON app.title_lookup(primary_key);
CREATE INDEX IF NOT EXISTS idx_title_lookup_original_key ON app.title_lookup(original_key);
CREATE INDEX IF NOT EXISTS idx_title_lookup_primary_prefix3 ON app.title_lookup(primary_prefix3);
CREATE INDEX IF NOT EXISTS idx_title_lookup_original_prefix3 ON app.title_lookup(original_prefix3);
CREATE INDEX IF NOT EXISTS idx_title_lookup_primary_prefix2 ON app.title_lookup(primary_prefix2);
CREATE INDEX IF NOT EXISTS idx_title_lookup_original_prefix2 ON app.title_lookup(original_prefix2);
CREATE INDEX IF NOT EXISTS idx_title_lookup_primary_prefix1_len ON app.title_lookup(primary_prefix1, primary_length);
CREATE INDEX IF NOT EXISTS idx_title_lookup_original_prefix1_len ON app.title_lookup(original_prefix1, original_length);

CREATE INDEX IF NOT EXISTS idx_person_lookup_name_key ON app.person_lookup(name_key);
CREATE INDEX IF NOT EXISTS idx_person_lookup_prefix3 ON app.person_lookup(name_prefix3);
CREATE INDEX IF NOT EXISTS idx_person_lookup_first_token_prefix3 ON app.person_lookup(first_token_prefix3);
CREATE INDEX IF NOT EXISTS idx_person_lookup_last_token_prefix3 ON app.person_lookup(last_token_prefix3);
CREATE INDEX IF NOT EXISTS idx_person_lookup_compact_prefix3 ON app.person_lookup(compact_name_prefix3);
CREATE INDEX IF NOT EXISTS idx_person_lookup_prefix2 ON app.person_lookup(name_prefix2);
CREATE INDEX IF NOT EXISTS idx_person_lookup_first_token_prefix2 ON app.person_lookup(first_token_prefix2);
CREATE INDEX IF NOT EXISTS idx_person_lookup_last_token_prefix2 ON app.person_lookup(last_token_prefix2);
CREATE INDEX IF NOT EXISTS idx_person_lookup_compact_prefix2 ON app.person_lookup(compact_name_prefix2);
CREATE INDEX IF NOT EXISTS idx_person_lookup_prefix1_len ON app.person_lookup(name_prefix1, name_length);
CREATE INDEX IF NOT EXISTS idx_person_lookup_last_token_prefix1_len ON app.person_lookup(last_token_prefix1, last_token_length);
CREATE INDEX IF NOT EXISTS idx_person_lookup_compact_prefix1_len ON app.person_lookup(compact_name_prefix1, compact_name_length);

CREATE OR REPLACE VIEW app.latest_title_posters AS
SELECT
    tconst,
    relative_path AS poster_relative_path,
    local_path AS poster_local_path,
    fetched_at,
    id
FROM (
    SELECT
        tconst,
        relative_path,
        local_path,
        fetched_at,
        id,
        row_number() OVER (PARTITION BY tconst ORDER BY fetched_at DESC, id DESC) AS rn
    FROM app.tmdb_assets
    WHERE asset_kind = 'poster' AND status = 'fetched'
) AS ranked
WHERE rn = 1;

CREATE OR REPLACE VIEW app.catalog_title_cards AS
SELECT
    t.tconst,
    t.title_type,
    t.start_year,
    t.primary_title,
    p.poster_relative_path,
    p.poster_local_path
FROM app.catalog_titles AS t
LEFT JOIN app.latest_title_posters AS p ON p.tconst = t.tconst;

CREATE OR REPLACE VIEW app.watched_display_rollup AS
SELECT
    COALESCE(e.series_tconst, w.tconst) AS display_tconst,
    COUNT(*)::integer AS watch_count,
    MAX(w.watched_on) AS latest_watched_on,
    MAX(COALESCE(w.created_at, CAST(w.watched_on AS timestamp without time zone))) AS latest_created_at
FROM app.watch_events AS w
LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
WHERE w.tconst IS NOT NULL
GROUP BY 1;

CREATE OR REPLACE VIEW app.active_user_list_display_items AS
SELECT
    i.id,
    i.list_id,
    l.slug AS list_slug,
    l.name AS list_name,
    l.list_kind,
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
    i.source_ref,
    i.created_at,
    i.updated_at,
    COALESCE(e.series_tconst, i.tconst, i.parent_tconst) AS display_tconst
FROM app.user_list_items AS i
JOIN app.user_lists AS l ON l.id = i.list_id
LEFT JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
WHERE i.is_archived = FALSE;

COMMIT;
