-- Grants for list actions and title session runtime schema.
-- Aplikuje se az po schema kroku 006.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'filmy' THEN
        RAISE EXCEPTION 'List actions runtime grants must be applied to database filmy, got %',
            current_database();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'filmy_app') THEN
        RAISE EXCEPTION 'Required role filmy_app does not exist';
    END IF;
END
$$;

REVOKE ALL PRIVILEGES ON TABLE
    app.list_action_rules,
    app.title_sessions,
    app.title_session_actions,
    app.title_session_effect_queue
FROM filmy_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    app.list_action_rules,
    app.title_sessions,
    app.title_session_actions,
    app.title_session_effect_queue
TO filmy_app;

COMMIT;
