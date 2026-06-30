from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from time import time


ROOT = Path(__file__).resolve().parents[2]
JOB_SPECS = [
    {
        "name": "metadata_pipeline",
        "pid_file": ROOT / "data" / "metadata_pipeline.pid",
        "log_file": ROOT / "data" / "metadata_pipeline.log",
    },
]

ARCHIVAL_JOB_SPECS = [
    {
        "name": "tmdb_backfill_archive",
        "pid_file": ROOT / "data" / "tmdb_backfill.pid",
        "log_file": ROOT / "data" / "tmdb_backfill.log",
    },
    {
        "name": "title_details_cache_archive",
        "pid_file": ROOT / "data" / "title_details_cache.pid",
        "log_file": ROOT / "data" / "title_details_cache.log",
    },
]


def _read_last_line(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines:
        return None
    return lines[-1]


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _describe_job(spec: dict[str, Path | str]) -> dict[str, object]:
    pid_file = spec["pid_file"]
    log_file = spec["log_file"]
    job: dict[str, object] = {
        "name": spec["name"],
        "pid": None,
        "alive": False,
        "log_exists": log_file.exists(),
        "log_age_seconds": None,
        "last_log_line": _read_last_line(log_file) if log_file.exists() else None,
    }

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None:
            job["pid"] = pid
            job["alive"] = _process_alive(pid)

    if log_file.exists():
        try:
            job["log_age_seconds"] = round(time() - log_file.stat().st_mtime, 1)
        except OSError:
            job["log_age_seconds"] = None

    return job


def main() -> int:
    parser = argparse.ArgumentParser(description="Show status of background FILMY jobs.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    jobs = [_describe_job(spec) for spec in JOB_SPECS]
    archival_jobs = [_describe_job(spec) for spec in ARCHIVAL_JOB_SPECS]
    if args.json:
        print(json.dumps({"jobs": jobs, "archival_jobs": archival_jobs}, ensure_ascii=False, indent=2), flush=True)
        return 0

    print("Active jobs:")
    for job in jobs:
        alive = "alive" if job["alive"] else "dead"
        pid = job["pid"] if job["pid"] is not None else "-"
        log_age = job["log_age_seconds"] if job["log_age_seconds"] is not None else "-"
        print(f'{job["name"]}: {alive}, pid={pid}, log_age_s={log_age}')
        if job["last_log_line"]:
            print(f'  last: {job["last_log_line"]}')

    print("\nArchival maintenance tools:")
    for job in archival_jobs:
        alive = "alive" if job["alive"] else "dead"
        pid = job["pid"] if job["pid"] is not None else "-"
        log_age = job["log_age_seconds"] if job["log_age_seconds"] is not None else "-"
        print(f'{job["name"]}: {alive}, pid={pid}, log_age_s={log_age}')
        if job["last_log_line"]:
            print(f'  last: {job["last_log_line"]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
