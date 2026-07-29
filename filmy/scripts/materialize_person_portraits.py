"""CLI materializace person portrait assetu."""

from __future__ import annotations

import argparse
import json
import threading
from time import perf_counter

from filmy.db import _get_relevant_people_candidates
from filmy.integrations.tmdb import fetch_person_portrait, get_person_portrait_status


def _emit(payload: dict[str, object]) -> None:
    """Vypis jeden JSON zaznam o stavu stahovani portretu."""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _start_heartbeat_thread(state: dict[str, object], interval_seconds: float = 10.0) -> tuple[threading.Event, threading.Thread]:
    """Spust heartbeat vlakno pro delsi beh stahovani portretu."""
    stop_event = threading.Event()

    def run() -> None:
        """Pravidelne emituj heartbeat s poctem fetchu, chyb a missu."""
        while not stop_event.wait(interval_seconds):
            _emit(
                {
                    "phase": "heartbeat",
                    "event": "alive",
                    "index": state.get("index"),
                    "fetched": state.get("fetched"),
                    "cache_hits": state.get("cache_hits"),
                    "missing": state.get("missing"),
                    "errors": state.get("errors"),
                }
            )

    thread = threading.Thread(target=run, name="person-portrait-heartbeat", daemon=True)
    thread.start()
    return stop_event, thread


def get_person_portrait_targets(limit: int | None = None, include_ready: bool = False) -> list[dict[str, object]]:
    """Vrat relevantni osoby, kterym chybi portret nebo se ma prepsat."""
    seed_limit = None if limit is None else max(limit * 3, limit)
    people = _get_relevant_people_candidates(limit=seed_limit)
    items: list[dict[str, object]] = []
    for person in people:
        nconst = str(person["nconst"])
        portrait_status = get_person_portrait_status(nconst)
        if not include_ready and portrait_status["status"] in {"fetched", "no_profile", "not_found"}:
            continue
        items.append({**person, "portrait_status": portrait_status["status"]})
        if limit is not None and len(items) >= limit:
            break
    return items


def get_person_portrait_pending_count() -> int:
    """Spocitej, kolika relevantnim osobam jeste chybi portret."""
    people = _get_relevant_people_candidates(limit=None)
    pending = 0
    for person in people:
        nconst = str(person["nconst"])
        portrait_status = get_person_portrait_status(nconst)
        if portrait_status["status"] not in {"fetched", "no_profile", "not_found"}:
            pending += 1
    return pending


def materialize_person_portraits(limit: int | None = None, rewrite: bool = False) -> dict[str, object]:
    """Stahni nebo obnov portrety relevantnich osob z TMDB."""
    targets = get_person_portrait_targets(limit=limit, include_ready=rewrite)
    start = perf_counter()
    fetched = 0
    cache_hits = 0
    missing = 0
    errors = 0
    progress_state: dict[str, object] = {
        "index": 0,
        "fetched": 0,
        "cache_hits": 0,
        "missing": 0,
        "errors": 0,
    }
    heartbeat_stop, heartbeat_thread = _start_heartbeat_thread(progress_state)

    _emit({"phase": "start", "target_people": len(targets), "limit": limit, "rewrite": rewrite})

    try:
        for index, target in enumerate(targets, start=1):
            nconst = str(target["nconst"])
            progress_state["index"] = index
            progress_state["fetched"] = fetched
            progress_state["cache_hits"] = cache_hits
            progress_state["missing"] = missing
            progress_state["errors"] = errors

            if not rewrite and target.get("portrait_status") in {"fetched", "no_profile", "not_found"}:
                cache_hits += 1
                progress_state["cache_hits"] = cache_hits
                continue

            item_start = perf_counter()
            try:
                result = fetch_person_portrait(nconst, fetch_reason="background_person_portrait")
            except Exception as exc:
                errors += 1
                progress_state["errors"] = errors
                _emit(
                    {
                        "phase": "error",
                        "index": index,
                        "nconst": nconst,
                        "name": target.get("name"),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue

            elapsed = perf_counter() - item_start
            status = str(result.get("status") or "")
            if status == "fetched":
                fetched += 1
                progress_state["fetched"] = fetched
                _emit(
                    {
                        "phase": "write",
                        "index": index,
                        "nconst": nconst,
                        "name": target.get("name"),
                        "status": status,
                        "path": result.get("local_path"),
                        "elapsed_s": round(elapsed, 4),
                    }
                )
            elif status in {"no_profile", "not_found"}:
                missing += 1
                progress_state["missing"] = missing
                _emit(
                    {
                        "phase": "skip",
                        "index": index,
                        "nconst": nconst,
                        "name": target.get("name"),
                        "status": status,
                        "elapsed_s": round(elapsed, 4),
                    }
                )
            else:
                cache_hits += 1
                progress_state["cache_hits"] = cache_hits
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)

    summary = {
        "phase": "done",
        "target_people": len(targets),
        "fetched": fetched,
        "cache_hits": cache_hits,
        "missing": missing,
        "errors": errors,
        "elapsed_s": round(perf_counter() - start, 4),
    }
    _emit(summary)
    return summary


def main() -> int:
    """CLI vstup pro davkovou materializaci portretu osob."""
    parser = argparse.ArgumentParser(description="Materialize person portrait files.")
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of people to process.")
    parser.add_argument("--rewrite", action="store_true", help="Rewrite portrait state even when it already exists.")
    args = parser.parse_args()

    materialize_person_portraits(limit=args.limit, rewrite=args.rewrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
