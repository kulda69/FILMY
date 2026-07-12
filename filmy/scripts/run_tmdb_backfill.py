from __future__ import annotations

import argparse
import atexit
import json
import signal
import threading
import time

from filmy.db import get_tmdb_target_counts
from filmy.integrations.tmdb import enrich_library_from_tmdb
def get_counts() -> tuple[int, int]:
    return get_tmdb_target_counts()


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
