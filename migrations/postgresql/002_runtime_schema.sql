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
    description text,
    ai_input_role text NOT NULL DEFAULT 'ignore'
);

ALTER TABLE app.user_lists
    ADD COLUMN IF NOT EXISTS ai_input_role text NOT NULL DEFAULT 'ignore';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'user_lists_ai_input_role_check'
          AND conrelid = 'app.user_lists'::regclass
    ) THEN
        ALTER TABLE app.user_lists
            ADD CONSTRAINT user_lists_ai_input_role_check
            CHECK (
                ai_input_role = ANY (ARRAY[
                    'strong_positive'::text,
                    'interested_owned'::text,
                    'interested_planned'::text,
                    'in_progress'::text,
                    'negative'::text,
                    'external_suggestion'::text,
                    'ignore'::text
                ])
            );
    END IF;
END;
$$;

UPDATE app.user_lists
SET ai_input_role = CASE
    WHEN slug = 'kouknout-znou' THEN 'strong_positive'
    WHEN slug = 'mam' THEN 'interested_owned'
    WHEN slug IN ('watchlist', 'koukni-rychle', 'stahnout') THEN 'interested_planned'
    WHEN slug = 'rozkoukano' THEN 'in_progress'
    WHEN slug = 'nedokoukano' THEN 'negative'
    WHEN slug = 'ai-navrhy' THEN 'external_suggestion'
    ELSE 'ignore'
END,
updated_at = CURRENT_TIMESTAMP
WHERE ai_input_role = 'ignore'
  AND slug IN (
      'kouknout-znou',
      'mam',
      'watchlist',
      'koukni-rychle',
      'stahnout',
      'rozkoukano',
      'nedokoukano',
      'ai-navrhy'
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
    updated_at timestamp without time zone NOT NULL,
    liked_notes text,
    disliked_notes text
);

ALTER TABLE app.user_ratings
    ADD COLUMN IF NOT EXISTS liked_notes text;

ALTER TABLE app.user_ratings
    ADD COLUMN IF NOT EXISTS disliked_notes text;

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

CREATE TABLE IF NOT EXISTS app.user_title_role_signals (
    signal_key text PRIMARY KEY,
    tconst text NOT NULL,
    nconst text,
    character_name text,
    signal_type text NOT NULL,
    polarity text NOT NULL DEFAULT 'positive',
    strength integer NOT NULL,
    notes text,
    source_origin text NOT NULL,
    source_ref text,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'watch_events_batch_import_row_key'
          AND conrelid = 'app.watch_events'::regclass
    ) THEN
        ALTER TABLE app.watch_events
            ADD CONSTRAINT watch_events_batch_import_row_key
            UNIQUE (batch_id, import_row_id);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'watch_events_event_scope_check'
          AND conrelid = 'app.watch_events'::regclass
    ) THEN
        ALTER TABLE app.watch_events
            ADD CONSTRAINT watch_events_event_scope_check
            CHECK (event_scope IN ('title', 'episode'));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'watch_events_rating_check'
          AND conrelid = 'app.watch_events'::regclass
    ) THEN
        ALTER TABLE app.watch_events
            ADD CONSTRAINT watch_events_rating_check
            CHECK (rating IS NULL OR rating BETWEEN 1 AND 10);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'user_ratings_rating_check'
          AND conrelid = 'app.user_ratings'::regclass
    ) THEN
        ALTER TABLE app.user_ratings
            ADD CONSTRAINT user_ratings_rating_check
            CHECK (rating BETWEEN 1 AND 10);
    END IF;
END
$$;

DO $$
BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'content_state_interest_state_check'
              AND conrelid = 'app.content_state'::regclass
        ) THEN
            ALTER TABLE app.content_state
            ADD CONSTRAINT content_state_interest_state_check
            CHECK (interest_state IN ('previewed', 'in_progress', 'watched'));
        END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'user_people_affinity_rating_check'
          AND conrelid = 'app.user_people'::regclass
    ) THEN
        ALTER TABLE app.user_people
            ADD CONSTRAINT user_people_affinity_rating_check
            CHECK (affinity_rating IS NULL OR affinity_rating BETWEEN 0 AND 10);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'user_title_role_signals_strength_check'
          AND conrelid = 'app.user_title_role_signals'::regclass
    ) THEN
        ALTER TABLE app.user_title_role_signals
            ADD CONSTRAINT user_title_role_signals_strength_check
            CHECK (strength BETWEEN 0 AND 10);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'user_title_role_signals_polarity_check'
          AND conrelid = 'app.user_title_role_signals'::regclass
    ) THEN
        ALTER TABLE app.user_title_role_signals
            ADD CONSTRAINT user_title_role_signals_polarity_check
            CHECK (polarity = ANY (ARRAY['positive'::text, 'negative'::text, 'mixed'::text]));
    END IF;

    ALTER TABLE app.user_title_role_signals
        DROP CONSTRAINT IF EXISTS user_title_role_signals_signal_type_check;

    ALTER TABLE app.user_title_role_signals
        ADD CONSTRAINT user_title_role_signals_signal_type_check
        CHECK (
            signal_type = ANY (ARRAY[
                'character'::text,
                'dialogue'::text,
                'behavior'::text,
                'relationship_dynamic'::text,
                'performance'::text,
                'visual_appeal'::text,
                'attraction'::text,
                'other'::text
            ])
        );
