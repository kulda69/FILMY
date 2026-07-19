-- Opravneni se aplikuji az po exact fingerprint kontrole 002 runtime schematu.
-- Skript je transakcni a idempotentni; nezasahuje do default privileges.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'filmy' THEN
        RAISE EXCEPTION 'Runtime grants must be applied to database filmy, got %',
            current_database();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'filmy_app') THEN
        RAISE EXCEPTION 'Required role filmy_app does not exist';
    END IF;
END
$$;

REVOKE ALL PRIVILEGES ON DATABASE filmy FROM filmy_app;
REVOKE ALL PRIVILEGES ON SCHEMA app, old, public FROM filmy_app;
REVOKE ALL PRIVILEGES ON TABLE
    app.imdb_file_manifest,
    app.catalog_refresh_meta,
    app.tmdb_title_map,
    app.tmdb_title_details,
    app.tmdb_watch_providers,
    app.tmdb_assets,
    app.import_batches,
    app.import_rows,
    app.local_seed_meta,
    app.user_lists,
    app.user_list_items,
    app.watch_events,
    app.user_ratings,
    app.content_state,
    app.user_people,
    app.user_title_role_signals,
    app.favorite_genres,
    app.favorite_traits,
    app.genre_scores,
    app.search_recall,
    old.trakt_sync_runs,
    old.trakt_sync_files,
    old.trakt_history_events,
    old.trakt_ratings,
    old.trakt_lists,
    old.trakt_list_items,
    old.trakt_collection_items,
    old.trakt_history_snapshot,
    old.trakt_ratings_snapshot,
    old.trakt_list_items_snapshot,
    old.trakt_collection_snapshot,
    old.imdb_list_sync_runs,
    old.imdb_watchlist_items,
    old.imdb_favorite_people,
    old.plex_sync_runs,
    old.plex_library_items
FROM filmy_app;

GRANT CONNECT ON DATABASE filmy TO filmy_app;
GRANT USAGE ON SCHEMA app TO filmy_app;
GRANT USAGE ON SCHEMA old TO filmy_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    app.imdb_file_manifest,
    app.catalog_refresh_meta,
    app.tmdb_title_map,
    app.tmdb_title_details,
    app.tmdb_watch_providers,
    app.tmdb_assets,
    app.import_batches,
    app.import_rows,
    app.local_seed_meta,
    app.user_lists,
    app.user_list_items,
    app.watch_events,
    app.user_ratings,
    app.content_state,
    app.user_people,
    app.user_title_role_signals,
    app.favorite_genres,
    app.favorite_traits,
    app.genre_scores,
    app.search_recall,
    old.trakt_sync_runs,
    old.trakt_sync_files,
    old.trakt_history_events,
    old.trakt_ratings,
    old.trakt_lists,
    old.trakt_list_items,
    old.trakt_collection_items,
    old.trakt_history_snapshot,
    old.trakt_ratings_snapshot,
    old.trakt_list_items_snapshot,
    old.trakt_collection_snapshot,
    old.imdb_list_sync_runs,
    old.imdb_watchlist_items,
    old.imdb_favorite_people,
    old.plex_sync_runs,
    old.plex_library_items
TO filmy_app;

COMMIT;
