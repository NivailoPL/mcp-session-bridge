"""Topics as context blocks.

A topic block holds every user and model turn from the chapters sorted into one
topic of the active map, session by session. Like a session block it is a
snapshot: it notes the map version and a fingerprint of the turns, and turns
"outdated" when sorting or the turns change. Chapters from sensitive groups stay
out unless the block asks for them, because a topic gathers sessions the owner
did not pick one by one.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from app.context_redaction import Redactor
from app.storage import ContextBlockSnapshot, ExchangeRecord, Store
from app.topics import POOL_KEY

# Rough characters per token for the library estimate; the block counts exactly.
CHARS_PER_TOKEN = 3.6


def topic_source_key(topic_key: str) -> str:
    return f"topic:{topic_key}"


def _members(store: Store, topic_key: str, include_sensitive: bool) -> dict[str, Any] | None:
    """The topic, its chapters on the active map, and how many sensitive ones stayed out."""
    version = store.active_topic_version()
    if version is None or topic_key == POOL_KEY:
        return None
    topic = next((t for t in version["topics"] if t["key"] == topic_key), None)
    if topic is None:
        return None
    current = store.topic_current_assignments(version["version_id"])
    chapters = [c for c in store.topic_chapters() if (current.get(c["strand_id"]) or {}).get("topic_key") == topic_key]
    kept = [c for c in chapters if include_sensitive or not c["sensitive"]]
    return {"version": version, "topic": topic, "chapters": kept, "sensitive_left_out": len(chapters) - len(kept)}


def _turns(store: Store, chapters: list[dict[str, Any]]) -> dict[str, list[ExchangeRecord]]:
    """Each session's turns that fall in the chapters, in order; excluded turns are never listed."""
    ranges = store.topic_strand_ranges([c["strand_id"] for c in chapters])
    bounds: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for chapter in chapters:
        bounds[chapter["session_id"]].extend(ranges.get(chapter["strand_id"], []))
    out: dict[str, list[ExchangeRecord]] = {}
    for session_id, spans in bounds.items():
        for exchange in store.list_exchanges(session_id):
            if any(a <= exchange.exchange_id <= b for a, b in spans):
                out.setdefault(session_id, []).append(exchange)
    return out


def _fingerprint(version_id: int, include_sensitive: bool, strand_ids: list[int], turns: dict[str, list[ExchangeRecord]]) -> str:
    canonical = [
        version_id,
        include_sensitive,
        sorted(strand_ids),
        [
            [e.exchange_id, e.model_name, e.user_message, e.assistant_response, e.assistant_created_at,
             e.assistant_masked_at is not None]
            for session_id in sorted(turns) for e in turns[session_id]
        ],
    ]
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode("utf-8")).hexdigest()


def snapshot_topic(
    store: Store, topic_key: str, *, include_sensitive: bool = False, timezone_name: str | None = None,
    redactor: Redactor,
) -> ContextBlockSnapshot:
    from app.contexts import ContextSourceError, count_tokens, render_exchange

    members = _members(store, topic_key, include_sensitive)
    if members is None:
        raise ContextSourceError(f"Unknown topic on the active map: {topic_key}")
    chapters = members["chapters"]
    turns = _turns(store, chapters)
    titles = {c["session_id"]: c["session_title"] for c in chapters}
    order = sorted(turns, key=lambda sid: (turns[sid][0].created_at, sid))
    pieces: list[str] = []
    parts: list[list[Any]] = []
    cursor = 0

    def add(text: str, session_id: str, exchange_id: int | None) -> None:
        nonlocal cursor
        pieces.append(text)
        parts.append([session_id, exchange_id, cursor, cursor + len(text)])
        cursor += len(text)

    for session_id in order:
        add(f"**Session · {titles.get(session_id, 'Untitled')}**\n\n", session_id, None)
        for exchange in turns[session_id]:
            add(render_exchange(exchange, timezone_name=timezone_name) + "\n", session_id, exchange.exchange_id)
    content = "".join(pieces) if pieces else "_No turns are sorted into this topic yet._\n"
    topic = members["topic"]
    groups: dict[str, int] = defaultdict(int)
    for chapter in chapters:
        groups[chapter.get("group_id") or ""] += 1
    meta = {
        "topic_key": topic_key,
        "version_id": members["version"]["version_id"],
        "color": topic.get("color"),
        "family": topic.get("family"),
        "include_sensitive": include_sensitive,
        "sensitive_left_out": members["sensitive_left_out"],
        "session_count": len(order),
        "chapter_count": len(chapters),
        "strand_ids": sorted(c["strand_id"] for c in chapters),
        "group_ids": [g for g, _ in sorted(groups.items(), key=lambda item: -item[1])][:3],
        "parts": parts,
    }
    return ContextBlockSnapshot(
        source_type="topic",
        source_key=topic_source_key(topic_key),
        source_session_id=None,
        source_file_id=None,
        title=topic["name"],
        content=content,
        source_hash=_fingerprint(meta["version_id"], include_sensitive, meta["strand_ids"], turns),
        found_ids=redactor.find(f"{topic['name']}\n{content}"),
        token_count=count_tokens(content),
        meta=meta,
    )


