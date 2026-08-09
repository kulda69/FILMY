"""CLI wrapper pro periodicky IMDb refresh workflow."""

from __future__ import annotations

import atexit
import gzip
import json
import shutil
import signal
import ssl
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.request import urlopen

import certifi

from filmy.paths import (
    IMDB_DIR,
    IMDB_REFRESH_DIR,
    IMDB_REFRESH_PID_PATH,
    IMDB_REFRESH_STATUS_PATH,
)
from filmy.scripts.rebuild_catalog_postgresql import rebuild_catalog_from_current_imdb


IMDB_DATASET_BASE_URL = "https://datasets.imdbws.com"
IMDB_DATASET_FILES = (
    "title.basics.tsv.gz",
    "title.ratings.tsv.gz",
    "title.akas.tsv.gz",
    "title.episode.tsv.gz",
    "title.crew.tsv.gz",
    "title.principals.tsv.gz",
    "name.basics.tsv.gz",
)


def _now_ts() -> float:
    """Vrat aktualni cas jako unix timestamp."""
    import time

    return time.time()


def _now_iso() -> str:
    """Vrat aktualni UTC cas v ISO formatu bez mikrosekund."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _emit(payload: dict[str, object]) -> None:
    """Vypis jeden strojove cteny log zaznam."""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _write_status(**updates: object) -> None:
    """Uloz prubezny stav refresh behu do status JSONu."""
    current: dict[str, object] = {}
    if IMDB_REFRESH_STATUS_PATH.exists():
        try:
            current = json.loads(IMDB_REFRESH_STATUS_PATH.read_text(encoding="utf-8"))
            if not isinstance(current, dict):
                current = {}
        except (OSError, json.JSONDecodeError):
            current = {}
    current.update(updates)
    current["updated_at"] = _now_iso()
    IMDB_REFRESH_STATUS_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _set_error(message: str) -> None:
    """Zapis chybovy stav a soucasne ho emituj do stdout logu."""
    _write_status(state="error", stage="error", message=message, error=message, finished_at=_now_iso())
    _emit({"phase": "error", "message": message})


def _install_lifecycle_logging() -> None:
    """Nastav signal handlery a uklid PID souboru pri ukonceni."""
    def handle_signal(signum: int, _: object) -> None:
        """Preved procesni signal na citelny chybovy stav refresh behu."""
        name = signal.Signals(signum).name
        _set_error(f"IMDb refresh prerusen signalem {name}.")
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
        signal.signal(signum, handle_signal)

    atexit.register(lambda: IMDB_REFRESH_PID_PATH.unlink(missing_ok=True))


def _copy_stream(
    source: object,
    destination: object,
    *,
    total_bytes: int | None,
    progress: Callable[..., None] | None,
) -> None:
    """Prekopiruj stream po blocich a hlas prubeh nejvyse jednou za 5 sekund."""
    import time

    processed_bytes = 0
    last_report_at = 0.0
    while True:
        chunk = source.read(1024 * 1024)  # type: ignore[attr-defined]
        if not chunk:
            break
        destination.write(chunk)  # type: ignore[attr-defined]
        processed_bytes += len(chunk)
        now = time.monotonic()
        if progress is not None and (
            now - last_report_at >= 5.0 or (total_bytes is not None and processed_bytes >= total_bytes)
        ):
            progress(
                current_file_bytes=processed_bytes,
                current_file_total_bytes=total_bytes,
                current_file_percent=(
                    round(processed_bytes / total_bytes * 100, 1) if total_bytes else None
                ),
            )
            last_report_at = now


def _download_file(
    url: str,
    destination: Path,
    progress: Callable[..., None] | None = None,
) -> None:
    """Stahni jeden IMDb archiv do pracovniho adresare."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(url, timeout=120, context=ssl_context) as response, destination.open("wb") as handle:
        content_length = response.headers.get("Content-Length")
        total_bytes = int(content_length) if content_length and content_length.isdigit() else None
        _copy_stream(response, handle, total_bytes=total_bytes, progress=progress)


def _extract_gzip(
    source: Path,
    destination: Path,
    progress: Callable[..., None] | None = None,
) -> None:
    """Rozbal jeden `.gz` archiv do TSV souboru."""
    with gzip.open(source, "rb") as compressed, destination.open("wb") as extracted:
        _copy_stream(compressed, extracted, total_bytes=None, progress=progress)


def _validate_extracted_files(extracted_dir: Path) -> None:
    """Over, ze vsechny povinne IMDb TSV soubory existuji a nejsou prazdne."""
    missing: list[str] = []
    empty: list[str] = []
    for gz_name in IMDB_DATASET_FILES:
        tsv_name = gz_name.removesuffix(".gz")
        path = extracted_dir / tsv_name
        if not path.exists():
            missing.append(tsv_name)
            continue
        if path.stat().st_size <= 0:
            empty.append(tsv_name)
    if missing or empty:
        parts = []
        if missing:
            parts.append(f"chybi: {', '.join(missing)}")
        if empty:
            parts.append(f"prazdne: {', '.join(empty)}")
        raise RuntimeError("Neplatne rozbalene IMDb dumpy - " + "; ".join(parts))


def _swap_imdb_directory(extracted_dir: Path) -> Path | None:
    """Prohod aktivni IMDb adresar za nove rozbalenou verzi a vrat rollback adresar."""
    backup_dir: Path | None = None
    if IMDB_DIR.exists():
        backup_dir = IMDB_REFRESH_DIR / f"rollback-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        IMDB_DIR.rename(backup_dir)

    extracted_dir.rename(IMDB_DIR)
    return backup_dir


