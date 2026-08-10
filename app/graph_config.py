from __future__ import annotations

import re
from typing import Any


GRAPH_PROVIDER_CODEX = "codex"
GRAPH_PROVIDERS = {GRAPH_PROVIDER_CODEX}
GRAPH_EFFORTS = {"low", "medium", "high", "xhigh"}
GRAPH_INACTIVITY_HOURS = {1, 3, 6, 24}
GRAPH_SCHEMA_VERSION = "concept-extraction/v1"
GRAPH_MODEL_MAX_CHARS = 96
GRAPH_PROMPT_MAX_CHARS = 24_000
GRAPH_MAX_CONCEPTS_MIN = 1
GRAPH_MAX_CONCEPTS_MAX = 200

DEFAULT_GRAPH_PROMPT = """Extract durable concepts from this session for a personal knowledge graph.

Prefer specific people, projects, organizations, tools, places, decisions, and recurring topics. Use concise canonical names. Every concept must include literal evidence from the supplied session. Do not infer sensitive facts that are not explicitly present."""


class GraphConfigError(ValueError):
    """Raised when a Graph production policy would be invalid or misleading."""


def validate_graph_draft(payload: dict[str, Any]) -> dict[str, Any]:
    provider = _required_string(payload, "provider").lower()
    if provider == "api":
        raise GraphConfigError("API provider is not available yet.")
    if provider not in GRAPH_PROVIDERS:
        raise GraphConfigError("provider must be codex.")

    model = _required_string(payload, "model")
    if len(model) > GRAPH_MODEL_MAX_CHARS or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise GraphConfigError("model must be a valid model identifier.")

    effort = _required_string(payload, "effort").lower()
    if effort not in GRAPH_EFFORTS:
        raise GraphConfigError("effort must be low, medium, high, or xhigh.")

    prompt = _required_string(payload, "prompt")
    if len(prompt) > GRAPH_PROMPT_MAX_CHARS:
        raise GraphConfigError(f"prompt must be {GRAPH_PROMPT_MAX_CHARS} characters or fewer.")

    max_concepts = _required_int(payload, "max_concepts")
    if not GRAPH_MAX_CONCEPTS_MIN <= max_concepts <= GRAPH_MAX_CONCEPTS_MAX:
        raise GraphConfigError(
            f"max_concepts must be between {GRAPH_MAX_CONCEPTS_MIN} and {GRAPH_MAX_CONCEPTS_MAX}."
        )

    inactivity_hours = _required_int(payload, "inactivity_hours")
    if inactivity_hours not in GRAPH_INACTIVITY_HOURS:
        raise GraphConfigError("inactivity_hours must be 1, 3, 6, or 24.")

    include_sensitive = payload.get("include_sensitive")
    if not isinstance(include_sensitive, bool):
        raise GraphConfigError("include_sensitive must be a boolean.")

    return {
        "provider": provider,
        "model": model,
        "effort": effort,
        "prompt": prompt,
        "max_concepts": max_concepts,
        "inactivity_hours": inactivity_hours,
        "include_sensitive": include_sensitive,
    }


def default_graph_profile() -> dict[str, Any]:
    return {
        "provider": GRAPH_PROVIDER_CODEX,
        "model": "gpt-5.6-sol",
        "effort": "medium",
        "prompt": DEFAULT_GRAPH_PROMPT,
        "max_concepts": 25,
        "inactivity_hours": 24,
        "include_sensitive": False,
    }


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise GraphConfigError(f"{field} must be a non-empty string.")
    return value.strip()


def _required_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraphConfigError(f"{field} must be an integer.")
    return value

