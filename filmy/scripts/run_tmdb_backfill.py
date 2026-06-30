from __future__ import annotations

import argparse
import atexit
import json
import signal
import threading
import time

import duckdb

from filmy.db import DB_PATH
from filmy.integrations.tmdb import enrich_library_from_tmdb


TARGET_COUNTS_SQL = """
WITH candidates AS (
    SELECT w.tconst AS target_tconst, 1 AS priority
    FROM app.watch_events AS w
    JOIN app.catalog_titles AS t ON t.tconst = w.tconst
    GROUP BY 1, 2

    UNION ALL

    SELECT e.series_tconst AS target_tconst, 1 AS priority
    FROM app.watch_events AS w
    JOIN app.catalog_episodes AS e ON e.episode_tconst = w.tconst
    GROUP BY 1, 2

    UNION ALL

    SELECT cs.tconst AS target_tconst, 2 AS priority
    FROM app.content_state AS cs
    JOIN app.catalog_titles AS t ON t.tconst = cs.tconst
    WHERE cs.interest_state = 'in_progress'
    GROUP BY 1, 2

    UNION ALL

    SELECT e.series_tconst AS target_tconst, 2 AS priority
    FROM app.content_state AS cs
    JOIN app.catalog_episodes AS e ON e.episode_tconst = cs.tconst
    WHERE cs.interest_state = 'in_progress'
    GROUP BY 1, 2

    UNION ALL

    SELECT i.tconst AS target_tconst, 3 AS priority
    FROM app.user_list_items AS i
    JOIN app.user_lists AS l ON l.id = i.list_id
    JOIN app.catalog_titles AS t ON t.tconst = i.tconst
    WHERE i.is_archived = FALSE AND l.list_kind = 'watchlist'
    GROUP BY 1, 2

    UNION ALL

    SELECT e.series_tconst AS target_tconst, 3 AS priority
    FROM app.user_list_items AS i
    JOIN app.user_lists AS l ON l.id = i.list_id
    JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
    WHERE i.is_archived = FALSE AND l.list_kind = 'watchlist'
    GROUP BY 1, 2

    UNION ALL

    SELECT i.tconst AS target_tconst, 3 AS priority
    FROM app.user_list_items AS i
    JOIN app.user_lists AS l ON l.id = i.list_id
    JOIN app.catalog_titles AS t ON t.tconst = i.tconst
    WHERE i.is_archived = FALSE AND i.source_origin = 'seed_plex_library'
    GROUP BY 1, 2

    UNION ALL

    SELECT e.series_tconst AS target_tconst, 3 AS priority
    FROM app.user_list_items AS i
    JOIN app.user_lists AS l ON l.id = i.list_id
    JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
    WHERE i.is_archived = FALSE AND i.source_origin = 'seed_plex_library'
    GROUP BY 1, 2

    UNION ALL

    SELECT i.tconst AS target_tconst, 4 AS priority
    FROM app.user_list_items AS i
    JOIN app.user_lists AS l ON l.id = i.list_id
    JOIN app.catalog_titles AS t ON t.tconst = i.tconst
    WHERE i.is_archived = FALSE AND l.list_kind = 'custom' AND i.source_origin <> 'seed_plex_library'
    GROUP BY 1, 2

    UNION ALL

    SELECT e.series_tconst AS target_tconst, 4 AS priority
    FROM app.user_list_items AS i
    JOIN app.user_lists AS l ON l.id = i.list_id
    JOIN app.catalog_episodes AS e ON e.episode_tconst = i.tconst
    WHERE i.is_archived = FALSE AND l.list_kind = 'custom' AND i.source_origin <> 'seed_plex_library'
    GROUP BY 1, 2
),
ranked AS (
    SELECT target_tconst, MIN(priority) AS priority
    FROM candidates
    WHERE target_tconst IS NOT NULL
    GROUP BY 1
),
targets AS (
    SELECT r.target_tconst AS tconst, r.priority
    FROM ranked AS r
    LEFT JOIN app.tmdb_title_map AS m ON m.tconst = r.target_tconst
    WHERE COALESCE(m.sync_status, '') <> 'not_found'
),
detail_flags AS (
    SELECT
        tconst,
        MAX(CASE WHEN locale = 'en-US' THEN 1 ELSE 0 END) AS has_en,
        MAX(CASE WHEN locale = 'cs-CZ' THEN 1 ELSE 0 END) AS has_cs,
        MAX(CASE WHEN locale = 'en-US' THEN poster_path WHEN locale = 'cs-CZ' THEN poster_path ELSE NULL END) AS poster_path,
        MAX(CASE WHEN locale = 'en-US' THEN backdrop_path WHEN locale = 'cs-CZ' THEN backdrop_path ELSE NULL END) AS backdrop_path
    FROM app.tmdb_title_details
    GROUP BY 1
),
asset_flags AS (
    SELECT
        tconst,
        MAX(CASE WHEN asset_kind = 'poster' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_poster,
        MAX(CASE WHEN asset_kind = 'backdrop' AND status = 'fetched' THEN 1 ELSE 0 END) AS has_backdrop
    FROM app.tmdb_assets
    GROUP BY 1
)
SELECT
    COUNT(*) AS total,
    COUNT(*) FILTER (
        WHERE COALESCE(df.has_en, 0) = 1
          AND COALESCE(df.has_cs, 0) = 1
          AND (COALESCE(df.poster_path, '') = '' OR COALESCE(af.has_poster, 0) = 1)
          AND (COALESCE(df.backdrop_path, '') = '' OR COALESCE(af.has_backdrop, 0) = 1)
    ) AS complete
FROM targets AS t
LEFT JOIN detail_flags AS df USING (tconst)
LEFT JOIN asset_flags AS af USING (tconst)
"""