END
$$;

CREATE OR REPLACE FUNCTION app.commit_import_batch(
    p_batch_id text,
    p_committed_at timestamp without time zone
)
RETURNS TABLE(inserted_events integer, skipped_events integer, batch_status text)
LANGUAGE plpgsql
AS $$
DECLARE
    v_status text;
    v_inserted integer := 0;
    v_skipped integer := 0;
BEGIN
    SELECT status
    INTO v_status
    FROM app.import_batches
    WHERE id = p_batch_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Import batch % not found', p_batch_id;
    END IF;

    IF v_status = 'committed' THEN
        RETURN QUERY SELECT 0, 0, 'already_committed'::text;
        RETURN;
    END IF;

    IF v_status <> 'previewed' THEN
        RAISE EXCEPTION 'Import batch % is in unsupported status %', p_batch_id, v_status;
    END IF;

    WITH resolved_rows AS (
        SELECT
            row.id,
            row.source,
            row.parsed_watched_on,
            row.resolved_tconst,
            CASE
                WHEN row.parsed_season_number IS NOT NULL OR row.parsed_episode_number IS NOT NULL
                    THEN 'episode'
                ELSE 'title'
            END AS event_scope
        FROM app.import_rows AS row
        WHERE row.batch_id = p_batch_id
          AND row.resolution_status = 'resolved'
          AND row.resolved_tconst IS NOT NULL
          AND row.parsed_watched_on IS NOT NULL
    ), inserted AS (
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
        SELECT
            'import:' || p_batch_id || ':' || resolved.id,
            resolved.resolved_tconst,
            resolved.event_scope,
            resolved.parsed_watched_on,
            resolved.source,
            p_batch_id,
            resolved.id,
            NULL,
            NULL,
            p_committed_at
        FROM resolved_rows AS resolved
        ON CONFLICT ON CONSTRAINT watch_events_batch_import_row_key DO NOTHING
        RETURNING tconst
    ), updated_content_state AS (
        INSERT INTO app.content_state (
            tconst,
            interest_state,
            last_previewed_at,
            last_watched_at,
            updated_at
        )
        SELECT DISTINCT
            inserted.tconst,
            'watched',
            NULL,
            p_committed_at,
            p_committed_at
        FROM inserted
        ON CONFLICT (tconst) DO UPDATE
        SET interest_state = 'watched',
            last_watched_at = GREATEST(
                COALESCE(app.content_state.last_watched_at, '-infinity'::timestamp),
                EXCLUDED.last_watched_at
            ),
            updated_at = GREATEST(
                COALESCE(app.content_state.updated_at, '-infinity'::timestamp),
                EXCLUDED.updated_at
            )
        RETURNING 1
    )
    SELECT
        (SELECT COUNT(*) FROM inserted),
        GREATEST(
            (SELECT COUNT(*) FROM resolved_rows) - (SELECT COUNT(*) FROM inserted),
            0
        )
    INTO v_inserted, v_skipped;

    UPDATE app.import_batches
    SET status = 'committed'
    WHERE id = p_batch_id;

    RETURN QUERY SELECT v_inserted, v_skipped, 'committed'::text;
END;
$$;

CREATE OR REPLACE FUNCTION app.record_watched(
    p_event_id text,
    p_tconst text,
    p_event_scope text,
    p_watched_on date,
    p_notes text,
    p_created_at timestamp without time zone,
    p_archive_from_list_id text DEFAULT NULL,
    p_archive_canonical_key text DEFAULT NULL,
    p_archive_display_tconst text DEFAULT NULL
)
RETURNS TABLE(event_id text, content_state_changed boolean, archived_items integer)
LANGUAGE plpgsql
AS $$
DECLARE
    v_content_state_changed boolean := false;
    v_archived_items integer := 0;
