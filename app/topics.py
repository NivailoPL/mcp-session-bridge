"""Topics: a cross-session picture of what the owner writes about.

A topic map is an immutable list of topics plus one built-in pool for chapters
that fit no topic. Jev (a decisions model on OpenRouter) pins each accepted
chapter to one topic of the active map. Topics never filter or judge anything;
they only describe. Every step runs from an explicit admin request.
"""

from __future__ import annotations

import re
from typing import Any

POOL_KEY = "unsorted"
POOL_NAME = "Unsorted"
POOL_DESCRIPTION = "Do ułożenia: rozmowa nie pasuje do żadnego z tematów opisanych wyżej."
POOL_COLOR = "#4b5563"

TOPIC_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
TOPIC_NAME_MAX_CHARS = 60
TOPIC_DESCRIPTION_MAX_CHARS = 600
TOPIC_FAMILY_MAX_CHARS = 40
TOPIC_MAX_COUNT = 60
TOPIC_NOTE_MAX_CHARS = 500
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_TOPIC_COLOR = "#7a7df0"

TOPIC_SETTINGS_KEY = "topics.settings"
TOPIC_OPENROUTER_KEY_SETTING = "topics.openrouter.api_key"
TOPIC_EFFORTS = ("low", "medium", "high", "xhigh")
TOPIC_MODEL_MAX_CHARS = 96

JEV_INSTRUCTIONS = (
    "This is one chapter of a personal conversation between a human and AI models, "
    "saved in a conversation archive. Which topic does this chapter belong to?"
)
# Observed in the October 2026 experiments: only input is billed.
JEV_DEFAULT_COST_PER_CHAPTER = 0.00008

RUN_KINDS = ("classify", "test", "find", "refine")
RUN_STATUSES = ("queued", "running", "paused", "completed", "failed")
SCOPE_MODES = ("unclassified", "all", "topic")
JOURNAL_ACTORS = ("owner", "jev", "codex", "system")


class TopicError(ValueError):
    def __init__(self, message: str, *, code: str = "topic_invalid"):
        super().__init__(message)
        self.code = code


def default_topic_settings() -> dict[str, Any]:
    return {
        "jev_model": "typesafe/jev-1.14",
        "codex_model": "gpt-5.6-sol",
        "codex_effort": "medium",
        "include_sensitive": False,
        "weak_threshold": 0.0,
        "pool_hint": 15,
        "codex_timeout_minutes": 10,
    }