def main() -> int:
    """Proved cely refresh IMDb dumpu vcetne obnovy PostgreSQL katalogu."""
    _install_lifecycle_logging()
    started_at = _now_ts()
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    work_root = IMDB_REFRESH_DIR / run_id
    download_dir = work_root / "download"
    extracted_dir = work_root / "extracted"
    total_files = len(IMDB_DATASET_FILES)
    downloaded = 0
    extracted = 0

    work_root.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir.mkdir(parents=True, exist_ok=True)

    _write_status(
        state="running",
        stage="download",
        message="IMDb refresh bezi.",
        started_at=_now_iso(),
        finished_at=None,
        current_file=None,
        current_file_bytes=None,
        current_file_total_bytes=None,
        current_file_percent=None,
        last_activity_at=_now_iso(),
        files_total=total_files,
        files_downloaded=0,
        files_extracted=0,
        stats=None,
        error=None,
    )
    _emit({"phase": "start", "run_id": run_id, "files_total": total_files})

    last_heartbeat_at = 0.0

    def _update_progress(*, stage: str, message: str, current_file: str | None, **metrics: object) -> None:
        """Uloz prubeh a zapis heartbeat do logu nejvyse jednou za 30 sekund."""
        nonlocal last_heartbeat_at
        _write_status(
            stage=stage,
            message=message,
            current_file=current_file,
            last_activity_at=_now_iso(),
            **metrics,
        )
        now = _now_ts()
        if now - last_heartbeat_at >= 30.0:
            _emit(
                {
                    "phase": f"{stage}_heartbeat",
                    "message": message,
                    "current_file": current_file,
                    **metrics,
                }
            )
            last_heartbeat_at = now

    try:
        for index, gz_name in enumerate(IMDB_DATASET_FILES, start=1):
            url = f"{IMDB_DATASET_BASE_URL}/{gz_name}"
            download_path = download_dir / gz_name
            _write_status(
                stage="download",
                message=f"Stahuji {gz_name}.",
                current_file=gz_name,
                files_downloaded=downloaded,
                files_extracted=extracted,
                current_file_bytes=0,
                current_file_total_bytes=None,
                current_file_percent=0,
            )
            _emit({"phase": "download_start", "file": gz_name, "index": index, "url": url})
            _download_file(
                url,
                download_path,
                progress=lambda **metrics: _update_progress(
                    stage="download",
                    message=f"Stahuji {gz_name}.",
                    current_file=gz_name,
                    **metrics,
                ),
            )
            downloaded += 1
            _emit({"phase": "download_done", "file": gz_name, "index": index, "size": download_path.stat().st_size})
            _write_status(files_downloaded=downloaded)

            tsv_name = gz_name.removesuffix(".gz")
            extracted_path = extracted_dir / tsv_name
            _write_status(
                stage="extract",
                message=f"Rozbaluji {gz_name}.",
                current_file=tsv_name,
                files_downloaded=downloaded,
                files_extracted=extracted,
                current_file_bytes=0,
                current_file_total_bytes=None,
                current_file_percent=None,
            )
            _emit({"phase": "extract_start", "file": gz_name, "target": tsv_name, "index": index})
            _extract_gzip(
                download_path,
                extracted_path,
                progress=lambda **metrics: _update_progress(
                    stage="extract",
                    message=f"Rozbaluji {gz_name}.",
                    current_file=tsv_name,
                    **metrics,
                ),
            )
            extracted += 1
            _emit({"phase": "extract_done", "file": tsv_name, "index": index, "size": extracted_path.stat().st_size})
            _write_status(files_extracted=extracted)

        _write_status(stage="validate", message="Kontroluji rozbalene IMDb dumpy.", current_file=None)
        _validate_extracted_files(extracted_dir)
        _emit({"phase": "validate_done", "files_total": total_files})

        _write_status(stage="swap", message="Prepinam aktivni IMDb dumpy.", current_file=None)
        backup_dir = _swap_imdb_directory(extracted_dir)
        _emit({"phase": "swap_done", "imdb_dir": IMDB_DIR.as_posix()})

        try:
            _write_status(stage="refresh_catalog", message="Obnovuji katalog v PostgreSQL.", current_file=None)

            def _progress(**payload: object) -> None:
                """Propis prubezny stav rebuild kroku do statusu i stdout logu."""
                _update_progress(stage="refresh_catalog", **payload)

            stats = rebuild_catalog_from_current_imdb(force=True, progress=_progress)
            _emit({"phase": "refresh_catalog_done", "stats": stats})
        except Exception:
            if backup_dir is not None and backup_dir.exists():
                if IMDB_DIR.exists():
                    shutil.rmtree(IMDB_DIR)
                backup_dir.rename(IMDB_DIR)
                _emit({"phase": "rollback_done", "restored_dir": IMDB_DIR.as_posix()})
            raise

        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir)

        shutil.rmtree(work_root, ignore_errors=True)
        finished_at = _now_iso()
        _write_status(
            state="completed",
            stage="completed",
            message="IMDb refresh uspesne dokoncen.",
            current_file=None,
            finished_at=finished_at,
            stats=stats,
        )
        _emit({"phase": "done", "finished_at": finished_at, "stats": stats})
        return 0
    except Exception as exc:
        _set_error(f"IMDb refresh selhal: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
