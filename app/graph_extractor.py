from __future__ import annotations

from typing import Any


GRAPH_CONCEPT_TYPES = {
    "person",
    "project",
    "organization",
    "technology",
    "place",
    "topic",
    "decision",
    "other",
}

GRAPH_EXTRACTION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["concepts"],
    "properties": {
        "concepts": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["canonical_name", "type", "summary"],
                "properties": {
                    "canonical_name": {"type": "string", "minLength": 1, "maxLength": 160},
                    "type": {"type": "string", "enum": sorted(GRAPH_CONCEPT_TYPES)},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 1_000},
                },
            },
        }
    },
}


class GraphExtractionError(ValueError):
    """A stable, operator-visible reason production Graph data was rejected."""

    def __init__(self, message: str, *, code: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def validate_extraction_result(
    result: Any,
    *,
    max_concepts: int,
) -> dict[str, Any]:
    if not isinstance(result, dict) or set(result) != {"concepts"}:
        raise GraphExtractionError(
            "Extraction output must contain only concepts.", code="output_shape_invalid"
        )
    concepts = result.get("concepts")
    if not isinstance(concepts, list) or not concepts:
        raise GraphExtractionError(
            "Extraction output must contain at least one concept.", code="concepts_empty"
        )
    if len(concepts) > max_concepts:
        raise GraphExtractionError(
            "Extraction output exceeds the configured concept limit.",
            code="concept_limit_exceeded",
        )

    validated: list[dict[str, Any]] = []
    names: set[str] = set()
    for concept in concepts:
        if not isinstance(concept, dict) or set(concept) != {
            "canonical_name",
            "type",
            "summary",
        }:
            raise GraphExtractionError(
                "Each concept must match the production schema.", code="concept_schema_invalid"
            )
        name = _bounded_string(concept["canonical_name"], "canonical_name", 160)
        folded = name.casefold()
        if folded in names:
            continue
        names.add(folded)
        concept_type = _bounded_string(concept["type"], "type", 40).lower()
        if concept_type not in GRAPH_CONCEPT_TYPES:
            raise GraphExtractionError(
                "Extraction output contains an unsupported concept type.",
                code="concept_type_unsupported",
            )
        summary = _bounded_string(concept["summary"], "summary", 1_000)
        validated.append(
            {
                "canonical_name": name,
                "type": concept_type,
                "summary": summary,
                "evidence": [],
            }
        )
    return {"concepts": validated}


def _bounded_string(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GraphExtractionError(
            f"{field} must be a non-empty string.", code=f"{field}_empty"
        )
    normalized = value.strip()
    if len(normalized) > maximum:
        raise GraphExtractionError(
            f"{field} is too long.", code=f"{field}_too_long"
        )
    return normalized
