from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from filmy.ai_recommendations import (
    delete_ai_recommendation_file,
    import_ai_recommendations_file,
    normalize_ai_recommendation_payload,
)


def _payload() -> dict:
    return {
        "contract_version": 1,
        "created_at": "2026-07-19T13:00:00+02:00",
        "intent": "recommendation_batch",
        "status": "draft_for_review",
        "source_inputs": ["../filmy_input/context.json"],
        "method_notes": [],
        "deprioritized_candidates": [],
        "notes": None,
        "recommendations": [
            {
                "title": "Enemy",
                "year": 2013,
                "imdb_id": "tt2316411",
                "tmdb_id": 181886,
                "media_type": "movie",
                "confidence": "high",
                "status": "candidate_from_existing_library",
                "priority": 1,
                "fit_reasons": ["Jake Gyllenhaal affinity."],
                "risk_reasons": ["Cerebral and stylized."],
                "source_signal_refs": [],
                "notes": "Compact psychological thriller.",
            }
        ],
    }


def test_normalize_ai_recommendation_payload_keeps_stable_shape() -> None:
    normalized = normalize_ai_recommendation_payload(_payload())

    assert normalized["contract_version"] == 1
    assert normalized["recommendations"][0]["imdb_id"] == "tt2316411"
    assert normalized["recommendations"][0]["priority"] == 1
    assert normalized["recommendations"][0]["fit_reasons"] == ["Jake Gyllenhaal affinity."]


def test_normalize_ai_recommendation_payload_rejects_missing_required_field() -> None:
    payload = _payload()
    del payload["recommendations"][0]["source_signal_refs"]

    with pytest.raises(ValueError, match="source_signal_refs"):
        normalize_ai_recommendation_payload(payload)


def test_import_ai_recommendations_file_calls_runtime_import(tmp_path) -> None:
    path = tmp_path / "recommendations.json"
    path.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")

    with patch(
        "filmy.ai_recommendations.import_ai_recommendation_batch",
        return_value={"recommendations": 1, "resolved": 1},
    ) as import_mock:
        result = import_ai_recommendations_file(path)

    assert result == {"recommendations": 1, "resolved": 1}
    kwargs = import_mock.call_args.kwargs
    assert kwargs["source_filename"] == "recommendations.json"
    assert kwargs["contract_version"] == 1
    assert kwargs["recommendations"][0]["title"] == "Enemy"
    assert kwargs["recommendations"][0]["imdb_id"] == "tt2316411"


def test_delete_ai_recommendation_file_deletes_only_named_json(tmp_path) -> None:
    path = tmp_path / "recommendations.json"
    path.write_text("{}", encoding="utf-8")

    result = delete_ai_recommendation_file("recommendations.json", directory=tmp_path)

    assert result["deleted"] is True
    assert result["filename"] == "recommendations.json"
    assert not path.exists()


def test_delete_ai_recommendation_file_rejects_path_traversal(tmp_path) -> None:
    with pytest.raises(ValueError, match="Neplatny nazev"):
        delete_ai_recommendation_file("../recommendations.json", directory=tmp_path)
