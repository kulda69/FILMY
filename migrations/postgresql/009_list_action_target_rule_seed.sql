-- Seed prvni sady list-action pravidel pro akce s cilem.
-- V1 zamerne nepovoluje zapis do `watchlist` ani do `ai-navrhy`.

BEGIN;

WITH source_lists AS (
    SELECT
        id,
        slug
    FROM app.user_lists
    WHERE slug IN (
        'watchlist',
        'koukni-rychle',
        'kouknout-znou',
        'mam',
        'plex-library',
        'rozkoukano',
        'ai-navrhy',
        'nedokoukano',
        'stahnout'
    )
),
target_lists AS (
    SELECT
        id,
        slug
    FROM app.user_lists
    WHERE slug IN (
        'koukni-rychle',
        'kouknout-znou',
        'mam',
        'plex-library',
        'rozkoukano',
        'nedokoukano',
        'stahnout'
    )
),
seed AS (
    SELECT
        'rule:' || src.slug || ':copy_to_list:' || dst.slug || ':add_target' AS rule_id,
        src.id AS source_list_id,
        'copy_to_list'::text AS trigger_action,
        dst.id AS target_list_id,
        'add_target_membership'::text AS effect_type,
        'immediate'::text AS phase,
        10 AS order_index
    FROM source_lists AS src
    JOIN target_lists AS dst ON dst.id <> src.id

    UNION ALL

    SELECT
        'rule:' || src.slug || ':move_to_list:' || dst.slug || ':add_target',
        src.id,
        'move_to_list',
        dst.id,
        'add_target_membership',
        'immediate',
        10
    FROM source_lists AS src
    JOIN target_lists AS dst ON dst.id <> src.id

    UNION ALL

    SELECT
        'rule:' || src.slug || ':move_to_list:' || dst.slug || ':deactivate_source',
        src.id,
        'move_to_list',
        dst.id,
        'deactivate_source_membership',
        'finalize_only',
        20
    FROM source_lists AS src
    JOIN target_lists AS dst ON dst.id <> src.id
)
INSERT INTO app.list_action_rules (
    rule_id,
    source_list_id,
    trigger_action,
    target_list_id,
    effect_type,
    phase,
    order_index,
    enabled,
    lock_reason_key,
    lock_reason_text,
    effect_params,
    created_at,
    updated_at
)
SELECT
    seed.rule_id,
    seed.source_list_id,
    seed.trigger_action,
    seed.target_list_id,
    seed.effect_type,
    seed.phase,
    seed.order_index,
    TRUE,
    NULL,
    NULL,
    '{}'::jsonb,
    CURRENT_TIMESTAMP,
    CURRENT_TIMESTAMP
FROM seed
ON CONFLICT (rule_id) DO UPDATE SET
    target_list_id = excluded.target_list_id,
    effect_type = excluded.effect_type,
    phase = excluded.phase,
    order_index = excluded.order_index,
    enabled = TRUE,
    updated_at = CURRENT_TIMESTAMP;

COMMIT;
