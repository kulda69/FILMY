from __future__ import annotations

import argparse
import atexit
import json
import signal
import threading
import time

from filmy.db import get_person_detail_cache_targets, get_title_detail_cache_targets
from filmy.integrations.tmdb import enrich_library_from_tmdb
from filmy.paths import METADATA_PIPELINE_SIGNAL_PATH
from filmy.scripts.materialize_person_details import materialize_person_detail_cache
from filmy.scripts.materialize_person_portraits import (
    get_person_portrait_pending_count,
    get_person_portrait_targets,
    materialize_person_portraits,
)
from filmy.scripts.materialize_title_details import materialize_title_detail_cache
from filmy.scripts.run_tmdb_backfill import get_counts as get_tmdb_counts


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
        while True:
            heartbeat_interval = float(state.get("heartbeat_interval_seconds") or interval_seconds)
            if stop_event.wait(heartbeat_interval):
                break
            _emit(
                {
                    "phase": "heartbeat",
                    "event": "alive",
                    "cycle_index": state.get("cycle_index"),
                    "stage": state.get("stage"),
                    "tmdb_remaining": state.get("tmdb_remaining"),
                    "detail_pending": state.get("detail_pending"),
                    "person_portrait_pending": state.get("person_portrait_pending"),
                    "person_detail_pending": state.get("person_detail_pending"),
                    "heartbeat_interval_seconds": heartbeat_interval,
                }
            )

    thread = threading.Thread(target=run, name="metadata-pipeline-heartbeat", daemon=True)
    thread.start()
    return stop_event, thread


def _get_signal_marker() -> int | None:
    try:
        return METADATA_PIPELINE_SIGNAL_PATH.stat().st_mtime_ns
    except OSError:
        return None


def _signal_changed(previous_marker: int | None) -> bool:
    return _get_signal_marker() != previous_marker


