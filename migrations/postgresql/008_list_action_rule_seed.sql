-- Seed prvni sady list-action pravidel pro bezcilove akce.
-- Zamerne jen `set_rating` a `mark_watched` nad dnesnimi realnymi seznamy.

BEGIN;

WITH source_lists AS (
    SELECT
        id,
        slug,
        CASE
            WHEN slug IN ('watchlist', 'koukni-rychle', 'rozkoukano', 'ai-navrhy') THEN 'deactivate'
            WHEN slug IN ('kouknout-znou', 'mam', 'plex-library', 'nedokoukano', 'stahnout') THEN 'preserve'
            ELSE NULL
        END AS cleanup_mode
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
    NULL,
    seed.effect_type,
    seed.phase,
    seed.order_index,
    TRUE,
    NULL,
    NULL,
    '{}'::jsonb,
    CURRENT_TIMESTAMP,
    CURRENT_TIMESTAMP
FROM (
    SELECT
        'rule:' || slug || ':set_rating:write_rating' AS rule_id,
        id AS source_list_id,
        'set_rating'::text AS trigger_action,
        'write_rating'::text AS effect_type,
        'immediate'::text AS phase,
        10 AS order_index
    FROM source_lists

    UNION ALL

    SELECT
        'rule:' || slug || ':set_rating:derive_watched',
        id,
        'set_rating',
        'derive_watched',
        'immediate',
        20
    FROM source_lists

    UNION ALL

    SELECT
        'rule:' || slug || ':set_rating:write_watched',
        id,
        'set_rating',
        'write_watched',
        'finalize_only',
        30
    FROM source_lists

    UNION ALL

    SELECT
        'rule:' || slug || ':set_rating:deactivate_source',
        id,
        'set_rating',
        'deactivate_source_membership',
        'finalize_only',
        40
    FROM source_lists
    WHERE cleanup_mode = 'deactivate'

    UNION ALL

    SELECT
        'rule:' || slug || ':set_rating:preserve_source',
        id,
        'set_rating',
        'preserve_source_membership',
        'finalize_only',
        40
    FROM source_lists
    WHERE cleanup_mode = 'preserve'

    UNION ALL

    SELECT
        'rule:' || slug || ':mark_watched:write_watched',
        id,
        'mark_watched',
        'write_watched',
        'finalize_only',
        10
    FROM source_lists

    UNION ALL

    SELECT
        'rule:' || slug || ':mark_watched:deactivate_source',
        id,
        'mark_watched',
        'deactivate_source_membership',
        'finalize_only',
        20
    FROM source_lists
    WHERE cleanup_mode = 'deactivate'

    UNION ALL

    SELECT
        'rule:' || slug || ':mark_watched:preserve_source',
        id,
        'mark_watched',
        'preserve_source_membership',
        'finalize_only',
        20
    FROM source_lists
    WHERE cleanup_mode = 'preserve'
) AS seed
ON CONFLICT (rule_id) DO UPDATE SET
    effect_type = excluded.effect_type,
    phase = excluded.phase,
    order_index = excluded.order_index,
    enabled = TRUE,
    updated_at = CURRENT_TIMESTAMP;

COMMIT;
