from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filmy.db import get_tmdb_target_counts
from filmy.paths import ASSETS_DIR, DATA_DIR, METADATA_PIPELINE_SIGNAL_PATH, PROJECT_ROOT


def signal_background_activity(reason: str, *, target_tconst: str | None = None) -> None:
    """Wake the metadata pipeline after a user or admin write action."""

    METADATA_PIPELINE_SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": time.time(), "reason": reason}
    if target_tconst:
        payload["target_tconst"] = target_tconst
    METADATA_PIPELINE_SIGNAL_PATH.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def read_background_activity_signal() -> dict[str, Any] | None:
    try:
        raw = METADATA_PIPELINE_SIGNAL_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


@dataclass(frozen=True)
class JobSpec:
    """Definition of one supervised background worker."""

    name: str
    module: str
    args: tuple[str, ...]
    log_path: Path
    pid_path: Path
    stale_after_seconds: float
    restart_delay_seconds: float


@dataclass
class RunningJob:
    """Mutable runtime state for one worker process."""

    spec: JobSpec
    process: subprocess.Popen[str] | None = None
    log_handle: Any | None = None
    restart_count: int = 0
    last_started_at: float | None = None
    last_exit_code: int | None = None
    last_stop_reason: str | None = None
    next_restart_not_before: float = 0.0


