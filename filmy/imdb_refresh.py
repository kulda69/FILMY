from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filmy.paths import (
    IMDB_REFRESH_LOG_PATH,
    IMDB_REFRESH_PID_PATH,
    IMDB_REFRESH_STATUS_PATH,
    PROJECT_ROOT,
)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _read_pid() -> int | None:
    try:
        raw = IMDB_REFRESH_PID_PATH.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError):
        return None


def is_imdb_refresh_running() -> bool:
    pid = _read_pid()
    return pid is not None and _process_alive(pid)


def load_imdb_refresh_status() -> dict[str, Any]:
    base_status: dict[str, Any] = {
        "state": "idle",
        "stage": "idle",
        "message": "IMDb refresh jeste nebyl spusten.",
        "is_running": False,
        "pid": None,
        "updated_at": None,
        "started_at": None,
        "finished_at": None,
        "current_file": None,
        "files_total": 0,
        "files_downloaded": 0,
        "files_extracted": 0,
        "stats": None,
        "error": None,
    }

    if IMDB_REFRESH_STATUS_PATH.exists():
        try:
            stored = json.loads(IMDB_REFRESH_STATUS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                base_status.update(stored)
        except (OSError, json.JSONDecodeError):
            pass

    pid = _read_pid()
    running = pid is not None and _process_alive(pid)
    base_status["pid"] = pid
    base_status["is_running"] = running
    if running and base_status.get("state") not in {"running", "starting"}:
        base_status["state"] = "running"
    if not running and base_status.get("state") == "running":
        base_status["state"] = "unknown"
    return base_status


def read_imdb_refresh_log_tail(limit: int = 40) -> list[str]:
    if not IMDB_REFRESH_LOG_PATH.exists():
        return []
    try:
        lines = IMDB_REFRESH_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-limit:]


def get_imdb_refresh_snapshot(log_limit: int = 40) -> dict[str, Any]:
    return {
        "status": load_imdb_refresh_status(),
        "log_lines": read_imdb_refresh_log_tail(limit=log_limit),
    }


def start_imdb_refresh_job() -> dict[str, Any]:
    current = load_imdb_refresh_status()
    if current.get("is_running"):
        return current

    IMDB_REFRESH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": "starting",
        "stage": "starting",
        "message": "IMDb refresh se spousti.",
        "updated_at": _now_iso(),
        "started_at": _now_iso(),
        "finished_at": None,
        "current_file": None,
        "files_total": 0,
        "files_downloaded": 0,
        "files_extracted": 0,
        "stats": None,
        "error": None,
    }
    IMDB_REFRESH_STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log_handle = IMDB_REFRESH_LOG_PATH.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "filmy.scripts.run_imdb_refresh"],
        cwd=PROJECT_ROOT,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    IMDB_REFRESH_PID_PATH.write_text(f"{process.pid}\n", encoding="utf-8")
    log_handle.close()
    return load_imdb_refresh_status()
