"""Prompts, output schemas and result validation for Find topics and Refine topics.

The proposing model sees chapter titles, session titles and group names only.
It returns proposals; nothing it says changes the map until a proposal is
tested by the sorting model and accepted by the owner.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from app.topics import POOL_KEY, TOPIC_KEY_PATTERN, TopicError

DETAIL_LEVELS = {
    "broad": "Prefer fewer, broader topics (roughly 10–15 for an archive of this size).",
    "balanced": "Aim for topics a person would name on their own (roughly 15–25 for an archive of this size).",
    "fine": "Prefer more specific topics where the material supports them (roughly 25–40 for an archive of this size).",
}
MIN_TOPIC_CHAPTERS = 3
MIN_TOPIC_SESSIONS = 2
MIN_REFINE_CHAPTERS = 4
MAX_REFINE_OPERATIONS = 5
NOTE_MAX_CHARS = 1000

COMMON_RULES = f"""The topics are not a quality judgement; they are a picture of what this person writes about. Housekeeping chapters (starting a session, asking for a summary) are normal content: they get a topic only if they keep coming back.

Write every name and description in the language of the chapter titles. A description is read by a separate, simpler sorting model that sees only chapter titles, session titles and group names, so write it as a concrete list of recognisable things (people, places, tools, symptoms, projects), not as an abstract summary. A topic must not overlap another topic; when two topics could both claim a chapter, make their descriptions draw the line.

A topic needs at least {MIN_TOPIC_CHAPTERS} chapters from at least {MIN_TOPIC_SESSIONS} different sessions: one long conversation is not yet a recurring subject. Keys are snake_case English (letters, digits, underscore). Families group related topics (for example Work and money, Health, Inner life); use 4–8 families."""


def _chapter_lines(chapters: list[dict[str, Any]]) -> str:
    sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chapter in chapters:
        sessions[chapter["session_id"]].append(chapter)
    blocks = []
    for items in sessions.values():
        head = items[0]
        lines = [f'Session "{head["session_title"]}" (group: {head["group_name"]})']
        lines += [f'  - id {c["strand_id"]}: {c["title"]}' for c in items]
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _rejected_lines(rejected: list[dict[str, Any]]) -> str:
    if not rejected:
        return "None."
    return "\n".join(f"- {item['summary']}" + (f" (owner's note: {item['note']})" if item.get("note") else "") for item in rejected)


def _guidance(detail: str, note: str) -> str:
    text = DETAIL_LEVELS.get(detail, DETAIL_LEVELS["balanced"])
    if note:
        text += f"\nThe owner adds: {note}"
    return text


def _map_lines(topics: list[dict[str, str]]) -> str:
    return "\n".join(
        f"- {t['key']} | {t['name']} | family {t['family']}: {t['description']}" for t in topics if t["key"] != POOL_KEY
    )


# ---------- first map ----------

FIRST_MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topics", "reason"],
    "properties": {
        "reason": {"type": "string"},
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "name", "family", "description", "chapter_ids"],
                "properties": {
                    "key": {"type": "string"},
                    "name": {"type": "string"},
                    "family": {"type": "string"},
                    "description": {"type": "string"},
                    "chapter_ids": {"type": "array", "items": {"type": "integer"}},
                },
            },
        },
    },
}


def build_first_map_prompt(chapters: list[dict[str, Any]], *, detail: str, note: str, rejected: list[dict[str, Any]]) -> str:
    return f"""You build the first topic map of a personal archive of conversations with AI models. Each conversation (session) is split into chapters. Right now every chapter is unsorted.

Propose the topics. For each topic give a key, a short name, a family, a description for the sorting model, and the ids of the chapters that belong to it. Chapters that fit no recurring subject stay unsorted: simply leave their ids out. A chapter id may appear in at most one topic.

{COMMON_RULES}

{_guidance(detail, note)}

Earlier proposals the owner rejected (do not propose them again unless the material clearly changed):
{_rejected_lines(rejected)}

In "reason", explain the shape of the map in one or two sentences.