def topic_block_status(store: Store, block: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """missing when the topic left the active map; outdated when sorting or its turns changed."""
    meta = block.get("meta") or {}
    include = bool(meta.get("include_sensitive"))
    members = _members(store, meta.get("topic_key") or block["source_key"].split(":", 1)[-1], include)
    if members is None:
        return "missing", {}
    strand_ids = [c["strand_id"] for c in members["chapters"]]
    turns = _turns(store, members["chapters"])
    current = _fingerprint(members["version"]["version_id"], include, strand_ids, turns)
    if current == block["source_hash"]:
        return "current", {}
    before = set(meta.get("strand_ids") or [])
    return "outdated", {"new_chapters": len(set(strand_ids) - before), "gone_chapters": len(before - set(strand_ids))}


def trim_topic_content(block: dict[str, Any], sessions_above: set[str], turns_above: set[int]) -> tuple[str, int]:
    """The block's text without turns an earlier block already gives the model; and how many were dropped."""
    parts = (block.get("meta") or {}).get("parts") or []
    content = block.get("content", "")
    if not parts:
        return content, 0
    kept: list[str] = []
    skipped = 0
    header: str | None = None
    for session_id, exchange_id, start, end in parts:
        piece = content[start:end]
        if exchange_id is None:
            header = piece
            continue
        if session_id in sessions_above or exchange_id in turns_above:
            skipped += 1
            continue
        if header is not None:
            kept.append(header)
            header = None
        kept.append(piece)
    text = "".join(kept)
    return (text if text else "_Every turn of this topic is already in a block above._\n"), skipped


def topic_turn_ids(block: dict[str, Any]) -> set[int]:
    return {part[1] for part in (block.get("meta") or {}).get("parts") or [] if part[1] is not None}


def library_topics(store: Store) -> list[dict[str, Any]]:
    """Topics of the active map as library cards; Unsorted is not a block."""
    version = store.active_topic_version()
    if version is None:
        return []
    current = store.topic_current_assignments(version["version_id"])
    sizes = store.topic_strand_sizes()
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chapter in store.topic_chapters():
        item = current.get(chapter["strand_id"])
        if item and item["topic_key"] != POOL_KEY:
            by_topic[item["topic_key"]].append({**chapter, "confidence": item.get("confidence")})
    out = []
    for topic in version["topics"]:
        if topic["key"] == POOL_KEY:
            continue
        chapters = by_topic.get(topic["key"], [])
        sent = [c for c in chapters if not c["sensitive"]]
        groups: dict[str, int] = defaultdict(int)
        for chapter in sent:
            groups[chapter.get("group_id") or ""] += 1
        out.append({
            "topic_key": topic["key"],
            "name": topic["name"],
            "family": topic.get("family") or "Other",
            "color": topic.get("color"),
            "chapter_count": len(sent),
            "session_count": len({c["session_id"] for c in sent}),
            "sensitive_count": len(chapters) - len(sent),
            "token_estimate": round(sum(sizes.get(c["strand_id"], 0) for c in sent) / CHARS_PER_TOKEN),
            "group_ids": [g for g, _ in sorted(groups.items(), key=lambda item: -item[1])][:3],
            "confidences": [round(c["confidence"] or 0, 2) for c in sorted(sent, key=lambda c: -(c["confidence"] or 0))][:14],
            "last_at": max((c.get("session_updated_at") or 0 for c in sent), default=0),
            "version_id": version["version_id"],
        })
    return out