def validate_topic_settings(payload: Any, current: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TopicError("Settings must be an object.", code="settings_invalid")
    merged = {**(current or default_topic_settings()), **payload}
    unknown = set(merged) - set(default_topic_settings())
    if unknown:
        raise TopicError(f"Unknown settings: {', '.join(sorted(unknown))}.", code="settings_invalid")
    for key in ("jev_model", "codex_model"):
        value = merged[key]
        if not isinstance(value, str) or not value.strip() or len(value) > TOPIC_MODEL_MAX_CHARS:
            raise TopicError(f"{key} must be a model name.", code="settings_invalid")
        merged[key] = value.strip()
    if merged["codex_effort"] not in TOPIC_EFFORTS:
        raise TopicError("codex_effort must be one of low, medium, high, xhigh.", code="settings_invalid")
    if not isinstance(merged["include_sensitive"], bool):
        raise TopicError("include_sensitive must be true or false.", code="settings_invalid")
    threshold = merged["weak_threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold < 1:
        raise TopicError("weak_threshold must be between 0 and 1 (0 turns it off).", code="settings_invalid")
    merged["weak_threshold"] = float(threshold)
    minutes = merged["codex_timeout_minutes"]
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 2 <= minutes <= 30:
        raise TopicError("The proposing time limit is a whole number of minutes from 2 to 30.", code="settings_invalid")
    hint = merged["pool_hint"]
    if isinstance(hint, bool) or not isinstance(hint, int) or not 1 <= hint <= 500:
        raise TopicError("pool_hint must be a whole number from 1 to 500.", code="settings_invalid")
    return merged


def validate_topic_map(payload: Any) -> list[dict[str, str]]:
    """Normalize a map to a list of topics with the pool last.

    Accepts a list of topic objects, or the experiment shape ``{key: description}``.
    The pool is built in: a ``unsorted`` entry in the payload is ignored.
    """
    if isinstance(payload, dict):
        items = [{"key": key, "description": value} for key, value in payload.items()]
    elif isinstance(payload, list):
        items = payload
    else:
        raise TopicError("A map is a list of topics.", code="map_invalid")
    topics: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise TopicError("Every topic must be an object.", code="map_invalid")
        key = item.get("key")
        if key == POOL_KEY:
            continue
        if not isinstance(key, str) or not TOPIC_KEY_PATTERN.match(key):
            raise TopicError(
                f"Topic key {key!r} must be snake_case: a letter, then letters, digits or _.", code="map_invalid"
            )
        if key in seen:
            raise TopicError(f"Topic key {key} appears twice.", code="map_invalid")
        seen.add(key)
        description = _text(item.get("description"), "description", key, TOPIC_DESCRIPTION_MAX_CHARS)
        name = item.get("name")
        name = _text(name, "name", key, TOPIC_NAME_MAX_CHARS) if name not in (None, "") else _name_from_key(key)
        family = item.get("family")
        family = _text(family, "family", key, TOPIC_FAMILY_MAX_CHARS) if family not in (None, "") else "Other"
        color = item.get("color") or DEFAULT_TOPIC_COLOR
        if not isinstance(color, str) or not COLOR_PATTERN.match(color):
            raise TopicError(f"Topic {key}: color must look like #7a7df0.", code="map_invalid")
        topics.append({"key": key, "name": name, "description": description, "family": family, "color": color.lower()})
    if not topics:
        raise TopicError("A map needs at least one topic besides the pool.", code="map_invalid")
    if len(topics) > TOPIC_MAX_COUNT:
        raise TopicError(f"A map can hold at most {TOPIC_MAX_COUNT} topics.", code="map_invalid")
    topics.append(pool_topic())
    return topics


def pool_topic() -> dict[str, str]:
    return {"key": POOL_KEY, "name": POOL_NAME, "description": POOL_DESCRIPTION, "family": "Other", "color": POOL_COLOR}


def jev_criteria(topics: list[dict[str, str]]) -> dict[str, str]:
    """Choice criteria for Jev, in map order with the pool last."""
    ordered = [topic for topic in topics if topic["key"] != POOL_KEY] + [pool_topic()]
    return {topic["key"]: topic["description"] for topic in ordered}


def jev_state(chapter: dict[str, Any]) -> dict[str, str]:
    """What Jev sees about one chapter: titles and the group name, never transcript text."""
    return {
        "chapter_title": chapter["title"],
        "session_title": chapter["session_title"],
        "archive_group": chapter["group_name"],
    }


def validate_scope(payload: Any, topic_keys: set[str]) -> dict[str, Any]:
    if payload is None:
        payload = {"mode": "unclassified"}
    if not isinstance(payload, dict) or payload.get("mode") not in SCOPE_MODES:
        raise TopicError("Scope mode must be unclassified, all or topic.", code="scope_invalid")
    if payload["mode"] == "topic":
        key = payload.get("topic_key")
        if key not in topic_keys:
            raise TopicError("Choose a topic of the active map.", code="scope_invalid")
        return {"mode": "topic", "topic_key": key}
    return {"mode": payload["mode"]}


def validate_note(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > TOPIC_NOTE_MAX_CHARS:
        raise TopicError(f"A note is text of at most {TOPIC_NOTE_MAX_CHARS} characters.", code="note_invalid")
    return value.strip()


def _text(value: Any, field: str, key: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TopicError(f"Topic {key}: {field} is required.", code="map_invalid")
    value = value.strip()
    if len(value) > limit:
        raise TopicError(f"Topic {key}: {field} is longer than {limit} characters.", code="map_invalid")
    return value


def _name_from_key(key: str) -> str:
    return key.replace("_", " ").capitalize()
