-- Mala zapisovana runtime vrstva FILMY. Katalog zustava v DuckDB.
-- Skript je idempotentni a vytvari pouze objekty vlastnene administratorem.
-- Opravneni filmy_app se udeluji az v 003 po exact fingerprint kontrole.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'filmy' THEN
        RAISE EXCEPTION 'Runtime schema must be applied to database filmy, got %',
            current_database();
    END IF;
    IF pg_get_userbyid((SELECT nspowner FROM pg_namespace WHERE nspname = 'app'))
       IS DISTINCT FROM current_user THEN
        RAISE EXCEPTION 'Schema app must be owned by current administrator %', current_user;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS app.imdb_file_manifest (
    source_key text PRIMARY KEY,
    source_path text NOT NULL,
    source_mtime bigint NOT NULL,
    source_size bigint NOT NULL,
    source_sha256 text NOT NULL,
    recorded_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS app.catalog_refresh_meta (
    source_key text NOT NULL,
    fingerprint text NOT NULL
);

CREATE TABLE IF NOT EXISTS app.tmdb_title_map (
    tconst text PRIMARY KEY,
    tmdb_media_type text NOT NULL,
    tmdb_id bigint NOT NULL,
    matched_by text NOT NULL,
    matched_at timestamp without time zone NOT NULL,
    sync_status text NOT NULL,
    last_error text
);

CREATE TABLE IF NOT EXISTS app.tmdb_title_details (
    tconst text NOT NULL,
    locale text NOT NULL,
    display_title text,
    original_title text,
    overview text,
    poster_path text,
    backdrop_path text,
    release_date text,
    genres_json text,
    raw_json text NOT NULL,
    synced_at timestamp without time zone NOT NULL,
    PRIMARY KEY (tconst, locale)
);

CREATE TABLE IF NOT EXISTS app.tmdb_watch_providers (
    tconst text NOT NULL,
    country_code text NOT NULL,
    provider_type text NOT NULL,
    provider_id bigint NOT NULL,
    provider_name text,
    logo_path text,
    display_priority integer,
    synced_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS app.tmdb_assets (
    id text PRIMARY KEY,
    tconst text NOT NULL,
    asset_kind text NOT NULL,
    relative_path text NOT NULL,
    local_path text NOT NULL,
    fetch_reason text NOT NULL,
    status text NOT NULL,
    sha256 text,
    fetched_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS app.import_batches (
    id text PRIMARY KEY,
    source text NOT NULL,
    filename text NOT NULL,
    checksum text NOT NULL,
    status text NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS app.import_rows (
    id text PRIMARY KEY,
    batch_id text NOT NULL,
    source text NOT NULL,
    row_number integer NOT NULL,
    raw_json text NOT NULL,
    parsed_title text,
    parsed_year integer,
    parsed_watched_on date,
    parsed_season_number integer,
    parsed_episode_number integer,
    parsed_imdb_id text,
    parsed_tmdb_id bigint,
    resolution_status text NOT NULL,
    resolved_tconst text,
    resolution_confidence double precision,
    resolution_note text
);

CREATE TABLE IF NOT EXISTS app.local_seed_meta (
    seed_name text PRIMARY KEY,
    seeded_at timestamp without time zone NOT NULL,
    note text
);

CREATE TABLE IF NOT EXISTS app.user_lists (
    id text PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    name text NOT NULL,
    list_kind text NOT NULL,
    source_origin text NOT NULL,
    source_ref text,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    description text
);

CREATE TABLE IF NOT EXISTS app.user_list_items (
    id text PRIMARY KEY,
    list_id text NOT NULL,
    canonical_key text NOT NULL,
    tconst text,
    media_type text NOT NULL,
    imdb_id text,
    tmdb_id bigint,
    trakt_id bigint,
    parent_tconst text,
    parent_title text,
    title text,
    season_number integer,
    episode_number integer,
    rank integer,
    added_at timestamp without time zone,
    notes text,
    source_origin text NOT NULL,
    source_ref text,
    is_archived boolean NOT NULL DEFAULT false,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    UNIQUE (list_id, canonical_key)
);

CREATE TABLE IF NOT EXISTS app.watch_events (
    id text PRIMARY KEY,
    tconst text NOT NULL,
    event_scope text NOT NULL,
    watched_on date NOT NULL,
    source text NOT NULL,
    batch_id text,
    import_row_id text,
    rating smallint,
    notes text,
    created_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS app.user_ratings (
    canonical_key text PRIMARY KEY,
    tconst text,
    media_type text NOT NULL,
    imdb_id text,
    tmdb_id bigint,
    trakt_id bigint,
    parent_tconst text,
    parent_title text,
    title text,
    season_number integer,
    episode_number integer,
    rating smallint NOT NULL,
    rated_at timestamp without time zone,
    source_origin text NOT NULL,
    source_ref text,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS app.content_state (
    tconst text PRIMARY KEY,
    interest_state text NOT NULL,
    last_previewed_at timestamp without time zone,
    last_watched_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS app.user_people (
    person_key text PRIMARY KEY,
    nconst text,
    name text NOT NULL,
    known_for text,
    birth_date text,
    source_origin text NOT NULL,
    source_ref text,
    is_favorite boolean NOT NULL DEFAULT true,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    affinity_rating integer
);

CREATE TABLE IF NOT EXISTS app.favorite_genres (
    genre text PRIMARY KEY,
    weight double precision NOT NULL DEFAULT 1.0,
    preference_rank integer,
    source_origin text NOT NULL,
    source_ref text,
    notes text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS app.favorite_traits (
    trait text PRIMARY KEY,
    weight double precision NOT NULL DEFAULT 1.0,
    preference_rank integer,
    source_origin text NOT NULL,
    source_ref text,
    notes text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS app.genre_scores (
    id text PRIMARY KEY,
    genre text NOT NULL,
    generated_at timestamp without time zone NOT NULL,
    algorithm_version text,
    score_scope text,
    source_origin text NOT NULL,
    source_ref text,
    titles_considered integer,
    watched_titles_considered integer,
    rated_titles_considered integer,
    contributing_titles_json text,
    excluded_titles_json text,
    favorite_genre_weight double precision,
    preference_overlap_score double precision,
    preference_alignment_score double precision,
    affinity_score double precision,
    rating_signal_score double precision,
    watch_signal_score double precision,
    recency_score double precision,
    actor_affinity_score double precision,
    frequency_score double precision,
    consistency_score double precision,
    novelty_score double precision,
    confidence_score double precision,
    manual_adjustment_score double precision,
    final_score double precision NOT NULL,
    normalized_score double precision,
    rank_in_run integer,
    metrics_json text,
    explanation text,
    created_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS app.search_recall (
    id text PRIMARY KEY,
    entity_type text NOT NULL,
    query_text text NOT NULL,
    query_text_fold text NOT NULL,
    query_key text NOT NULL,
    target_id text NOT NULL,
    target_label text,
    target_title_type text,
    matched_alias_title text,
    fuzzy_score double precision,
    first_searched_at timestamp without time zone NOT NULL,
    last_searched_at timestamp without time zone NOT NULL,
    hit_count integer NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS old.trakt_sync_runs (
    id text PRIMARY KEY,
    export_path text NOT NULL,
    export_fingerprint text NOT NULL,
    status text NOT NULL,
    summary_json text NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS old.trakt_sync_files (
    sync_run_id text NOT NULL,
    relative_path text NOT NULL,
    file_size bigint NOT NULL,
    file_mtime bigint NOT NULL,
    file_sha256 text NOT NULL,
    category text NOT NULL,
    item_count integer,
    imported boolean NOT NULL,
    PRIMARY KEY (sync_run_id, relative_path)
);

CREATE TABLE IF NOT EXISTS old.trakt_history_events (
    history_id bigint PRIMARY KEY,
    tconst text,
    media_type text NOT NULL,
    trakt_id bigint,
    imdb_id text,
    tmdb_id bigint,
    parent_trakt_id bigint,
    parent_title text,
    title text,
    season_number integer,
    episode_number integer,
    watched_at timestamp without time zone NOT NULL,
    watched_on date NOT NULL,
    action text,
    is_active boolean NOT NULL DEFAULT true,
    last_seen_sync_id text NOT NULL,
    raw_json text NOT NULL
);

CREATE TABLE IF NOT EXISTS old.trakt_ratings (
    source_key text PRIMARY KEY,
    media_type text NOT NULL,
    trakt_id bigint,
    imdb_id text,
    tmdb_id bigint,
    tconst text,
    parent_trakt_id bigint,
    parent_title text,
    title text,
    season_number integer,
    episode_number integer,
    rating smallint NOT NULL,
    rated_at timestamp without time zone NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    last_seen_sync_id text NOT NULL,
    raw_json text NOT NULL
);

CREATE TABLE IF NOT EXISTS old.trakt_lists (
    trakt_list_id bigint PRIMARY KEY,
    slug text,
    name text NOT NULL,
    description text,
    privacy text,
    list_type text,
    item_count integer,
    updated_at timestamp without time zone,
    is_active boolean NOT NULL DEFAULT true,
    last_seen_sync_id text NOT NULL,
    raw_json text NOT NULL
);

CREATE TABLE IF NOT EXISTS old.trakt_list_items (
    source_key text PRIMARY KEY,
    trakt_list_id text NOT NULL,
    list_kind text NOT NULL,
    list_name text,
    item_id bigint NOT NULL,
    media_type text NOT NULL,
    trakt_id bigint,
    imdb_id text,
    tmdb_id bigint,
    tconst text,
    parent_trakt_id bigint,
    parent_title text,
    title text,
    season_number integer,
    episode_number integer,
    rank integer,
    listed_at timestamp without time zone,
    notes text,
    my_rating smallint,
    is_active boolean NOT NULL DEFAULT true,
    last_seen_sync_id text NOT NULL,
    raw_json text NOT NULL
);

CREATE TABLE IF NOT EXISTS old.trakt_collection_items (
    source_key text PRIMARY KEY,
    media_type text NOT NULL,
    trakt_id bigint,
    imdb_id text,
    tmdb_id bigint,
    tconst text,
    parent_trakt_id bigint,
    parent_title text,
    title text,
    season_number integer,
    episode_number integer,
    collected_at timestamp without time zone,
    updated_at timestamp without time zone,
    is_active boolean NOT NULL DEFAULT true,
    last_seen_sync_id text NOT NULL,
    raw_json text NOT NULL
);

CREATE TABLE IF NOT EXISTS old.trakt_history_snapshot (
    sync_run_id text NOT NULL,
    history_id bigint NOT NULL,
    PRIMARY KEY (sync_run_id, history_id)
);

CREATE TABLE IF NOT EXISTS old.trakt_ratings_snapshot (
    sync_run_id text NOT NULL,
    source_key text NOT NULL,
    PRIMARY KEY (sync_run_id, source_key)
);

CREATE TABLE IF NOT EXISTS old.trakt_list_items_snapshot (
    sync_run_id text NOT NULL,
    source_key text NOT NULL,
    PRIMARY KEY (sync_run_id, source_key)
);

CREATE TABLE IF NOT EXISTS old.trakt_collection_snapshot (
    sync_run_id text NOT NULL,
    source_key text NOT NULL,
    PRIMARY KEY (sync_run_id, source_key)
);

CREATE TABLE IF NOT EXISTS old.imdb_list_sync_runs (
    id text PRIMARY KEY,
    export_path text NOT NULL,
    export_fingerprint text NOT NULL,
    status text NOT NULL,
    summary_json text NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS old.imdb_watchlist_items (
    tconst text PRIMARY KEY,
    position integer,
    created_at_src date,
    modified_at_src date,
    description text,
    title text,
    original_title text,
    url text,
    title_type text,
    imdb_rating double precision,
    runtime_minutes integer,
    year integer,
    genres text,
    num_votes integer,
    release_date date,
    directors text,
    your_rating smallint,
    date_rated date,
    is_active boolean NOT NULL DEFAULT true,
    last_seen_sync_id text NOT NULL,
    raw_json text NOT NULL
);

CREATE TABLE IF NOT EXISTS old.imdb_favorite_people (
    nconst text PRIMARY KEY,
    position integer,
    created_at_src date,
    modified_at_src date,
    description text,
    name text,
    known_for text,
    birth_date text,
    is_active boolean NOT NULL DEFAULT true,
    last_seen_sync_id text NOT NULL,
    raw_json text NOT NULL
);

CREATE TABLE IF NOT EXISTS old.plex_sync_runs (
    id text PRIMARY KEY,
    server_name text NOT NULL,
    server_client_identifier text NOT NULL,
    source_fingerprint text NOT NULL,
    status text NOT NULL,
    summary_json text NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS old.plex_library_items (
    source_key text PRIMARY KEY,
    plex_rating_key text NOT NULL,
    plex_guid text,
    section_key text NOT NULL,
    section_title text NOT NULL,
    library_type text NOT NULL,
    title text NOT NULL,
    year integer,
    imdb_id text,
    tmdb_id bigint,
    tvdb_id bigint,
    tconst text,
    view_count integer,
    viewed_leaf_count integer,
    leaf_count integer,
    last_viewed_at timestamp without time zone,
    added_at_src timestamp without time zone,
    updated_at_src timestamp without time zone,
    originally_available_at date,
    directors_json text NOT NULL,
    roles_json text NOT NULL,
    genres_json text NOT NULL,
    countries_json text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    last_seen_sync_id text NOT NULL,
    raw_json text NOT NULL
);

-- Indexy kopiruji hlavni runtime pristupy aplikace; nevytvareji vazbu na katalog.
CREATE INDEX IF NOT EXISTS idx_user_list_items_list_active
    ON app.user_list_items (list_id, is_archived, rank);
CREATE INDEX IF NOT EXISTS idx_user_list_items_tconst
    ON app.user_list_items (tconst);
CREATE INDEX IF NOT EXISTS idx_watch_events_tconst_watched
    ON app.watch_events (tconst, watched_on DESC);
CREATE INDEX IF NOT EXISTS idx_user_ratings_tconst
    ON app.user_ratings (tconst);
CREATE INDEX IF NOT EXISTS idx_content_state_interest
    ON app.content_state (interest_state);
CREATE INDEX IF NOT EXISTS idx_user_people_nconst
    ON app.user_people (nconst);
CREATE INDEX IF NOT EXISTS idx_user_people_favorite
    ON app.user_people (is_favorite);
CREATE INDEX IF NOT EXISTS idx_favorite_genres_active_rank
    ON app.favorite_genres (is_active, preference_rank);
CREATE INDEX IF NOT EXISTS idx_favorite_traits_active_rank
    ON app.favorite_traits (is_active, preference_rank);
CREATE INDEX IF NOT EXISTS idx_genre_scores_genre_generated_at
    ON app.genre_scores (genre, generated_at);
CREATE INDEX IF NOT EXISTS idx_genre_scores_scope_generated_at
    ON app.genre_scores (score_scope, generated_at);
CREATE INDEX IF NOT EXISTS idx_search_recall_entity_query_key
    ON app.search_recall (entity_type, query_key);
CREATE INDEX IF NOT EXISTS idx_search_recall_last_searched_at
    ON app.search_recall (last_searched_at);

COMMIT;
