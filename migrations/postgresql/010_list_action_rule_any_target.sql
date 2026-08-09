BEGIN;

ALTER TABLE app.list_action_rules
    DROP CONSTRAINT IF EXISTS list_action_rules_target_required_check;

ALTER TABLE app.list_action_rules
    ADD CONSTRAINT list_action_rules_target_required_check
    CHECK (
        trigger_action IN ('copy_to_list', 'move_to_list')
        OR (
            trigger_action IN ('set_rating', 'mark_watched', 'remove_from_list', 'set_notes')
            AND target_list_id IS NULL
        )
    );

COMMENT ON CONSTRAINT list_action_rules_target_required_check ON app.list_action_rules IS
    'NULL target u copy/move konfigurace znamena wildcard pro jakykoli konkretni cil akce.';

COMMIT;
