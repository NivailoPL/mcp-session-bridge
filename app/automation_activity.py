"""Codex activity: what automation did per day, for the header panel.

Titles come from the log automatic naming writes. Chapters and Topics are read
back from the runs they already store, so their history reaches back further
than the panel itself. Tokens are estimates from text length, because Codex
App Server does not report usage; sorting into topics runs on OpenRouter,
which does report a real cost in dollars.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.chapter_automation import CHARS_PER_TOKEN, PROMPT_OVERHEAD_TOKENS
from app.storage import Store
from app.topics import POOL_KEY

ACTIVITY_DAYS = 30
# Rows sent per area per day; counts and totals always cover every row.
ITEMS_PER_DAY = 40
CHAPTERS_ACTOR = "chapters-fill"
TOPICS_ACTOR = "system"


def _empty_day(day: date) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "estimated_tokens": 0,
        "titles": {"count": 0, "estimated_tokens": 0, "items": []},
        "chapters": {"sessions": 0, "chapters": 0, "estimated_tokens": 0, "items": []},
        "topics": {"chapters": 0, "topics": 0, "new_topics": 0, "cost_usd": 0.0, "items": []},
    }


def activity_report(
    store: Store, *, timezone_name: str, days: int = ACTIVITY_DAYS, now: int | None = None
) -> dict[str, Any]:
    """Every day of the window, newest first, including days with nothing done."""
    zone = ZoneInfo(timezone_name)
    current = int(now if now is not None else time.time())
    today = datetime.fromtimestamp(current, zone).date()
    first = today - timedelta(days=days - 1)
    since = int(datetime.combine(first, datetime.min.time(), zone).timestamp())
    by_day = {first + timedelta(days=offset): _empty_day(first + timedelta(days=offset)) for offset in range(days)}

    def bucket(timestamp: int) -> dict[str, Any] | None:
        return by_day.get(datetime.fromtimestamp(timestamp, zone).date())

    with store._lock, store._connect() as conn:
        titles = conn.execute(
            "SELECT session_id, old_title, new_title, estimated_tokens, created_at FROM session_title_events "
            "WHERE created_at >= ? ORDER BY created_at DESC, event_id DESC",
            (since,),
        ).fetchall()
        runs = conn.execute(
            """
            SELECT r.run_id, r.session_id, r.completed_at, r.proposed_count, se.title,
                   (SELECT COALESCE(SUM(length(e.user_message) + CASE WHEN e.assistant_masked_at IS NULL
                                    THEN length(e.assistant_response) ELSE 0 END), 0)
                    FROM exchanges e WHERE e.session_id = r.session_id AND e.exchange_id <= r.source_exchange_id
                      AND e.deleted_at IS NULL) AS chars
            FROM strand_runs r JOIN sessions se ON se.session_id = r.session_id
            WHERE r.created_by = ? AND r.status = 'completed' AND r.completed_at >= ?
            ORDER BY r.completed_at DESC, r.run_id DESC
            """,
            (CHAPTERS_ACTOR, since),
        ).fetchall()
        chapter_titles: dict[int, list[str]] = {}
        for row in conn.execute(
            "SELECT run_id, title FROM strands WHERE run_id IN (SELECT run_id FROM strand_runs "
            "WHERE created_by = ? AND completed_at >= ?) ORDER BY strand_id",
            (CHAPTERS_ACTOR, since),
        ):
            chapter_titles.setdefault(row["run_id"], []).append(row["title"])
        sorted_items = conn.execute(
            """
            SELECT i.chapter_title, i.topic_key, i.cost_usd, i.updated_at, r.version_id
            FROM topic_run_items i JOIN topic_runs r ON r.run_id = i.run_id
            WHERE r.kind = 'classify' AND r.created_by = ? AND i.status = 'done' AND i.updated_at >= ?
            ORDER BY i.updated_at DESC
            """,
            (TOPICS_ACTOR, since),
        ).fetchall()
        accepts = conn.execute(
            "SELECT created_at, data_json FROM topic_journal "
            "WHERE action = 'proposal.accept' AND actor = 'system' AND created_at >= ? ORDER BY created_at DESC",
            (since,),
        ).fetchall()
        proposals: dict[int, dict[str, Any]] = {}
        for accept in accepts:
            proposal_id = (json.loads(accept["data_json"] or "{}") or {}).get("proposal_id")
            if proposal_id is None:
                continue
            row = conn.execute(
                "SELECT payload_json FROM topic_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is not None:
                proposals[proposal_id] = json.loads(row["payload_json"] or "{}")
        version_ids = {row["version_id"] for row in sorted_items}
        names: dict[int, dict[str, str]] = {}
        for version_id in version_ids:
            row = conn.execute(
                "SELECT topics_json FROM topic_map_versions WHERE version_id = ?", (version_id,)
            ).fetchone()
            topics = json.loads(row["topics_json"]) if row else []
            names[version_id] = {topic["key"]: topic.get("name") or topic["key"] for topic in topics}

    for row in titles:
        day = bucket(row["created_at"])
        if day is None:
            continue
        area = day["titles"]
        area["count"] += 1
        area["estimated_tokens"] += row["estimated_tokens"]
        if len(area["items"]) < ITEMS_PER_DAY:
            area["items"].append({
                "at": row["created_at"], "session_id": row["session_id"],
                "old_title": row["old_title"], "new_title": row["new_title"],
            })

    for row in runs:
        day = bucket(row["completed_at"])
        if day is None:
            continue
        tokens = row["chars"] // CHARS_PER_TOKEN + PROMPT_OVERHEAD_TOKENS
        area = day["chapters"]
        area["sessions"] += 1
        area["chapters"] += row["proposed_count"]
        area["estimated_tokens"] += tokens
        if len(area["items"]) < ITEMS_PER_DAY:
            area["items"].append({
                "at": row["completed_at"], "session_id": row["session_id"], "title": row["title"],
                "count": row["proposed_count"], "chapters": chapter_titles.get(row["run_id"], []),
            })

    # Topics are grouped per day by topic: how many chapters each one received.
    grouped: dict[date, dict[str, dict[str, Any]]] = {}
    for row in sorted_items:
        day_key = datetime.fromtimestamp(row["updated_at"], zone).date()
        if day_key not in by_day:
            continue
        day = by_day[day_key]
        day["topics"]["chapters"] += 1
        day["topics"]["cost_usd"] += row["cost_usd"] or 0
        key = row["topic_key"]
        name = "Unsorted" if key == POOL_KEY else names.get(row["version_id"], {}).get(key, key)
        topic = grouped.setdefault(day_key, {}).setdefault(
            key, {"key": key, "name": name, "added": 0, "is_new": False, "chapters": []}
        )
        topic["added"] += 1
        if len(topic["chapters"]) < 5:
            topic["chapters"].append(row["chapter_title"])

    for accept in accepts:
        day_key = datetime.fromtimestamp(accept["created_at"], zone).date()
        if day_key not in by_day:
            continue
        payload = proposals.get((json.loads(accept["data_json"] or "{}") or {}).get("proposal_id"), {})
        for part in payload.get("parts") or []:
            name = part.get("name") or part.get("key") or "New topic"
            key = part.get("key") or name
            topic = grouped.setdefault(day_key, {}).setdefault(
                key, {"key": key, "name": name, "added": 0, "is_new": False, "chapters": []}
            )
            topic["is_new"] = True
            by_day[day_key]["topics"]["new_topics"] += 1

    for day_key, topics in grouped.items():
        area = by_day[day_key]["topics"]
        ordered = sorted(topics.values(), key=lambda topic: (not topic["is_new"], -topic["added"], topic["name"]))
        area["topics"] = sum(1 for topic in ordered if topic["key"] != POOL_KEY and topic["added"])
        area["items"] = ordered[:ITEMS_PER_DAY]
        area["cost_usd"] = round(area["cost_usd"], 6)

    for day in by_day.values():
        day["estimated_tokens"] = day["titles"]["estimated_tokens"] + day["chapters"]["estimated_tokens"]

    return {
        "timezone": timezone_name,
        "today": today.isoformat(),
        "days": [by_day[key] for key in sorted(by_day, reverse=True)],
        "estimate_note": (
            f"Codex tokens are estimates: about one token per {CHARS_PER_TOKEN} characters sent, plus the "
            "instructions. Codex does not report real usage. Sorting into topics runs on OpenRouter and "
            "shows its real cost."
        ),
    }


def live_status(chapters: Any, topics: Any) -> dict[str, Any]:
    """What automation is doing right now, for the header meter."""
    batch = chapters.batch()
    status = batch.get("status", "idle")
    chapter_live: dict[str, Any] = {"status": status, "last_error": batch.get("last_error")}
    if status in ("running", "pausing"):
        processed = batch.get("processed", 0)
        chapter_live.update(
            processed=processed,
            total=processed + chapters.progress()["todo"],
            current_title=batch.get("current_title"),
        )
    job = getattr(topics, "job", None)
    topic_live = {"status": "running", "kind": job.get("kind"), "progress": job.get("progress")} if job else {"status": "idle"}
    return {"chapters": chapter_live, "topics": topic_live}