CHAPTERS, grouped by session
{_chapter_lines(chapters)}"""


def validate_first_map(raw: Any, chapters: list[dict[str, Any]]) -> dict[str, Any]:
    known = {c["strand_id"]: c for c in chapters}
    if not isinstance(raw, dict) or not isinstance(raw.get("topics"), list):
        raise TopicError("The proposing model returned no topics.", code="proposal_invalid")
    used: set[int] = set()
    topics, dropped = [], []
    keys: set[str] = set()
    for item in raw["topics"]:
        key = str(item.get("key", "")).strip().lower()
        key = re.sub(r"[^a-z0-9_]+", "_", key).strip("_")
        if not TOPIC_KEY_PATTERN.match(key or "") or key == POOL_KEY or key in keys:
            continue
        ids = [i for i in item.get("chapter_ids", []) if isinstance(i, int) and i in known and i not in used]
        sessions = {known[i]["session_id"] for i in ids}
        name = str(item.get("name", "")).strip()[:60] or key
        if len(ids) < MIN_TOPIC_CHAPTERS or len(sessions) < MIN_TOPIC_SESSIONS:
            dropped.append({"key": key, "name": name, "chapters": len(ids), "sessions": len(sessions)})
            continue
        keys.add(key)
        used.update(ids)
        topics.append({
            "key": key,
            "name": name,
            "family": str(item.get("family", "")).strip()[:40] or "Other",
            "description": str(item.get("description", "")).strip()[:600] or name,
            "chapter_ids": ids,
        })
    if not topics:
        raise TopicError("The proposal had no topic with enough chapters and sessions.", code="proposal_empty")
    return {"topics": topics, "dropped": dropped, "reason": str(raw.get("reason", ""))[:600]}


# ---------- new topics from Unsorted ----------

FIND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["new_topics", "waiting"],
    "properties": {
        "new_topics": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "name", "family", "description", "chapter_ids", "reason"],
                "properties": {
                    "key": {"type": "string"},
                    "name": {"type": "string"},
                    "family": {"type": "string"},
                    "description": {"type": "string"},
                    "chapter_ids": {"type": "array", "items": {"type": "integer"}},
                    "reason": {"type": "string"},
                },
            },
        },
        "waiting": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "chapter_ids", "reason"],
                "properties": {
                    "label": {"type": "string"},
                    "chapter_ids": {"type": "array", "items": {"type": "integer"}},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


def build_find_prompt(
    topics: list[dict[str, str]], pool: list[dict[str, Any]], *, detail: str, note: str, rejected: list[dict[str, Any]]
) -> str:
    return f"""You maintain the topic map of a personal archive of conversations with AI models. Chapters that fit no topic wait in Unsorted. Review Unsorted.

Propose a new topic only for a subject that meets the rules below and does not belong to an existing topic. Everything else goes to "waiting" in groups with a short label and a one-sentence reason (for example: only one session so far). Never propose a topic that duplicates or overlaps an existing one.

{COMMON_RULES}

{_guidance(detail, note)}

Earlier proposals the owner rejected:
{_rejected_lines(rejected)}

EXISTING TOPICS
{_map_lines(topics)}

UNSORTED CHAPTERS, grouped by session
{_chapter_lines(pool)}"""


def validate_find(raw: Any, pool: list[dict[str, Any]], existing_keys: set[str]) -> dict[str, Any]:
    known = {c["strand_id"]: c for c in pool}
    if not isinstance(raw, dict):
        raise TopicError("The proposing model returned nothing usable.", code="proposal_invalid")
    proposals, used = [], set()
    for item in raw.get("new_topics") or []:
        key = re.sub(r"[^a-z0-9_]+", "_", str(item.get("key", "")).strip().lower()).strip("_")
        if not TOPIC_KEY_PATTERN.match(key or "") or key in existing_keys or key == POOL_KEY:
            continue
        ids = [i for i in item.get("chapter_ids", []) if isinstance(i, int) and i in known and i not in used]
        sessions = {known[i]["session_id"] for i in ids}
        if len(ids) < MIN_TOPIC_CHAPTERS or len(sessions) < MIN_TOPIC_SESSIONS:
            continue
        used.update(ids)
        existing_keys = existing_keys | {key}
        name = str(item.get("name", "")).strip()[:60] or key
        proposals.append({
            "key": key, "name": name, "family": str(item.get("family", "")).strip()[:40] or "Other",
            "description": str(item.get("description", "")).strip()[:600] or name,
            "chapter_ids": ids, "reason": str(item.get("reason", ""))[:400],
        })
    waiting = [
        {"label": str(w.get("label", ""))[:80], "chapter_ids": [i for i in w.get("chapter_ids", []) if i in known],
         "reason": str(w.get("reason", ""))[:300]}
        for w in raw.get("waiting") or [] if isinstance(w, dict)
    ]
    return {"new_topics": proposals, "waiting": waiting}


# ---------- refine ----------

REFINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["operations"],
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "target", "parts", "new_description", "reason"],
                "properties": {
                    "op": {"type": "string", "enum": ["split", "new_topic", "redescribe"]},
                    "target": {"type": "string"},
                    "parts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["key", "name", "description", "chapter_ids"],
                            "properties": {
                                "key": {"type": "string"},
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "chapter_ids": {"type": "array", "items": {"type": "integer"}},
                            },
                        },
                    },
                    "new_description": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


def build_refine_prompt(
    topics: list[dict[str, str]], by_topic: dict[str, list[dict[str, Any]]], *, detail: str, note: str,
    rejected: list[dict[str, Any]],
) -> str:
    blocks = []
    for topic in topics:
        if topic["key"] == POOL_KEY or topic["key"] not in by_topic:
            continue
        items = by_topic[topic["key"]]
        lines = [f"### {topic['key']} | {topic['name']} ({len(items)} chapters): {topic['description']}"]
        lines += [
            f'- id {c["strand_id"]}: {c["title"]} | session "{c["session_title"]}" | confidence {c["confidence"]:.2f}'
            for c in items
        ]
        blocks.append("\n".join(lines))
    return f"""You maintain the topic map of a personal archive of conversations with AI models. The archive only grows; nothing is deleted. Review the topics and the chapters currently sorted into them, and propose how the map should become MORE DETAILED or clearer where the material calls for it.