def main() -> int:
    _install_lifecycle_logging()
    parser = argparse.ArgumentParser(description="Sequential TMDB enrichment and detail cache materialization.")
    parser.add_argument("--tmdb-batch-size", type=int, default=10)
    parser.add_argument("--detail-batch-size", type=int, default=20)
    parser.add_argument("--person-portrait-batch-size", type=int, default=20)
    parser.add_argument("--person-detail-batch-size", type=int, default=20)
    parser.add_argument("--active-sleep-seconds", type=float, default=2.0)
    parser.add_argument("--idle-sleep-seconds", type=float, default=180.0)
    parser.add_argument("--wake-check-seconds", type=float, default=5.0)
    parser.add_argument("--sleep-seconds", type=float, default=None)
    parser.add_argument("--max-cycles", type=int, default=None)
    args = parser.parse_args()
    if args.sleep_seconds is not None:
        args.active_sleep_seconds = args.sleep_seconds

    initial_tmdb_total, initial_tmdb_complete = get_tmdb_counts()
    initial_detail_pending = len(get_title_detail_cache_targets(limit=args.detail_batch_size))
    initial_person_portrait_pending = get_person_portrait_pending_count()
    initial_person_detail_pending = len(get_person_detail_cache_targets(limit=args.person_detail_batch_size))
    _emit(
        {
            "phase": "start",
            "tmdb_total": initial_tmdb_total,
            "tmdb_complete": initial_tmdb_complete,
            "tmdb_remaining": initial_tmdb_total - initial_tmdb_complete,
            "detail_pending": initial_detail_pending,
            "person_portrait_pending": initial_person_portrait_pending,
            "person_detail_pending": initial_person_detail_pending,
            "tmdb_batch_size": args.tmdb_batch_size,
            "detail_batch_size": args.detail_batch_size,
            "person_portrait_batch_size": args.person_portrait_batch_size,
            "person_detail_batch_size": args.person_detail_batch_size,
            "active_sleep_seconds": args.active_sleep_seconds,
            "idle_sleep_seconds": args.idle_sleep_seconds,
            "wake_check_seconds": args.wake_check_seconds,
        }
    )

    cycle_index = 0
    progress_state: dict[str, object] = {
        "cycle_index": 0,
        "stage": "idle",
        "tmdb_remaining": initial_tmdb_total - initial_tmdb_complete,
        "detail_pending": initial_detail_pending,
        "person_portrait_pending": initial_person_portrait_pending,
        "person_detail_pending": initial_person_detail_pending,
        "heartbeat_interval_seconds": 10.0,
    }
    heartbeat_stop, heartbeat_thread = _start_heartbeat_thread(progress_state)
    signal_marker = _get_signal_marker()

    try:
        while True:
            if args.max_cycles is not None and cycle_index >= args.max_cycles:
                break

            cycle_index += 1
            progress_state["cycle_index"] = cycle_index

            tmdb_total_before, tmdb_complete_before = get_tmdb_counts()
            tmdb_remaining_before = max(tmdb_total_before - tmdb_complete_before, 0)
            detail_targets_before = get_title_detail_cache_targets(limit=args.detail_batch_size)
            detail_pending_before = len(detail_targets_before)
            person_portrait_pending_before = get_person_portrait_pending_count()
            person_detail_targets_before = get_person_detail_cache_targets(limit=args.person_detail_batch_size)
            person_detail_pending_before = len(person_detail_targets_before)
            progress_state["tmdb_remaining"] = tmdb_remaining_before
            progress_state["detail_pending"] = detail_pending_before
            progress_state["person_portrait_pending"] = person_portrait_pending_before
            progress_state["person_detail_pending"] = person_detail_pending_before
            progress_state["heartbeat_interval_seconds"] = 10.0

            _emit(
                {
                    "phase": "cycle_start",
                    "cycle_index": cycle_index,
                    "tmdb_remaining_before": tmdb_remaining_before,
                    "detail_pending_before": detail_pending_before,
                    "person_portrait_pending_before": person_portrait_pending_before,
                    "person_detail_pending_before": person_detail_pending_before,
                }
            )

            progress_state["stage"] = "tmdb"
            tmdb_result = enrich_library_from_tmdb(limit=args.tmdb_batch_size) if tmdb_remaining_before > 0 and args.tmdb_batch_size > 0 else {
                "processed": 0,
                "skipped": 0,
                "synced": 0,
                "not_found": 0,
                "partials": 0,
                "asset_fetches": 0,
                "errors": 0,
            }

            tmdb_total_after, tmdb_complete_after = get_tmdb_counts()
            tmdb_remaining_after = max(tmdb_total_after - tmdb_complete_after, 0)
            progress_state["tmdb_remaining"] = tmdb_remaining_after

            progress_state["stage"] = "detail"
            detail_result = materialize_title_detail_cache(limit=args.detail_batch_size, rewrite=False)
            detail_pending_after = len(get_title_detail_cache_targets(limit=args.detail_batch_size))
            progress_state["detail_pending"] = detail_pending_after

            progress_state["stage"] = "person_portrait"
            person_portrait_result = materialize_person_portraits(limit=args.person_portrait_batch_size, rewrite=False)
            person_portrait_pending_after = get_person_portrait_pending_count()
            progress_state["person_portrait_pending"] = person_portrait_pending_after

            progress_state["stage"] = "person_detail"
            person_detail_result = materialize_person_detail_cache(limit=args.person_detail_batch_size, rewrite=False)
            person_detail_pending_after = len(get_person_detail_cache_targets(limit=args.person_detail_batch_size))
            progress_state["person_detail_pending"] = person_detail_pending_after

            _emit(
                {
                    "phase": "cycle_done",
                    "cycle_index": cycle_index,
                    "tmdb_remaining_before": tmdb_remaining_before,
                    "tmdb_remaining_after": tmdb_remaining_after,
                    "detail_pending_before": detail_pending_before,
                    "detail_pending_after": detail_pending_after,
                    "person_portrait_pending_before": person_portrait_pending_before,
                    "person_portrait_pending_after": person_portrait_pending_after,
                    "person_detail_pending_before": person_detail_pending_before,
                    "person_detail_pending_after": person_detail_pending_after,
                    "tmdb_processed": tmdb_result["processed"],
                    "tmdb_synced": tmdb_result["synced"],
                    "tmdb_asset_fetches": tmdb_result["asset_fetches"],
                    "tmdb_errors": tmdb_result["errors"],
                    "detail_written": detail_result["written"],
                    "detail_cache_hits": detail_result["cache_hits"],
                    "detail_missing_detail": detail_result["missing_detail"],
                    "person_portrait_fetched": person_portrait_result["fetched"],
                    "person_portrait_cache_hits": person_portrait_result["cache_hits"],
                    "person_portrait_missing": person_portrait_result["missing"],
                    "person_portrait_errors": person_portrait_result["errors"],
                    "person_detail_written": person_detail_result["written"],
                    "person_detail_cache_hits": person_detail_result["cache_hits"],
                    "person_detail_missing_detail": person_detail_result["missing_detail"],
                }
            )

            progress_made = any(
                [
                    tmdb_result["processed"],
                    tmdb_result["synced"],
                    tmdb_result["asset_fetches"],
                    tmdb_result["errors"],
                    detail_result["written"],
                    person_portrait_result["fetched"],
                    person_portrait_result["errors"],
                    person_detail_result["written"],
                ]
            )
            counts_changed = any(
                [
                    tmdb_remaining_after != tmdb_remaining_before,
                    detail_pending_after != detail_pending_before,
                    person_portrait_pending_after != person_portrait_pending_before,
                    person_detail_pending_after != person_detail_pending_before,
                ]
            )
            should_idle_wait = not progress_made and not counts_changed

            if should_idle_wait:
                progress_state["stage"] = "idle_wait"
                progress_state["heartbeat_interval_seconds"] = min(max(args.wake_check_seconds * 2, 15.0), 60.0)
                _emit(
                    {
                        "phase": "idle_wait",
                        "cycle_index": cycle_index,
                        "idle_sleep_seconds": args.idle_sleep_seconds,
                        "wake_check_seconds": args.wake_check_seconds,
                    }
                )
                deadline = time.monotonic() + args.idle_sleep_seconds
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _emit({"phase": "idle_resume", "cycle_index": cycle_index, "reason": "periodic_recheck"})
                        break
                    if _signal_changed(signal_marker):
                        signal_marker = _get_signal_marker()
                        _emit({"phase": "idle_resume", "cycle_index": cycle_index, "reason": "activity_signal"})
                        break
                    time.sleep(min(args.wake_check_seconds, remaining))
            else:
                progress_state["stage"] = "sleep"
                progress_state["heartbeat_interval_seconds"] = 10.0
                time.sleep(args.active_sleep_seconds)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)

    _emit({"phase": "done", "cycle_index": cycle_index})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
