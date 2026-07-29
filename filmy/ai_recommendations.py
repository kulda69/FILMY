"""Vyklad a formatovani AI doporucovacich vystupu pro FILMY."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any
import uuid

from filmy.paths import PROJECT_ROOT
from filmy.runtime_postgres import fetch_ai_recommendation_run_checksums, import_ai_recommendation_batch


REQUIRED_BATCH_FIELDS = {
    "contract_version",
    "created_at",
    "intent",
    "status",
    "source_inputs",
    "method_notes",
    "deprioritized_candidates",
    "notes",
    "recommendations",
}

REQUIRED_RECOMMENDATION_FIELDS = {
    "title",
    "year",
    "imdb_id",
    "tmdb_id",
    "media_type",
    "confidence",
    "fit_reasons",
    "risk_reasons",
    "source_signal_refs",
    "status",
    "notes",
}


def list_ai_recommendation_files(directory: str | Path | None = None) -> list[dict[str, Any]]:
    """List stable recommendation JSON files available for local import."""

    source_dir = Path(directory).expanduser().resolve() if directory else PROJECT_ROOT / "filmy_output"
    imported_by_checksum = fetch_ai_recommendation_run_checksums()
    if not source_dir.exists():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(source_dir.glob("*.json"), key=lambda item: item.name):
        try:
            raw_text = path.read_text(encoding="utf-8")
            payload = json.loads(raw_text)
            normalized = normalize_ai_recommendation_payload(payload)
            checksum = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            imported = imported_by_checksum.get(checksum)
            files.append(
                {
                    "filename": path.name,
                    "path": path.as_posix(),
                    "contract_version": normalized["contract_version"],
                    "created_at": normalized["created_at"],
                    "intent": normalized["intent"],
                    "status": normalized["status"],
                    "recommendation_count": len(normalized["recommendations"]),
                    "checksum": checksum,
                    "imported": imported is not None,
                    "imported_run_id": imported.get("run_id") if imported else None,
                    "imported_at": imported.get("imported_at") if imported else None,
                    "error": None,
                }
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            files.append(
                {
                    "filename": path.name,
                    "path": path.as_posix(),
                    "contract_version": None,
                    "created_at": None,
                    "intent": None,
                    "status": None,
                    "recommendation_count": None,
                    "checksum": None,
                    "imported": False,
                    "imported_run_id": None,
                    "imported_at": None,
                    "error": str(exc),
                }
            )
    return files


def import_ai_recommendations_file(path: str | Path) -> dict[str, Any]:
    """Import one stable recommendation JSON produced by the external AI project."""

    source_path = Path(path).expanduser().resolve()
    raw_text = source_path.read_text(encoding="utf-8")
    payload = json.loads(raw_text)
    normalized = normalize_ai_recommendation_payload(payload)
    checksum = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    imported_at = datetime.now(UTC).isoformat()

    return import_ai_recommendation_batch(
        run_id=f"ai-rec-run-{uuid.uuid4()}",
        source_path=source_path.as_posix(),
        source_filename=source_path.name,
        source_checksum=checksum,
        contract_version=normalized["contract_version"],
        intent=normalized["intent"],
        status=normalized["status"],
        payload_created_at=normalized["created_at"],
        imported_at=imported_at,
        source_inputs_json=json.dumps(normalized["source_inputs"], ensure_ascii=False),
        method_notes_json=json.dumps(normalized["method_notes"], ensure_ascii=False),
        deprioritized_candidates_json=json.dumps(
            normalized["deprioritized_candidates"],
            ensure_ascii=False,
        ),
        notes=normalized["notes"],
        raw_json=json.dumps(normalized, ensure_ascii=False, sort_keys=True),
        recommendations=normalized["recommendations"],
    )


def delete_ai_recommendation_file(filename: str, directory: str | Path | None = None) -> dict[str, Any]:
    """Delete one recommendation JSON from the stable filmy_output directory."""

    cleaned_filename = Path(str(filename or "").strip()).name
    if not cleaned_filename or cleaned_filename != str(filename or "").strip():
        raise ValueError("Neplatny nazev souboru.")
    source_dir = Path(directory).expanduser().resolve() if directory else PROJECT_ROOT / "filmy_output"
    target_path = (source_dir / cleaned_filename).resolve()
    if target_path.parent != source_dir.resolve() or target_path.suffix.lower() != ".json":
        raise ValueError("Mazat lze jen JSON soubory primo z filmy_output.")
    if not target_path.exists():
        raise ValueError("Soubor nebyl nalezen.")
    target_path.unlink()
    return {
        "deleted": True,
        "filename": cleaned_filename,
        "path": target_path.as_posix(),
    }


def normalize_ai_recommendation_payload(payload: Any) -> dict[str, Any]:
    """Validate and normalize the current stable AI recommendation output shape."""

    if not isinstance(payload, dict):
        raise ValueError("AI recommendation JSON must be an object.")
    missing = sorted(REQUIRED_BATCH_FIELDS - payload.keys())
    if missing:
        raise ValueError(f"AI recommendation JSON is missing fields: {', '.join(missing)}")
    recommendations = payload["recommendations"]
    if not isinstance(recommendations, list):
        raise ValueError("AI recommendation field `recommendations` must be a list.")

    normalized_recommendations = [
        _normalize_recommendation(item, index)
        for index, item in enumerate(recommendations, start=1)
    ]
    return {
        "contract_version": _require_int(payload["contract_version"], "contract_version"),
        "created_at": _optional_text(payload.get("created_at")),
        "intent": _require_text(payload["intent"], "intent"),
        "status": _require_text(payload["status"], "status"),
        "source_inputs": _require_list(payload["source_inputs"], "source_inputs"),
        "method_notes": _require_list(payload["method_notes"], "method_notes"),
        "deprioritized_candidates": _require_list(
            payload["deprioritized_candidates"],
            "deprioritized_candidates",
        ),
        "notes": _optional_text(payload.get("notes")),
        "recommendations": normalized_recommendations,
    }


def _normalize_recommendation(item: Any, index: int) -> dict[str, Any]:
    """Validuj a normalizuj jeden recommendation objekt z AI JSON batchu."""

    if not isinstance(item, dict):
        raise ValueError(f"Recommendation #{index} must be an object.")
    missing = sorted(REQUIRED_RECOMMENDATION_FIELDS - item.keys())
    if missing:
        raise ValueError(f"Recommendation #{index} is missing fields: {', '.join(missing)}")
    return {
        "title": _require_text(item["title"], f"recommendations[{index}].title"),
        "year": _optional_int(item.get("year"), f"recommendations[{index}].year"),
        "imdb_id": _optional_imdb_id(item.get("imdb_id"), index),
        "tmdb_id": _optional_int(item.get("tmdb_id"), f"recommendations[{index}].tmdb_id"),
        "media_type": _optional_text(item.get("media_type")),
        "confidence": _optional_text(item.get("confidence")),
        "status": _optional_text(item.get("status")),
        "priority": _optional_int(item.get("priority"), f"recommendations[{index}].priority"),
        "fit_reasons": _require_list(item["fit_reasons"], f"recommendations[{index}].fit_reasons"),
        "risk_reasons": _require_list(item["risk_reasons"], f"recommendations[{index}].risk_reasons"),
        "source_signal_refs": _require_list(
            item["source_signal_refs"],
            f"recommendations[{index}].source_signal_refs",
        ),
        "notes": _optional_text(item.get("notes")),
    }


def _require_text(value: Any, field: str) -> str:
    """Vynut neprazdny textovy retezec pro povinne pole."""

    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"Field `{field}` must be a non-empty string.")
    return text


def _optional_text(value: Any) -> str | None:
    """Normalizuj volitelny text na orezany retezec nebo `None`."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_int(value: Any, field: str) -> int:
    """Vynut celociselnou hodnotu pro povinne pole."""

    if isinstance(value, bool):
        raise ValueError(f"Field `{field}` must be an integer.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Field `{field}` must be an integer.") from exc


def _optional_int(value: Any, field: str) -> int | None:
    """Normalizuj volitelne cislo pres stejna pravidla jako povinne cele cislo."""

    if value is None:
        return None
    return _require_int(value, field)


def _optional_imdb_id(value: Any, index: int) -> str | None:
    """Validuj volitelne IMDb ID ve tvaru `tt1234567`."""

    text = _optional_text(value)
    if text is None:
        return None
    if not text.startswith("tt") or not text[2:].isdigit():
        raise ValueError(f"Recommendation #{index} has invalid imdb_id `{text}`.")
    return text


def _require_list(value: Any, field: str) -> list[Any]:
    """Vynut seznamovou hodnotu pro pole, ktera musi zustat listem."""

    if not isinstance(value, list):
        raise ValueError(f"Field `{field}` must be a list.")
    return value
