from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path
from time import perf_counter

from filmy.db import _title_detail_cache_path, get_title_detail_cache_targets, get_title_presentation


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _start_heartbeat_thread(state: dict[str, object], interval_seconds: float = 10.0) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def run() -> None:
        while not stop_event.wait(interval_seconds):
            _emit(
                {
                    "phase": "heartbeat",
                    "event": "alive",
                    "index": state.get("index"),
                    "written": state.get("written"),
                    "cache_hits": state.get("cache_hits"),
                    "missing_detail": state.get("missing_detail"),
                }
            )

    thread = threading.Thread(target=run, name="title-cache-heartbeat", daemon=True)
    thread.start()
    return stop_event, thread


def _iter_target_titles(limit: int | None, rewrite: bool) -> list[dict[str, object]]:
    return get_title_detail_cache_targets(limit=limit, include_ready=rewrite)


def materialize_title_detail_cache(limit: int | None = None, rewrite: bool = False) -> dict[str, object]:
    targets = _iter_target_titles(limit=limit, rewrite=rewrite)
    start = perf_counter()
    written = 0
    cache_hits = 0
    missing_detail = 0
    progress_state: dict[str, object] = {
        "index": 0,
        "written": 0,
        "cache_hits": 0,
        "missing_detail": 0,
    }
    heartbeat_stop, heartbeat_thread = _start_heartbeat_thread(progress_state)

    _emit(
        {
            "phase": "start",
            "target_titles": len(targets),
            "limit": limit,
            "rewrite": rewrite,
        }
    )

    try:
        for index, target in enumerate(targets, start=1):
            tconst = str(target["tconst"])
            title = target.get("primary_title")
            year = target.get("start_year")
            progress_state["index"] = index
            progress_state["written"] = written
            progress_state["cache_hits"] = cache_hits
            progress_state["missing_detail"] = missing_detail

            cache_path = _title_detail_cache_path(tconst)
            cache_exists_before = cache_path.exists()
            cache_mtime_before = cache_path.stat().st_mtime_ns if cache_exists_before else None
            if cache_exists_before and rewrite:
                cache_path.unlink()

            item_start = perf_counter()
            item = get_title_presentation(tconst)
            elapsed = perf_counter() - item_start
            if item is None:
                missing_detail += 1
                progress_state["missing_detail"] = missing_detail
                _emit(
                    {
                        "phase": "skip",
                        "index": index,
                        "tconst": tconst,
                        "title": title,
                        "year": year,
                        "reason": "missing_detail",
                    }
                )
                continue

            cache_exists_after = cache_path.exists()
            cache_mtime_after = cache_path.stat().st_mtime_ns if cache_exists_after else None
            was_written = cache_exists_after and (
                not cache_exists_before or cache_mtime_before != cache_mtime_after or rewrite
            )
            if was_written:
                written += 1
                progress_state["written"] = written
                _emit(
                    {
                        "phase": "write",
                        "index": index,
                        "tconst": tconst,
                        "title": item.get("title"),
                        "year": item.get("year"),
                        "path": Path(cache_path).as_posix(),
                        "cache_status": target.get("cache_status"),
                        "elapsed_s": round(elapsed, 4),
                    }
                )
            else:
                cache_hits += 1
                progress_state["cache_hits"] = cache_hits
                _emit(
                    {
                        "phase": "hit",
                        "index": index,
                        "tconst": tconst,
                        "title": item.get("title"),
                        "year": item.get("year"),
                        "cache_status": target.get("cache_status"),
                        "elapsed_s": round(elapsed, 4),
                    }
                )
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)

    total_elapsed = perf_counter() - start
    summary = {
        "phase": "done",
        "target_titles": len(targets),
        "written": written,
        "cache_hits": cache_hits,
        "missing_detail": missing_detail,
        "elapsed_s": round(total_elapsed, 4),
    }
    _emit(summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize cached title detail files.")
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of titles to process.")
    parser.add_argument("--rewrite", action="store_true", help="Rewrite cache files even when they already exist.")
    args = parser.parse_args()

    materialize_title_detail_cache(limit=args.limit, rewrite=args.rewrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
