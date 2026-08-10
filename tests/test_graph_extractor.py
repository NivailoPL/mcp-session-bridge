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
    "result",
    [
        {"concepts": []},
        {"concepts": [{"canonical_name": "X", "type": "unknown", "summary": "S", "evidence": []}]},
        {"concepts": [{"canonical_name": "X", "type": "topic", "summary": "S", "evidence": [{"exchange_id": 99, "quote": "missing"}]}]},
        {"concepts": [{"canonical_name": "X", "type": "topic", "summary": "S", "evidence": [{"exchange_id": 12, "quote": "not literal"}]}]},
    ],
)
def test_invalid_extraction_result_is_rejected(result) -> None:
    with pytest.raises(GraphExtractionError):
        validate_extraction_result(result, {12: "literal source"}, max_concepts=25)