BEGIN
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
    VALUES (
        p_event_id,
        p_tconst,
        p_event_scope,
        p_watched_on,
        'local_app',
        NULL,
        NULL,
        NULL,
        p_notes,
        p_created_at
    );

    INSERT INTO app.content_state (
        tconst,
        interest_state,
        last_previewed_at,
        last_watched_at,
        updated_at
    )
    VALUES (
        p_tconst,
        'watched',
        NULL,
        p_created_at,
        p_created_at
    )
    ON CONFLICT (tconst) DO UPDATE
    SET interest_state = 'watched',
        last_watched_at = GREATEST(
            COALESCE(app.content_state.last_watched_at, '-infinity'::timestamp),
            EXCLUDED.last_watched_at
        ),
        updated_at = GREATEST(
            COALESCE(app.content_state.updated_at, '-infinity'::timestamp),
            EXCLUDED.updated_at
        );
    GET DIAGNOSTICS v_content_state_changed = ROW_COUNT;
    v_content_state_changed := v_content_state_changed > 0;

    IF p_archive_from_list_id IS NOT NULL
       AND (p_archive_canonical_key IS NOT NULL OR p_archive_display_tconst IS NOT NULL) THEN
        WITH archive_candidates AS (
            SELECT item.id
            FROM app.user_list_items AS item
            LEFT JOIN app.catalog_episodes AS episode
                ON episode.episode_tconst = item.tconst
            WHERE item.list_id = p_archive_from_list_id
              AND item.is_archived = FALSE
              AND (
                  (p_archive_canonical_key IS NOT NULL AND item.canonical_key = p_archive_canonical_key)
                  OR (
                      p_archive_display_tconst IS NOT NULL
                      AND COALESCE(episode.series_tconst, item.tconst, item.parent_tconst) = p_archive_display_tconst
                  )
              )
        ), archived AS (
            UPDATE app.user_list_items AS item
            SET is_archived = TRUE,
                updated_at = p_created_at
            WHERE item.id IN (SELECT id FROM archive_candidates)
            RETURNING 1
        )
        SELECT COUNT(*)
        INTO v_archived_items
        FROM archived;
    END IF;

    RETURN QUERY SELECT p_event_id, v_content_state_changed, v_archived_items;
END;
$$;

CREATE OR REPLACE FUNCTION app.replace_favorite_genres(
    p_items jsonb,
    p_source_origin text,
    p_source_ref text,
    p_archive_missing boolean,
    p_now timestamp without time zone
)
RETURNS TABLE(touched_count integer, archived_count integer)
LANGUAGE plpgsql
AS $$
DECLARE
    v_touched integer := 0;
    v_archived integer := 0;
BEGIN
    WITH payload AS (
        SELECT
            trim(row.genre) AS genre,
            row.weight,
            row.preference_rank,
            row.notes,
            row.is_active
        FROM jsonb_to_recordset(COALESCE(p_items, '[]'::jsonb)) AS row(
            genre text,
            weight double precision,
            preference_rank integer,
            notes text,
            is_active boolean
        )
        WHERE trim(COALESCE(row.genre, '')) <> ''
    ), upserted AS (
        INSERT INTO app.favorite_genres (
            genre,
            weight,
            preference_rank,
            source_origin,
            source_ref,
            notes,
            is_active,
            created_at,
            updated_at
        )
        SELECT
            payload.genre,
            payload.weight,
            payload.preference_rank,
            p_source_origin,
            p_source_ref,
            payload.notes,
            COALESCE(payload.is_active, TRUE),
            p_now,
            p_now
        FROM payload
        ON CONFLICT (genre) DO UPDATE
        SET weight = EXCLUDED.weight,
            preference_rank = EXCLUDED.preference_rank,
            source_origin = EXCLUDED.source_origin,
            source_ref = EXCLUDED.source_ref,
            notes = EXCLUDED.notes,
            is_active = EXCLUDED.is_active,
            updated_at = EXCLUDED.updated_at
        RETURNING 1
    ), archived AS (
        UPDATE app.favorite_genres AS target
        SET is_active = FALSE,
            updated_at = p_now
        WHERE p_archive_missing
          AND NOT EXISTS (
              SELECT 1
              FROM payload
              WHERE payload.genre = target.genre
          )
          AND target.is_active = TRUE
        RETURNING 1
    )
    SELECT
        (SELECT COUNT(*) FROM upserted),
        (SELECT COUNT(*) FROM archived)
    INTO v_touched, v_archived;

    RETURN QUERY SELECT v_touched, v_archived;
END;
$$;

CREATE OR REPLACE FUNCTION app.replace_favorite_traits(
    p_items jsonb,
    p_source_origin text,
    p_source_ref text,
    p_archive_missing boolean,
    p_now timestamp without time zone
)
RETURNS TABLE(touched_count integer, archived_count integer)
LANGUAGE plpgsql
AS $$
DECLARE
    v_touched integer := 0;
    v_archived integer := 0;