Allowed operations (merging is not allowed):
- "split": target is one existing topic; parts replace it (at least two parts). Every chapter of the target must go to exactly one part.
- "new_topic": a subject has grown inside an existing topic (target) and deserves its own topic; parts holds exactly one new topic with the chapter ids that would move there.
- "redescribe": target's description misleads the sorting model; give new_description and the evidence in reason. parts stays empty.

Each new topic or part needs at least {MIN_REFINE_CHAPTERS} chapters from at least {MIN_TOPIC_SESSIONS} sessions. At most {MAX_REFINE_OPERATIONS} operations; prefer a few strong ones. Return an empty list if the map is fine. Keys are snake_case English. Names, descriptions and reasons in the language of the chapter titles; descriptions are concrete lists for the sorting model.

{_guidance(detail, note)}

Earlier proposals the owner rejected:
{_rejected_lines(rejected)}

CURRENT MAP
{chr(10).join(blocks)}"""


def validate_refine(
    raw: Any, topics: list[dict[str, str]], by_topic: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        raise TopicError("The proposing model returned nothing usable.", code="proposal_invalid")
    keys = {t["key"] for t in topics}
    sessions_of = {c["strand_id"]: c["session_id"] for items in by_topic.values() for c in items}
    out: list[dict[str, Any]] = []
    taken: set[str] = set()
    for item in (raw.get("operations") or [])[:MAX_REFINE_OPERATIONS]:
        op, target = item.get("op"), item.get("target")
        if target not in keys or target == POOL_KEY or target in taken:
            continue
        target_ids = {c["strand_id"] for c in by_topic.get(target, [])}
        if op == "redescribe":
            description = str(item.get("new_description", "")).strip()[:600]
            if description:
                out.append({"op": op, "target": target, "parts": [], "new_description": description,
                            "reason": str(item.get("reason", ""))[:400]})
                taken.add(target)
            continue
        parts, used, ok = [], set(), True
        for part in item.get("parts") or []:
            key = re.sub(r"[^a-z0-9_]+", "_", str(part.get("key", "")).strip().lower()).strip("_")
            ids = [i for i in part.get("chapter_ids", []) if i in target_ids and i not in used]
            if not TOPIC_KEY_PATTERN.match(key or "") or (key in keys and key != target) or key == POOL_KEY:
                ok = False
                break
            if len(ids) < MIN_REFINE_CHAPTERS or len({sessions_of[i] for i in ids}) < MIN_TOPIC_SESSIONS:
                ok = False
                break
            used.update(ids)
            name = str(part.get("name", "")).strip()[:60] or key
            parts.append({"key": key, "name": name, "description": str(part.get("description", "")).strip()[:600] or name,
                          "chapter_ids": ids})
        if not ok:
            continue
        # A split may leave a few stray chapters; the test sorts them anyway.
        if op == "split" and (len(parts) < 2 or len(used) < 0.85 * len(target_ids)):
            continue
        if op == "new_topic" and len(parts) != 1:
            continue
        out.append({"op": op, "target": target, "parts": parts, "new_description": "",
                    "reason": str(item.get("reason", ""))[:400]})
        taken.add(target)
    return out
