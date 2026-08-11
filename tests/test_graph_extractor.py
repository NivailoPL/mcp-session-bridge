from __future__ import annotations

import pytest

from app.graph_extractor import GraphExtractionError, validate_extraction_result


def test_extraction_result_requires_literal_evidence_from_named_exchange() -> None:
    source = {12: "USER:\nWe use SQLite for the durable job queue.\n\nMODEL:\nThat keeps retries local."}
    result = {
        "concepts": [
            {
                "canonical_name": "SQLite job queue",
                "type": "technology",
                "summary": "SQLite stores durable Graph jobs.",
                "evidence": [{"exchange_id": 12, "quote": "SQLite for the durable job queue"}],
            }
        ]
    }

    validated = validate_extraction_result(result, source, max_concepts=25)

    assert validated["concepts"][0]["canonical_name"] == "SQLite job queue"


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        ({"concepts": []}, "concepts_empty"),
        (
            {"concepts": [{"canonical_name": "X", "type": "unknown", "summary": "S", "evidence": []}]},
            "concept_type_unsupported",
        ),
        (
            {"concepts": [{"canonical_name": "X", "type": "topic", "summary": "S", "evidence": [{"exchange_id": 99, "quote": "missing"}]}]},
            "evidence_exchange_unknown",
        ),
        (
            {"concepts": [{"canonical_name": "X", "type": "topic", "summary": "S", "evidence": [{"exchange_id": 12, "quote": "not literal"}]}]},
            "evidence_quote_not_literal",
        ),
    ],
)
def test_invalid_extraction_result_has_stable_retryable_reason(result, expected_code) -> None:
    with pytest.raises(GraphExtractionError) as caught:
        validate_extraction_result(result, {12: "literal source"}, max_concepts=25)
    assert caught.value.code == expected_code
    assert caught.value.retryable is True

