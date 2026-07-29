-- List actions and title session runtime schema.
-- Skript je idempotentni a vytvari jen novou orchestration vrstvu.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'filmy' THEN
        RAISE EXCEPTION 'List actions runtime schema must be applied to database filmy, got %',
            current_database();
    END IF;
    IF pg_get_userbyid((SELECT nspowner FROM pg_namespace WHERE nspname = 'app'))
       IS DISTINCT FROM current_user THEN
        RAISE EXCEPTION 'Schema app must be owned by current administrator %', current_user;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS app.list_action_rules (
    rule_id text PRIMARY KEY,
    source_list_id text NOT NULL REFERENCES app.user_lists(id),
    trigger_action text NOT NULL,
    target_list_id text REFERENCES app.user_lists(id),
    effect_type text NOT NULL,
    phase text NOT NULL,
    order_index integer NOT NULL,
    enabled boolean NOT NULL DEFAULT TRUE,
    lock_reason_key text,
    lock_reason_text text,
    effect_params jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app.title_sessions (
    session_id text PRIMARY KEY,
    tconst text NOT NULL,
    status text NOT NULL,
    opened_from text,
    return_to_url text,
    source_list_id text REFERENCES app.user_lists(id),
    session_scope text NOT NULL DEFAULT 'title_detail',
    started_at timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finalized_at timestamp without time zone
);

CREATE TABLE IF NOT EXISTS app.title_session_actions (
    action_id text PRIMARY KEY,
    session_id text NOT NULL REFERENCES app.title_sessions(session_id) ON DELETE CASCADE,
    tconst text NOT NULL,
    source_list_id text REFERENCES app.user_lists(id),
    trigger_action text NOT NULL,
    target_list_id text REFERENCES app.user_lists(id),
    rating_value smallint,
    notes_text text,
    action_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    action_order integer NOT NULL,
    created_at timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app.title_session_effect_queue (
    effect_id text PRIMARY KEY,
    session_id text NOT NULL REFERENCES app.title_sessions(session_id) ON DELETE CASCADE,
    action_id text REFERENCES app.title_session_actions(action_id) ON DELETE SET NULL,
    rule_id text REFERENCES app.list_action_rules(rule_id) ON DELETE SET NULL,
    tconst text NOT NULL,
    effect_type text NOT NULL,
    phase text NOT NULL,
    source_list_id text REFERENCES app.user_lists(id),
    target_list_id text REFERENCES app.user_lists(id),
    effect_status text NOT NULL,
    effect_order integer NOT NULL,
    effect_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    executed_at timestamp without time zone
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'list_action_rules_trigger_action_check'
          AND conrelid = 'app.list_action_rules'::regclass
    ) THEN
        ALTER TABLE app.list_action_rules
            ADD CONSTRAINT list_action_rules_trigger_action_check
            CHECK (
                trigger_action = ANY (ARRAY[
                    'set_rating'::text,
                    'mark_watched'::text,
                    'copy_to_list'::text,
                    'move_to_list'::text,
                    'remove_from_list'::text,
                    'set_notes'::text
                ])
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'list_action_rules_effect_type_check'
          AND conrelid = 'app.list_action_rules'::regclass
    ) THEN
        ALTER TABLE app.list_action_rules
            ADD CONSTRAINT list_action_rules_effect_type_check
            CHECK (
                effect_type = ANY (ARRAY[
                    'write_rating'::text,
                    'derive_watched'::text,
                    'write_watched'::text,
                    'add_target_membership'::text,
                    'remove_source_membership'::text,
                    'deactivate_source_membership'::text,
                    'preserve_source_membership'::text,
                    'preserve_target_membership'::text,
                    'remove_target_membership'::text,
                    'noop'::text
                ])
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'list_action_rules_phase_check'
          AND conrelid = 'app.list_action_rules'::regclass
    ) THEN
        ALTER TABLE app.list_action_rules
            ADD CONSTRAINT list_action_rules_phase_check
            CHECK (phase = ANY (ARRAY['immediate'::text, 'finalize_only'::text]));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'list_action_rules_target_required_check'
          AND conrelid = 'app.list_action_rules'::regclass
    ) THEN
        ALTER TABLE app.list_action_rules
            ADD CONSTRAINT list_action_rules_target_required_check
            CHECK (
                (
                    trigger_action IN ('copy_to_list', 'move_to_list')
                    AND target_list_id IS NOT NULL
                )
                OR (
                    trigger_action IN ('set_rating', 'mark_watched', 'remove_from_list', 'set_notes')
                    AND target_list_id IS NULL
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'title_sessions_status_check'
          AND conrelid = 'app.title_sessions'::regclass
    ) THEN
        ALTER TABLE app.title_sessions
            ADD CONSTRAINT title_sessions_status_check
            CHECK (status = ANY (ARRAY['open'::text, 'finalizing'::text, 'finalized'::text, 'abandoned'::text]));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'title_sessions_session_scope_check'
          AND conrelid = 'app.title_sessions'::regclass
    ) THEN
        ALTER TABLE app.title_sessions
            ADD CONSTRAINT title_sessions_session_scope_check
            CHECK (
                session_scope = ANY (ARRAY[
                    'title_detail'::text,
                    'list_row_menu'::text,
                    'search_result'::text,
                    'system_import'::text
                ])
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'title_session_actions_trigger_action_check'
          AND conrelid = 'app.title_session_actions'::regclass
    ) THEN
        ALTER TABLE app.title_session_actions
            ADD CONSTRAINT title_session_actions_trigger_action_check
            CHECK (
                trigger_action = ANY (ARRAY[
                    'set_rating'::text,
                    'mark_watched'::text,
                    'copy_to_list'::text,
                    'move_to_list'::text,
                    'remove_from_list'::text,
                    'set_notes'::text
                ])
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'title_session_actions_target_required_check'
          AND conrelid = 'app.title_session_actions'::regclass
    ) THEN
        ALTER TABLE app.title_session_actions
            ADD CONSTRAINT title_session_actions_target_required_check
            CHECK (
                (
                    trigger_action IN ('copy_to_list', 'move_to_list')
                    AND target_list_id IS NOT NULL
                )
                OR (
                    trigger_action IN ('set_rating', 'mark_watched', 'remove_from_list', 'set_notes')
                    AND target_list_id IS NULL
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'title_session_actions_rating_range_check'
          AND conrelid = 'app.title_session_actions'::regclass
    ) THEN
        ALTER TABLE app.title_session_actions
            ADD CONSTRAINT title_session_actions_rating_range_check
            CHECK (rating_value IS NULL OR rating_value BETWEEN 0 AND 10);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'title_session_actions_session_order_key'
          AND conrelid = 'app.title_session_actions'::regclass
    ) THEN
        ALTER TABLE app.title_session_actions
            ADD CONSTRAINT title_session_actions_session_order_key
            UNIQUE (session_id, action_order);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'title_session_effect_queue_effect_type_check'
          AND conrelid = 'app.title_session_effect_queue'::regclass
    ) THEN
        ALTER TABLE app.title_session_effect_queue
            ADD CONSTRAINT title_session_effect_queue_effect_type_check
            CHECK (
                effect_type = ANY (ARRAY[
                    'write_rating'::text,
                    'derive_watched'::text,
                    'write_watched'::text,
                    'add_target_membership'::text,
                    'remove_source_membership'::text,
                    'deactivate_source_membership'::text,
                    'preserve_source_membership'::text,
                    'preserve_target_membership'::text,
                    'remove_target_membership'::text,
                    'noop'::text
                ])
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'title_session_effect_queue_phase_check'
          AND conrelid = 'app.title_session_effect_queue'::regclass
    ) THEN
        ALTER TABLE app.title_session_effect_queue
            ADD CONSTRAINT title_session_effect_queue_phase_check
            CHECK (phase = ANY (ARRAY['immediate'::text, 'finalize_only'::text]));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'title_session_effect_queue_effect_status_check'
          AND conrelid = 'app.title_session_effect_queue'::regclass
    ) THEN
        ALTER TABLE app.title_session_effect_queue
            ADD CONSTRAINT title_session_effect_queue_effect_status_check
            CHECK (
                effect_status = ANY (ARRAY[
                    'pending'::text,
                    'applied'::text,
                    'skipped'::text,
                    'cancelled'::text,
                    'failed'::text
                ])
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'title_session_effect_queue_session_order_key'
          AND conrelid = 'app.title_session_effect_queue'::regclass
    ) THEN
        ALTER TABLE app.title_session_effect_queue
            ADD CONSTRAINT title_session_effect_queue_session_order_key
            UNIQUE (session_id, effect_order);
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_list_action_rules_source_action_enabled
    ON app.list_action_rules (source_list_id, trigger_action, enabled);

