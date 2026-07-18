-- Opravneni katalogove vrstvy pro filmy_app.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'filmy' THEN
        RAISE EXCEPTION 'Catalog grants must be applied to database filmy, got %',
            current_database();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'filmy_app') THEN
        RAISE EXCEPTION 'Required role filmy_app does not exist';
    END IF;
END
$$;

REVOKE ALL PRIVILEGES ON SCHEMA raw FROM filmy_app;
REVOKE ALL PRIVILEGES ON TABLE
    app.catalog_titles,
    app.catalog_episodes,
    app.title_aliases,
    app.catalog_people,
    app.title_credits,
    app.title_alias_lookup,
    app.title_lookup,
    app.person_lookup,
    app.latest_title_posters,
    app.catalog_title_cards,
    app.watched_display_rollup,
    app.active_user_list_display_items
FROM filmy_app;

GRANT USAGE ON SCHEMA raw TO filmy_app;
GRANT SELECT ON TABLE
    raw.title_episode,
    raw.title_principals
TO filmy_app;
GRANT SELECT ON TABLE
    app.catalog_titles,
    app.catalog_episodes,
    app.title_aliases,
    app.catalog_people,
    app.title_credits,
    app.title_alias_lookup,
    app.title_lookup,
    app.person_lookup,
    app.latest_title_posters,
    app.catalog_title_cards,
    app.watched_display_rollup,
    app.active_user_list_display_items
TO filmy_app;

GRANT EXECUTE ON FUNCTION app.normalize_match_key(text, boolean) TO filmy_app;
GRANT EXECUTE ON FUNCTION app.alias_priority(text, text) TO filmy_app;

COMMIT;