class BackgroundJobSupervisor:
    """Starts, monitors and restarts long-running helper jobs."""

    def __init__(self) -> None:
        self._supervisor_log_path = DATA_DIR / "background_supervisor.log"
        self._jobs: dict[str, RunningJob] = {
            spec.name: RunningJob(spec=spec)
            for spec in (
                JobSpec(
                    name="metadata_pipeline",
                    module="filmy.scripts.run_metadata_pipeline",
                    args=(
                        "--tmdb-batch-size",
                        "10",
                        "--detail-batch-size",
                        "20",
                        "--person-portrait-batch-size",
                        "20",
                        "--person-detail-batch-size",
                        "0",
                        "--active-sleep-seconds",
                        "2",
                        "--idle-sleep-seconds",
                        "180",
                        "--wake-check-seconds",
                        "5",
                    ),
                    log_path=DATA_DIR / "metadata_pipeline.log",
                    pid_path=DATA_DIR / "metadata_pipeline.pid",
                    stale_after_seconds=240.0,
                    restart_delay_seconds=15.0,
                ),
            )
        }
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._homepage_snapshot_cache: dict[str, Any] | None = None
        self._homepage_snapshot_cached_at: float = 0.0
        self._homepage_snapshot_ttl_seconds: float = 15.0
        self._homepage_snapshot_refreshing: bool = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop_event.clear()
            for job in self._jobs.values():
                self._start_job(job, reason="startup")
            self._thread = threading.Thread(target=self._run, name="background-job-supervisor", daemon=True)
            self._thread.start()

    def cleanup_orphan_processes(self) -> None:
        """Stop previously supervised worker processes before app startup checks PostgreSQL."""
        with self._lock:
            for job in self._jobs.values():
                self._stop_existing_pid(job.spec)

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
            self._stop_event.set()
        if thread is not None:
            thread.join(timeout=5.0)
        for job in self._jobs.values():
            self._stop_job(job, reason="shutdown")

    def status(self) -> dict[str, Any]:
        with self._lock:
            jobs: list[dict[str, Any]] = []
            now = time.time()
            for job in self._jobs.values():
                process = job.process
                is_running = process is not None and process.poll() is None
                log_mtime = job.spec.log_path.stat().st_mtime if job.spec.log_path.exists() else None
                jobs.append(
                    {
                        "name": job.spec.name,
                        "module": job.spec.module,
                        "pid": process.pid if process is not None and is_running else None,
                        "is_running": is_running,
                        "restart_count": job.restart_count,
                        "last_started_at": job.last_started_at,
                        "last_exit_code": job.last_exit_code,
                        "last_stop_reason": job.last_stop_reason,
                        "log_path": job.spec.log_path.as_posix(),
                        "pid_path": job.spec.pid_path.as_posix(),
                        "seconds_since_log_update": (now - log_mtime) if log_mtime is not None else None,
                    }
                )
        return {"enabled": True, "jobs": jobs}

    def homepage_snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if (
                self._homepage_snapshot_cache is not None
                and (now - self._homepage_snapshot_cached_at) <= self._homepage_snapshot_ttl_seconds
            ):
                return dict(self._homepage_snapshot_cache)
            cached = dict(self._homepage_snapshot_cache) if self._homepage_snapshot_cache is not None else None
            if not self._homepage_snapshot_refreshing:
                self._homepage_snapshot_refreshing = True
                thread = threading.Thread(
                    target=self._refresh_homepage_snapshot,
                    name="homepage-snapshot-refresh",
                    daemon=True,
                )
                thread.start()
        if cached is not None:
            return cached
        status = self.status()
        jobs: list[dict[str, Any]] = []
        for item in status["jobs"]:
            seconds_since_log_update = item.get("seconds_since_log_update")
            health = "running" if item["is_running"] else "stopped"
            if item["is_running"] and seconds_since_log_update is not None and seconds_since_log_update > 60:
                health = "quiet"
            jobs.append(
                {
                    "name": item["name"],
                    "health": health,
                    "pid": item["pid"],
                    "restart_count": item["restart_count"],
                    "seconds_since_log_update": round(seconds_since_log_update, 1) if seconds_since_log_update is not None else None,
                }
            )
        return {
            "detail_cache_files": None,
            "tmdb_total_targets": None,
            "tmdb_complete_targets": None,
            "tmdb_remaining_targets": None,
            "jobs": jobs,
        }

    def _refresh_homepage_snapshot(self) -> None:
        status = self.status()
        tmdb_total: int | None = None
        tmdb_complete: int | None = None
        try:
            tmdb_total, tmdb_complete = self._tmdb_target_counts()
        except Exception:
            tmdb_total = None
            tmdb_complete = None
        detail_files = sum(1 for _ in ASSETS_DIR.rglob("detail.json")) if ASSETS_DIR.exists() else 0
        jobs: list[dict[str, Any]] = []
        for item in status["jobs"]:
            seconds_since_log_update = item.get("seconds_since_log_update")
            health = "running" if item["is_running"] else "stopped"
            if item["is_running"] and seconds_since_log_update is not None and seconds_since_log_update > 60:
                health = "quiet"
            jobs.append(
                {
                    "name": item["name"],
                    "health": health,
                    "pid": item["pid"],
                    "restart_count": item["restart_count"],
                    "seconds_since_log_update": round(seconds_since_log_update, 1) if seconds_since_log_update is not None else None,
                }
            )
        snapshot = {
            "detail_cache_files": detail_files,
            "tmdb_total_targets": tmdb_total,
            "tmdb_complete_targets": tmdb_complete,
            "tmdb_remaining_targets": max(tmdb_total - tmdb_complete, 0) if tmdb_total is not None and tmdb_complete is not None else None,
            "jobs": jobs,
        }
        with self._lock:
            self._homepage_snapshot_cache = dict(snapshot)
            self._homepage_snapshot_cached_at = time.time()
            self._homepage_snapshot_refreshing = False

    def _run(self) -> None:
        while not self._stop_event.wait(10.0):
            with self._lock:
                for job in self._jobs.values():
                    self._check_job(job)

    def _check_job(self, job: RunningJob) -> None:
        process = job.process
        now = time.time()
        if process is None:
            if now >= job.next_restart_not_before:
                self._start_job(job, reason="missing_process")
            return

        exit_code = process.poll()
        if exit_code is not None:
            job.last_exit_code = exit_code
            self._close_log_handle(job)
            job.process = None
            job.last_stop_reason = f"exit:{exit_code}"
            job.next_restart_not_before = now + job.spec.restart_delay_seconds
            self._write_supervisor_event(job.spec.name, "restart_scheduled", {"reason": job.last_stop_reason})
            return

        if self._is_stale(job, now):
            self._write_supervisor_event(job.spec.name, "stale_restart", {"stale_after_seconds": job.spec.stale_after_seconds})
            self._stop_job(job, reason="stale")
            job.next_restart_not_before = now + job.spec.restart_delay_seconds

        if job.process is None and now >= job.next_restart_not_before:
            self._start_job(job, reason="restart_after_stale")

    def _is_stale(self, job: RunningJob, now: float) -> bool:
        if not job.spec.log_path.exists():
            return False
        try:
            log_mtime = job.spec.log_path.stat().st_mtime
        except OSError:
            return False
        return (now - log_mtime) > job.spec.stale_after_seconds

    def _start_job(self, job: RunningJob, *, reason: str) -> None:
        job.spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        job.spec.pid_path.parent.mkdir(parents=True, exist_ok=True)
        self._stop_existing_pid(job.spec)
        log_handle = job.spec.log_path.open("a", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", job.spec.module, *job.spec.args],
            cwd=PROJECT_ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
        job.log_handle = log_handle
        job.process = process
        job.last_started_at = time.time()
        job.restart_count += 1
        job.last_exit_code = None
        job.last_stop_reason = None
        job.next_restart_not_before = 0.0
        job.spec.pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
        self._write_supervisor_event(job.spec.name, "started", {"pid": process.pid, "reason": reason})

    def _stop_job(self, job: RunningJob, *, reason: str) -> None:
        process = job.process
        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            process.wait(timeout=5.0)
        job.last_exit_code = process.returncode
        job.last_stop_reason = reason
        job.process = None
        self._close_log_handle(job)
        try:
            job.spec.pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._write_supervisor_event(job.spec.name, "stopped", {"reason": reason, "exit_code": process.returncode})

    def _close_log_handle(self, job: RunningJob) -> None:
        if job.log_handle is None:
            return
        try:
            job.log_handle.flush()
            job.log_handle.close()
        except OSError:
            pass
        finally:
            job.log_handle = None

    def _write_supervisor_event(self, job_name: str, event: str, payload: dict[str, Any]) -> None:
        self._supervisor_log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "job": job_name,
            "event": event,
            **payload,
        }
        with self._supervisor_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _tmdb_target_counts(self) -> tuple[int, int]:
        return get_tmdb_target_counts()

    def _stop_existing_pid(self, spec: JobSpec) -> None:
        if not spec.pid_path.exists():
            return
        try:
            raw_pid = spec.pid_path.read_text(encoding="utf-8").strip()
            pid = int(raw_pid)
        except (OSError, ValueError):
            return
        try:
            os.kill(pid, 0)
        except OSError:
            try:
                spec.pid_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                return
        try:
            spec.pid_path.unlink(missing_ok=True)
        except OSError:
            pass
