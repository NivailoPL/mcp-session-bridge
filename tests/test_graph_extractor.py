from __future__ import annotations

import pytest

from app.graph_extractor import (
    GRAPH_EXTRACTION_OUTPUT_SCHEMA,
    GraphExtractionError,
    validate_extraction_result,
)


def test_extraction_result_requires_only_concept_fields_and_adds_empty_evidence() -> None:
    result = {
        "concepts": [
            {
                "canonical_name": "SQLite job queue",
                "type": "technology",
                "summary": "SQLite stores durable Graph jobs.",
            }
        ]
    }

    validated = validate_extraction_result(result, max_concepts=25)

    assert validated["concepts"] == [{
        "canonical_name": "SQLite job queue",
        "type": "technology",
        "summary": "SQLite stores durable Graph jobs.",
        "evidence": [],
    }]
    concept_schema = GRAPH_EXTRACTION_OUTPUT_SCHEMA["properties"]["concepts"]["items"]
    assert concept_schema["required"] == ["canonical_name", "type", "summary"]
    assert "evidence" not in concept_schema["properties"]


def test_extraction_result_keeps_first_duplicate_canonical_name() -> None:
    result = {"concepts": [
        {"canonical_name": "SQLite", "type": "technology", "summary": "First."},
        {"canonical_name": "sqlite", "type": "topic", "summary": "Duplicate."},
    ]}

    validated = validate_extraction_result(result, max_concepts=25)

    assert [concept["canonical_name"] for concept in validated["concepts"]] == ["SQLite"]


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        ({"concepts": []}, "concepts_empty"),
        (
            {"concepts": [{"canonical_name": "X", "type": "unknown", "summary": "S"}]},
            "concept_type_unsupported",
        ),
        (
            {"concepts": [{"canonical_name": "X", "type": "topic", "summary": "S", "extra": "no"}]},
            "concept_schema_invalid",
        ),
    ],
)
def test_invalid_extraction_result_has_stable_terminal_reason(result, expected_code) -> None:
    with pytest.raises(GraphExtractionError) as caught:
        validate_extraction_result(result, max_concepts=25)
    assert caught.value.code == expected_code
    assert caught.value.retryable is False
