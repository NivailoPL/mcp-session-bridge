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
                "required": ["canonical_name", "type", "summary", "evidence"],
                "properties": {
                    "canonical_name": {"type": "string", "minLength": 1, "maxLength": 160},
                    "type": {"type": "string", "enum": sorted(GRAPH_CONCEPT_TYPES)},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 1_000},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["exchange_id", "quote"],
                            "properties": {
                                "exchange_id": {"type": "integer", "minimum": 1},
                                "quote": {"type": "string", "minLength": 1, "maxLength": 2_000},
                            },
                        },
                    },
                },
            },
        }
    },
}


class GraphExtractionError(ValueError):
    """A stable, operator-visible reason production Graph data was rejected."""

    def __init__(self, message: str, *, code: str, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def validate_extraction_result(
    result: Any,
    source_by_exchange: dict[int, str],
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
            "evidence",
        }:
            raise GraphExtractionError(
                "Each concept must match the production schema.", code="concept_schema_invalid"
            )
        name = _bounded_string(concept["canonical_name"], "canonical_name", 160)
        folded = name.casefold()
        if folded in names:
            raise GraphExtractionError(
                "Extraction output contains duplicate canonical names.",
                code="concept_name_duplicate",
            )
        names.add(folded)
        concept_type = _bounded_string(concept["type"], "type", 40).lower()
        if concept_type not in GRAPH_CONCEPT_TYPES:
            raise GraphExtractionError(
                "Extraction output contains an unsupported concept type.",
                code="concept_type_unsupported",
            )
        summary = _bounded_string(concept["summary"], "summary", 1_000)
        evidence = concept["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise GraphExtractionError(
                "Every concept must include evidence.", code="concept_evidence_empty"
            )
        validated_evidence: list[dict[str, Any]] = []
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"exchange_id", "quote"}:
                raise GraphExtractionError(
                    "Evidence must contain exchange_id and quote.", code="evidence_schema_invalid"
                )
            exchange_id = item["exchange_id"]
            if isinstance(exchange_id, bool) or not isinstance(exchange_id, int):
                raise GraphExtractionError(
                    "Evidence exchange_id must be an integer.",
                    code="evidence_exchange_id_invalid",
                )
            source = source_by_exchange.get(exchange_id)
            quote = _bounded_string(item["quote"], "quote", 2_000)
            if source is None:
                raise GraphExtractionError(
                    "Evidence references an exchange outside the source transcript.",
                    code="evidence_exchange_unknown",
                )
            if quote not in source:
                raise GraphExtractionError(
                    "Evidence quote must appear literally in its source exchange.",
                    code="evidence_quote_not_literal",
                )
            validated_evidence.append({"exchange_id": exchange_id, "quote": quote})
        validated.append(
            {
                "canonical_name": name,
                "type": concept_type,
                "summary": summary,
                "evidence": validated_evidence,
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