BEGIN
    WITH payload AS (
        SELECT
            trim(row.trait) AS trait,
            row.weight,
            row.preference_rank,
            row.notes,
            row.is_active
        FROM jsonb_to_recordset(COALESCE(p_items, '[]'::jsonb)) AS row(
            trait text,
            weight double precision,
            preference_rank integer,
            notes text,
            is_active boolean
        )
        WHERE trim(COALESCE(row.trait, '')) <> ''
    ), upserted AS (
        INSERT INTO app.favorite_traits (
            trait,
            weight,
            preference_rank,
            source_origin,
            source_ref,
            notes,
            is_active,
            created_at,
            updated_at
        )
        SELECT
            payload.trait,
            payload.weight,
            payload.preference_rank,
            p_source_origin,
            p_source_ref,
            payload.notes,
            COALESCE(payload.is_active, TRUE),
            p_now,
            p_now
        FROM payload
        ON CONFLICT (trait) DO UPDATE
        SET weight = EXCLUDED.weight,
            preference_rank = EXCLUDED.preference_rank,
            source_origin = EXCLUDED.source_origin,
            source_ref = EXCLUDED.source_ref,
            notes = EXCLUDED.notes,
            is_active = EXCLUDED.is_active,
            updated_at = EXCLUDED.updated_at
        RETURNING 1
    ), archived AS (
        UPDATE app.favorite_traits AS target
        SET is_active = FALSE,
            updated_at = p_now
        WHERE p_archive_missing
          AND NOT EXISTS (
              SELECT 1
              FROM payload
              WHERE payload.trait = target.trait
          )
          AND target.is_active = TRUE
        RETURNING 1
    )
    SELECT
        (SELECT COUNT(*) FROM upserted),
        (SELECT COUNT(*) FROM archived)
    INTO v_touched, v_archived;

    RETURN QUERY SELECT v_touched, v_archived;
END;
$$;

CREATE OR REPLACE FUNCTION app.touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_user_lists_touch_updated_at'
          AND tgrelid = 'app.user_lists'::regclass
    ) THEN
        CREATE TRIGGER trg_user_lists_touch_updated_at
        BEFORE UPDATE ON app.user_lists
        FOR EACH ROW
        EXECUTE FUNCTION app.touch_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_user_list_items_touch_updated_at'
          AND tgrelid = 'app.user_list_items'::regclass
    ) THEN
        CREATE TRIGGER trg_user_list_items_touch_updated_at
        BEFORE UPDATE ON app.user_list_items
        FOR EACH ROW
        EXECUTE FUNCTION app.touch_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_user_ratings_touch_updated_at'
          AND tgrelid = 'app.user_ratings'::regclass
    ) THEN
        CREATE TRIGGER trg_user_ratings_touch_updated_at
        BEFORE UPDATE ON app.user_ratings
        FOR EACH ROW
        EXECUTE FUNCTION app.touch_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_user_people_touch_updated_at'
          AND tgrelid = 'app.user_people'::regclass
    ) THEN
        CREATE TRIGGER trg_user_people_touch_updated_at
        BEFORE UPDATE ON app.user_people
        FOR EACH ROW
        EXECUTE FUNCTION app.touch_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_user_title_role_signals_touch_updated_at'
          AND tgrelid = 'app.user_title_role_signals'::regclass
    ) THEN
        CREATE TRIGGER trg_user_title_role_signals_touch_updated_at
        BEFORE UPDATE ON app.user_title_role_signals
        FOR EACH ROW
        EXECUTE FUNCTION app.touch_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_favorite_genres_touch_updated_at'
          AND tgrelid = 'app.favorite_genres'::regclass
    ) THEN
        CREATE TRIGGER trg_favorite_genres_touch_updated_at
        BEFORE UPDATE ON app.favorite_genres
        FOR EACH ROW
        EXECUTE FUNCTION app.touch_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_favorite_traits_touch_updated_at'
          AND tgrelid = 'app.favorite_traits'::regclass
    ) THEN
        CREATE TRIGGER trg_favorite_traits_touch_updated_at
        BEFORE UPDATE ON app.favorite_traits
        FOR EACH ROW
        EXECUTE FUNCTION app.touch_updated_at();
    END IF;
END;
$$;

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
CREATE INDEX IF NOT EXISTS idx_user_title_role_signals_tconst
    ON app.user_title_role_signals (tconst);
CREATE INDEX IF NOT EXISTS idx_user_title_role_signals_nconst
    ON app.user_title_role_signals (nconst);
CREATE INDEX IF NOT EXISTS idx_user_title_role_signals_polarity_strength
    ON app.user_title_role_signals (polarity, strength DESC);
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
