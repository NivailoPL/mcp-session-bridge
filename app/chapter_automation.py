"""Automatic chapters: one batch engine behind "Fill now" and the schedule.

A batch walks the chosen groups in order and handles one session at a time
with the same logic as the session panel: a first split for untouched
sessions, an update for sessions with new exchanges. Pause lets the current
session finish and then stops; a restart always leaves a batch paused, never
running. Token figures are estimates from text length, because Codex App
Server does not report usage back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.storage import Store
from app.strand_runtime import StrandRuntime
from app.strands import StrandError
from app.time_format import DISPLAY_TIMEZONE_SETTING_KEY, resolve_timezone_name

logger = logging.getLogger(__name__)

FILL_SETTINGS_KEY = "chapters.fill"
SCHEDULE_SETTINGS_KEY = "chapters.schedule"
BATCH_STATE_KEY = "chapters.batch"
BATCH_HISTORY_KEY = "chapters.batch_history"

SCHEDULE_MODES = ("interval", "daily", "weekly")
SCHEDULE_INTERVAL_HOURS = (3, 6, 12, 24)
BATCH_FAILURE_LIMIT = 3
BATCH_HISTORY_LIMIT = 8
SCHEDULER_TICK_SECONDS = 60

# Estimate: about four characters per token, plus the instructions sent with
# every session. Codex reasoning and its answer are not included.
CHARS_PER_TOKEN = 4
PROMPT_OVERHEAD_TOKENS = 400

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SKIP_CODES = {"source_too_large", "source_empty", "already_complete", "result_empty"}


class AutomationError(ValueError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


# ---------- settings ----------

def default_fill_settings() -> dict[str, Any]:
    return {"group_ids": None, "include_sensitive": False, "auto_accept": True}


def default_schedule() -> dict[str, Any]:
    return {"enabled": False, "mode": "daily", "interval_hours": 6, "time": "03:00", "days": [0, 1, 2, 3, 4, 5, 6],
            "anchor_at": None, "last_run_at": None}


def validate_fill_settings(payload: Any, current: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not set(payload) <= {"group_ids", "include_sensitive", "auto_accept"}:
        raise AutomationError("Send group_ids, include_sensitive and/or auto_accept.", code="settings_invalid")
    result = dict(current)
    if "group_ids" in payload:
        group_ids = payload["group_ids"]
        if group_ids is not None and (
            not isinstance(group_ids, list) or not all(isinstance(item, str) and item for item in group_ids)
        ):
            raise AutomationError("group_ids must be a list of group IDs or null for all groups.", code="settings_invalid")
        result["group_ids"] = list(dict.fromkeys(group_ids)) if group_ids is not None else None
    for key in ("include_sensitive", "auto_accept"):
        if key in payload:
            if not isinstance(payload[key], bool):
                raise AutomationError(f"{key} must be true or false.", code="settings_invalid")
            result[key] = payload[key]
    return result


def validate_schedule(payload: Any, current: dict[str, Any], *, now: int) -> dict[str, Any]:
    allowed = {"enabled", "mode", "interval_hours", "time", "days"}
    if not isinstance(payload, dict) or not payload or not set(payload) <= allowed:
        raise AutomationError("Send enabled, mode, interval_hours, time and/or days.", code="settings_invalid")
    result = dict(current)
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise AutomationError("enabled must be true or false.", code="settings_invalid")
        result["enabled"] = payload["enabled"]
    if "mode" in payload:
        if payload["mode"] not in SCHEDULE_MODES:
            raise AutomationError("mode must be interval, daily or weekly.", code="settings_invalid")
        result["mode"] = payload["mode"]
    if "interval_hours" in payload:
        if payload["interval_hours"] not in SCHEDULE_INTERVAL_HOURS:
            raise AutomationError("interval_hours must be 3, 6, 12 or 24.", code="settings_invalid")
        result["interval_hours"] = payload["interval_hours"]
    if "time" in payload:
        if not isinstance(payload["time"], str) or not _TIME.match(payload["time"]):
            raise AutomationError("time must look like 03:00.", code="settings_invalid")
        result["time"] = payload["time"]
    if "days" in payload:
        days = payload["days"]
        if not isinstance(days, list) or not days or not all(
            isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6 for day in days
        ):
            raise AutomationError("days must list weekdays from 0 (Monday) to 6 (Sunday).", code="settings_invalid")
        result["days"] = sorted(set(days))
    # Any change starts counting from now, so turning the schedule on never fires at once.
    result["anchor_at"] = now
    return result


def next_run_at(schedule: dict[str, Any], timezone_name: str, *, now: int) -> int | None:
    """Unix time of the next scheduled batch, or None when the schedule is off."""
    if not schedule.get("enabled"):
        return None
    after = max(schedule.get("last_run_at") or 0, schedule.get("anchor_at") or 0) or now
    if schedule["mode"] == "interval":
        return after + schedule["interval_hours"] * 3600
    zone = ZoneInfo(timezone_name)
    hour, minute = (int(part) for part in schedule["time"].split(":"))
    start = datetime.fromtimestamp(after, tz=zone)
    candidate = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= after:
        candidate += timedelta(days=1)
    days = set(range(7)) if schedule["mode"] == "daily" else set(schedule.get("days") or range(7))
    for _ in range(8):
        if candidate.weekday() in days:
            return int(candidate.timestamp())
        candidate += timedelta(days=1)
    return None


# ---------- engine ----------

class ChapterAutomation:
    def __init__(self, store: Store, runtime: StrandRuntime):
        self.store = store
        self.runtime = runtime
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ----- stored settings -----

    def _read(self, key: str, default: dict[str, Any]) -> dict[str, Any]:
        raw = self.store.get_app_setting(key)
        if raw is None:
            return dict(default)
        try:
            value = json.loads(raw)
        except ValueError:
            return dict(default)
        return {**default, **value} if isinstance(value, dict) else dict(default)

    def _write(self, key: str, value: Any) -> None:
        self.store.set_app_setting(key, json.dumps(value, ensure_ascii=False))

    def fill_settings(self) -> dict[str, Any]:
        return self._read(FILL_SETTINGS_KEY, default_fill_settings())

    def set_fill_settings(self, payload: Any) -> dict[str, Any]:
        settings = validate_fill_settings(payload, self.fill_settings())
        self._write(FILL_SETTINGS_KEY, settings)
        return settings

    def schedule(self) -> dict[str, Any]:
        return self._read(SCHEDULE_SETTINGS_KEY, default_schedule())

    def set_schedule(self, payload: Any, *, now: int | None = None) -> dict[str, Any]:
        schedule = validate_schedule(payload, self.schedule(), now=int(now if now is not None else time.time()))
        self._write(SCHEDULE_SETTINGS_KEY, schedule)
        return schedule

    def batch(self) -> dict[str, Any]:
        return self._read(BATCH_STATE_KEY, {"status": "idle"})

    def history(self) -> list[dict[str, Any]]:
        raw = self.store.get_app_setting(BATCH_HISTORY_KEY)
        try:
            value = json.loads(raw) if raw else []
        except ValueError:
            value = []
        return value if isinstance(value, list) else []

    def timezone(self) -> str:
        try:
            return resolve_timezone_name(self.store.get_app_setting(DISPLAY_TIMEZONE_SETTING_KEY))
        except ValueError:
            return "UTC"

    # ----- scope and progress -----

    def _scope(self, fill: dict[str, Any]) -> list[dict[str, Any]]:
        groups = self.store.list_session_groups()
        chosen = fill["group_ids"]
        ordered = groups if chosen is None else sorted(
            (group for group in groups if group["group_id"] in chosen),
            key=lambda group: chosen.index(group["group_id"]),
        )
        return [group for group in ordered if fill["include_sensitive"] or not group.get("is_sensitive")]

    def progress(self) -> dict[str, Any]:
        fill = self.fill_settings()
        states = self.store.chapter_states()
        chars = self.store.chapter_char_estimates(states)
        sessions = self.store.list_sessions()
        scope_ids = {group["group_id"] for group in self._scope(fill)}
        groups: dict[str, dict[str, Any]] = {}
        for group in self.store.list_session_groups():
            groups[group["group_id"]] = {
                "group_id": group["group_id"],
                "name": group["name"],
                "color": group.get("color"),
                "is_sensitive": bool(group.get("is_sensitive")),
                "in_scope": group["group_id"] in scope_ids,
                "total": 0, "done": 0, "review": 0, "todo": 0, "estimated_tokens": 0,
            }
        for session in sessions:
            if not session.get("exchange_count"):
                continue
            group = groups.get(session["group_id"])
            if group is None:
                continue
            state = (states.get(session["session_id"]) or {"state": "none"})["state"]
            group["total"] += 1
            if state == "complete":
                group["done"] += 1
            elif state in ("review", "running"):
                group["review"] += 1
            else:
                group["todo"] += 1
                group["estimated_tokens"] += _tokens(chars.get(session["session_id"], 0))
        in_scope = [group for group in groups.values() if group["in_scope"]]
        return {
            "groups": list(groups.values()),
            "total": sum(group["total"] for group in in_scope),
            "done": sum(group["done"] for group in in_scope),
            "review": sum(group["review"] for group in in_scope),
            "todo": sum(group["todo"] for group in in_scope),
            "estimated_tokens": sum(group["estimated_tokens"] for group in in_scope),
        }

    def overview(self, *, now: int | None = None) -> dict[str, Any]:
        current = int(now if now is not None else time.time())
        schedule = self.schedule()
        return {
            "fill": self.fill_settings(),
            "schedule": schedule,
            "next_run_at": next_run_at(schedule, self.timezone(), now=current),
            "timezone": self.timezone(),
            "batch": self.batch(),
            "history": self.history(),
            "progress": self.progress(),
            "estimate_note": (
                f"About one token per {CHARS_PER_TOKEN} characters of the exchanges sent, plus "
                f"~{PROMPT_OVERHEAD_TOKENS} tokens of instructions per session. Codex does not report real "
                "usage, and its reasoning and answer are not included."
            ),
        }

    # ----- running -----

    def recover(self) -> None:
        """A batch cut off by a restart comes back paused, never running."""
        batch = self.batch()
        if batch.get("status") in ("running", "pausing"):
            batch.update(status="paused", current_session_id=None, current_title=None,
                         last_error="Paused because Bridge restarted. Press Start to continue.")
            self._write(BATCH_STATE_KEY, batch)

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, *, trigger: str = "manual") -> dict[str, Any]:
        async with self._lock:
            if self.running():
                raise AutomationError("A fill is already running.", code="batch_running")
            if self.runtime.codex is None:
                raise AutomationError("Codex App Server is not configured.", code="codex_unavailable")
            if not self.store.get_token_features()["enabled"]:
                raise AutomationError("Token-spending features are switched off.", code="feature_off")
            previous = self.batch()
            resume = previous.get("status") == "paused"
            batch = {
                "status": "running",
                "trigger": trigger,
                "started_at": previous.get("started_at") if resume else int(time.time()),
                "finished_at": None,
                "processed": previous.get("processed", 0) if resume else 0,
                "accepted": previous.get("accepted", 0) if resume else 0,
                "failed": previous.get("failed", 0) if resume else 0,
                "skipped": list(previous.get("skipped", [])) if resume else [],
                # sessions that hit a Codex error are retried after Resume; skipped ones are not
                "failed_ids": [],
                "consecutive_failures": 0,
                "current_session_id": None,
                "current_title": None,
                "last_error": None,
                "pause_requested": False,
            }
            self._write(BATCH_STATE_KEY, batch)
            self._task = asyncio.create_task(self._loop())
            return batch

    def pause(self) -> dict[str, Any]:
        batch = self.batch()
        if batch.get("status") != "running":
            raise AutomationError("No fill is running.", code="batch_idle")
        batch.update(status="pausing", pause_requested=True)
        self._write(BATCH_STATE_KEY, batch)
        return batch

    def _next_session(self, skipped: set[str]) -> dict[str, Any] | None:
        fill = self.fill_settings()
        states = self.store.chapter_states()
        by_group: dict[str, list[dict[str, Any]]] = {}
        for session in self.store.list_sessions():
            by_group.setdefault(session["group_id"], []).append(session)
        for group in self._scope(fill):
            for session in by_group.get(group["group_id"], []):
                if not session.get("exchange_count") or session["session_id"] in skipped:
                    continue
                state = (states.get(session["session_id"]) or {"state": "none"})["state"]
                if state in ("none", "manual", "behind"):
                    return {**session, "is_sensitive": bool(group.get("is_sensitive"))}
        return None

    async def _loop(self) -> None:
        try:
            while True:
                batch = await asyncio.to_thread(self.batch)
                if batch.get("pause_requested"):
                    batch.update(status="paused", current_session_id=None, current_title=None)
                    await asyncio.to_thread(self._write, BATCH_STATE_KEY, batch)
                    return
                session = await asyncio.to_thread(
                    self._next_session, set(batch.get("skipped", [])) | set(batch.get("failed_ids", []))
                )
                if session is None:
                    await asyncio.to_thread(self._finish, batch, "done")
                    return
                batch.update(current_session_id=session["session_id"], current_title=session.get("title"))
                await asyncio.to_thread(self._write, BATCH_STATE_KEY, batch)
                outcome = await self._process(session)
                batch = await asyncio.to_thread(self.batch)
                batch["processed"] = batch.get("processed", 0) + 1
                if outcome["status"] == "ok":
                    batch["consecutive_failures"] = 0
                    batch["accepted"] = batch.get("accepted", 0) + outcome.get("accepted", 0)
                elif outcome["status"] == "skipped":
                    batch["skipped"] = [*batch.get("skipped", []), session["session_id"]]
                    batch["consecutive_failures"] = 0
                else:
                    batch["failed"] = batch.get("failed", 0) + 1
                    batch["failed_ids"] = [*batch.get("failed_ids", []), session["session_id"]]
                    batch["consecutive_failures"] = batch.get("consecutive_failures", 0) + 1
                    batch["last_error"] = outcome.get("message")
                    if batch["consecutive_failures"] >= BATCH_FAILURE_LIMIT:
                        batch.update(
                            status="paused", current_session_id=None, current_title=None,
                            last_error=f"Paused after {BATCH_FAILURE_LIMIT} Codex errors in a row: {outcome.get('message')}",
                        )
                        await asyncio.to_thread(self._write, BATCH_STATE_KEY, batch)
                        return
                await asyncio.to_thread(self._write, BATCH_STATE_KEY, batch)
        except asyncio.CancelledError:
            batch = await asyncio.to_thread(self.batch)
            batch.update(status="paused", current_session_id=None, current_title=None,
                         last_error="Paused because Bridge stopped.")
            await asyncio.to_thread(self._write, BATCH_STATE_KEY, batch)
            raise
        except Exception:
            logger.exception("Chapter fill failed")
            batch = await asyncio.to_thread(self.batch)
            batch.update(status="paused", current_session_id=None, current_title=None,
                         last_error="The fill stopped on an unexpected error. Press Start to try again.")
            await asyncio.to_thread(self._write, BATCH_STATE_KEY, batch)

    async def _process(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = session["session_id"]
        try:
            run = await self.runtime.run_now(session_id, actor="chapters-fill", confirm_sensitive=session["is_sensitive"])
        except StrandError as exc:
            if exc.code in _SKIP_CODES or exc.code == "pending_exists":
                return {"status": "skipped", "message": str(exc)}
            return {"status": "failed", "message": str(exc)}
        if run["status"] != "completed":
            if run.get("error_code") in _SKIP_CODES or run.get("error_code") == "range_unknown":
                return {"status": "skipped", "message": run.get("error_message")}
            return {"status": "failed", "message": run.get("error_message")}
        accepted = 0
        if self.fill_settings()["auto_accept"]:
            accepted = await asyncio.to_thread(self.store.accept_strands, session_id)
        return {"status": "ok", "accepted": accepted}

    def _finish(self, batch: dict[str, Any], status: str) -> None:
        batch.update(status=status, finished_at=int(time.time()), current_session_id=None, current_title=None)
        self._write(BATCH_STATE_KEY, batch)
        entry = {key: batch.get(key) for key in ("trigger", "started_at", "finished_at", "processed", "accepted", "failed")}
        entry["skipped"] = len(batch.get("skipped", []))
        self._write(BATCH_HISTORY_KEY, [entry, *self.history()][:BATCH_HISTORY_LIMIT])

    # ----- schedule -----

    async def tick(self, *, now: int | None = None) -> bool:
        """Start a scheduled batch when one is due; returns True when it started one."""
        current = int(now if now is not None else time.time())
        schedule = await asyncio.to_thread(self.schedule)
        due = await asyncio.to_thread(next_run_at, schedule, self.timezone(), now=current)
        if due is None or current < due or self.running():
            return False
        if (await asyncio.to_thread(self.batch)).get("status") == "paused":
            # A paused batch waits for the owner; the schedule does not resume it behind their back.
            schedule["last_run_at"] = current
            await asyncio.to_thread(self._write, SCHEDULE_SETTINGS_KEY, schedule)
            return False
        schedule["last_run_at"] = current
        await asyncio.to_thread(self._write, SCHEDULE_SETTINGS_KEY, schedule)
        try:
            await self.start(trigger="schedule")
        except AutomationError as exc:
            logger.info("Scheduled chapter fill skipped: %s", exc)
            return False
        return True

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    async def close(self) -> None:
        if self.running():
            assert self._task is not None
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def _tokens(chars: int) -> int:
    return chars // CHARS_PER_TOKEN + PROMPT_OVERHEAD_TOKENS if chars else 0