CREATE UNIQUE INDEX IF NOT EXISTS idx_list_action_rules_order_key
    ON app.list_action_rules (
        source_list_id,
        trigger_action,
        COALESCE(target_list_id, ''),
        phase,
        order_index
    );

CREATE INDEX IF NOT EXISTS idx_list_action_rules_source_action_target_enabled
    ON app.list_action_rules (source_list_id, trigger_action, target_list_id, enabled);

CREATE INDEX IF NOT EXISTS idx_title_sessions_status_updated
    ON app.title_sessions (status, updated_at);

CREATE INDEX IF NOT EXISTS idx_title_sessions_tconst_status
    ON app.title_sessions (tconst, status);

CREATE INDEX IF NOT EXISTS idx_title_session_actions_session_created
    ON app.title_session_actions (session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_title_session_effect_queue_session_status_phase
    ON app.title_session_effect_queue (session_id, effect_status, phase);

CREATE INDEX IF NOT EXISTS idx_title_session_effect_queue_action
    ON app.title_session_effect_queue (action_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_list_action_rules_touch_updated_at'
          AND tgrelid = 'app.list_action_rules'::regclass
    ) THEN
        CREATE TRIGGER trg_list_action_rules_touch_updated_at
        BEFORE UPDATE ON app.list_action_rules
        FOR EACH ROW
        EXECUTE FUNCTION app.touch_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_title_sessions_touch_updated_at'
          AND tgrelid = 'app.title_sessions'::regclass
    ) THEN
        CREATE TRIGGER trg_title_sessions_touch_updated_at
        BEFORE UPDATE ON app.title_sessions
        FOR EACH ROW
        EXECUTE FUNCTION app.touch_updated_at();
    END IF;
END;
$$;

COMMIT;