def get_counts() -> tuple[int, int]:
    with duckdb.connect(DB_PATH.as_posix(), read_only=True) as conn:
        total, complete = conn.execute(TARGET_COUNTS_SQL).fetchone()
    return int(total), int(complete)


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _install_lifecycle_logging() -> None:
    def handle_signal(signum: int, _: object) -> None:
        _emit({"phase": "signal", "signal": signal.Signals(signum).name})
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
        signal.signal(signum, handle_signal)

    atexit.register(lambda: _emit({"phase": "exit"}))


def _start_heartbeat_thread(state: dict[str, object], interval_seconds: float = 10.0) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def run() -> None:
        while not stop_event.wait(interval_seconds):
            _emit(
                {
                    "phase": "heartbeat",
                    "event": "alive",
                    "batch_index": state.get("batch_index"),
                    "processed": state.get("processed"),
                    "current_tconst": state.get("current_tconst"),
                    "remaining": state.get("remaining"),
                }
            )

    thread = threading.Thread(target=run, name="tmdb-heartbeat", daemon=True)
    thread.start()
    return stop_event, thread


def main() -> int:
    _install_lifecycle_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--max-stagnant-batches", type=int, default=2)
    args = parser.parse_args()

    total, complete = get_counts()
    print(
        json.dumps(
            {
                "phase": "start",
                "total": total,
                "complete": complete,
                "remaining": total - complete,
                "batch_size": args.batch_size,
                "sleep_seconds": args.sleep_seconds,
                "max_stagnant_batches": args.max_stagnant_batches,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    stagnant_batches = 0
    batch_index = 0
    progress_state: dict[str, object] = {
        "batch_index": batch_index,
        "processed": 0,
        "current_tconst": None,
        "remaining": total - complete,
    }
    heartbeat_stop, heartbeat_thread = _start_heartbeat_thread(progress_state)
    try:
        while True:
            if args.max_batches is not None and batch_index >= args.max_batches:
                break

            before_total, before_complete = get_counts()
            if before_complete >= before_total:
                break

            batch_index += 1
            progress_state["batch_index"] = batch_index
            progress_state["remaining"] = before_total - before_complete
            print(
                json.dumps(
                    {
                        "phase": "heartbeat",
                        "event": "batch_start",
                        "batch_index": batch_index,
                        "complete_before": before_complete,
                        "remaining_before": before_total - before_complete,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

            def emit_progress(event: dict[str, object]) -> None:
                if event.get("event") in {"done", "error", "not_found"}:
                    progress_state["processed"] = int(progress_state.get("processed") or 0) + 1
                progress_state["current_tconst"] = event.get("tconst")
                payload = {
                    "batch_index": batch_index,
                    **event,
                }
                print(json.dumps(payload, ensure_ascii=False), flush=True)

            try:
                result = enrich_library_from_tmdb(limit=args.batch_size, progress_callback=emit_progress)
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "phase": "error",
                            "batch_index": batch_index,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                raise
            after_total, after_complete = get_counts()
            delta_complete = after_complete - before_complete
            progress_state["remaining"] = after_total - after_complete

            print(
                json.dumps(
                    {
                        "phase": "batch",
                        "batch_index": batch_index,
                        "processed": result["processed"],
                        "skipped": result["skipped"],
                    "synced": result["synced"],
                    "not_found": result["not_found"],
                    "partials": result["partials"],
                        "asset_fetches": result["asset_fetches"],
                        "errors": result["errors"],
                        "complete_before": before_complete,
                        "complete_after": after_complete,
                        "delta_complete": delta_complete,
                        "remaining": after_total - after_complete,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

            if delta_complete <= 0:
                stagnant_batches += 1
            else:
                stagnant_batches = 0

            if stagnant_batches >= args.max_stagnant_batches:
                print(
                    json.dumps(
                        {
                            "phase": "stop",
                            "reason": "stagnant",
                            "stagnant_batches": stagnant_batches,
                            "remaining": after_total - after_complete,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                break

            print(
                json.dumps(
                    {
                        "phase": "heartbeat",
                        "event": "sleep",
                        "batch_index": batch_index,
                        "sleep_seconds": args.sleep_seconds,
                        "stagnant_batches": stagnant_batches,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            time.sleep(args.sleep_seconds)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)

    final_total, final_complete = get_counts()
    print(
        json.dumps(
            {
                "phase": "done",
                "batches": batch_index,
                "total": final_total,
                "complete": final_complete,
                "remaining": final_total - final_complete,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
