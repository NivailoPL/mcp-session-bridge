"""Closed sessions and their summaries.

A session closes on its own after a stretch without a new exchange, once the
owner switches closing on. Models can still read a closed session but cannot
add to it; the owner can reopen it for the same stretch, after which it closes
again unless a new exchange arrived. Closing is a plain rule with no model in
it, so it keeps working while "Runs models" is off.

A summary closes its session too: while a written summary covers the latest
exchange, the session is closed, whatever the timer says. That is how a
summary written by hand on an open session closes it, and reopening a
session deletes its summary.

Each close queues one summary. Codex writes it from the whole transcript, in
Markdown, from a fixed base prompt plus the owner's own instructions. The
length is a target for the model, not a cut: Bridge keeps the whole answer,
however long it comes back. The target follows the session: a fifth of the
transcript, never above the owner's setting and never below the floor, since
a model given a number tends to fill it. Only the
summaries wait for the "Runs models" switch; Regenerate always works when the
owner presses it. Token figures are estimates from text length, because Codex
App Server does not report usage back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.codex_app_server import CodexAppServerError, CodexUnavailableError
from app.strands import STRAND_EFFORTS

if TYPE_CHECKING:
    from app.storage import Store

logger = logging.getLogger(__name__)

SUMMARY_SETTINGS_KEY = "summaries.settings"
CODEX_MODELS_SETTING_KEY = "codex.models.catalog"

DAY_SECONDS = 86_400
CLOSE_AFTER_DAYS_CHOICES = (7, 14, 30, 60, 90)
CARD_COLORS = ("frost", "paper")
MIN_SUMMARY_CHARS = 300
# The target is this share of the transcript, so a short session is not padded out to the setting.
SUMMARY_SHARE = 0.2
MAX_SUMMARY_CHARS = 8000
INSTRUCTIONS_MAX_CHARS = 8000
CODEX_MODEL_MAX_CHARS = 96

# The fixed half of the prompt; the owner's instructions follow it.
BASE_PROMPT = "Summarize this session."

# What Bridge always adds around the two boxes. Not shown as editable.
FORMAT_RULES = (
    "Write the summary in Markdown: short paragraphs, lists and headings where they help, "
    "`code` for file names, commands and identifiers. Write in the language the session is written in. "
    "Use only what the transcript says; when something was left unresolved, say so. "
    "Do not address the reader and do not describe these instructions."
)

# Codex accepts 500k characters per turn; the transcript gets what the prompt leaves.
TRANSCRIPT_MAX_CHARS = 440_000
SUMMARY_TIMEOUT_SECONDS = 300.0
FAILURE_LIMIT = 3
CHARS_PER_TOKEN = 4
PROMPT_OVERHEAD_TOKENS = 300

SUMMARY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

SUMMARY_SCHEMA_SQL = """
-- One summary per session: the latest one written, failed or waiting.
-- Additive and ignored by older Bridges, so it needs no schema bump.
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    upto_exchange_id INTEGER,
    exchange_count INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    prompt_hash TEXT,
    error TEXT,
    estimated_tokens INTEGER NOT NULL DEFAULT 0,
    written_at INTEGER,
    updated_at INTEGER NOT NULL
);
"""


class SummaryError(ValueError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class CodexNotReady(Exception):
    """Codex is busy or not reachable right now; the summary stays queued for a later check."""


# ---------- settings ----------

def default_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "close_after_days": 30,
        "skip_pinned": True,
        "group_ids": None,
        "include_sensitive": False,
        "card_color": "frost",
        "max_chars": 2000,
        "instructions": "",
        "codex_model": "",
        "codex_effort": "low",
    }


def read_settings(raw: str | None) -> dict[str, Any]:
    settings = default_settings()
    if not raw:
        return settings
    try:
        stored = json.loads(raw)
    except ValueError:
        return settings
    if isinstance(stored, dict):
        settings.update({key: stored[key] for key in settings if key in stored})
    return settings


def validate_settings(payload: Any, current: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload or not set(payload) <= set(default_settings()):
        raise SummaryError("Send only known summary settings.", code="settings_invalid")
    result = dict(current)
    for key in ("enabled", "skip_pinned", "include_sensitive"):
        if key in payload:
            if not isinstance(payload[key], bool):
                raise SummaryError(f"{key} must be true or false.", code="settings_invalid")
            result[key] = payload[key]
    if "close_after_days" in payload:
        if payload["close_after_days"] not in CLOSE_AFTER_DAYS_CHOICES or isinstance(payload["close_after_days"], bool):
            choices = ", ".join(str(day) for day in CLOSE_AFTER_DAYS_CHOICES)
            raise SummaryError(f"close_after_days must be one of {choices}.", code="settings_invalid")
        result["close_after_days"] = payload["close_after_days"]
    if "group_ids" in payload:
        group_ids = payload["group_ids"]
        if group_ids is not None and (
            not isinstance(group_ids, list) or not all(isinstance(item, str) and item for item in group_ids)
        ):
            raise SummaryError("group_ids must be a list of group IDs or null for all groups.", code="settings_invalid")
        result["group_ids"] = list(dict.fromkeys(group_ids)) if group_ids is not None else None
    if "card_color" in payload:
        if payload["card_color"] not in CARD_COLORS:
            raise SummaryError("card_color must be frost or paper.", code="settings_invalid")
        result["card_color"] = payload["card_color"]
    if "max_chars" in payload:
        value = payload["max_chars"]
        if isinstance(value, bool) or not isinstance(value, int) or not MIN_SUMMARY_CHARS <= value <= MAX_SUMMARY_CHARS:
            raise SummaryError(
                f"max_chars must be a whole number from {MIN_SUMMARY_CHARS} to {MAX_SUMMARY_CHARS}.",
                code="settings_invalid",
            )
        result["max_chars"] = value
    if "instructions" in payload:
        if not isinstance(payload["instructions"], str):
            raise SummaryError("instructions must be text.", code="settings_invalid")
        instructions = payload["instructions"].strip()
        if len(instructions) > INSTRUCTIONS_MAX_CHARS:
            raise SummaryError(
                f"Additional instructions must be {INSTRUCTIONS_MAX_CHARS} characters or fewer.", code="settings_invalid"
            )
        result["instructions"] = instructions
    if "codex_model" in payload:
        model = payload["codex_model"]
        if not isinstance(model, str) or len(model.strip()) > CODEX_MODEL_MAX_CHARS:
            raise SummaryError(f"codex_model must be {CODEX_MODEL_MAX_CHARS} characters or fewer.", code="settings_invalid")
        result["codex_model"] = model.strip()
    if "codex_effort" in payload:
        if payload["codex_effort"] not in STRAND_EFFORTS:
            raise SummaryError("codex_effort is not a known reasoning effort.", code="settings_invalid")
        result["codex_effort"] = payload["codex_effort"]
    return result


# ---------- the closing rule ----------

def close_state(
    settings: dict[str, Any],
    *,
    last_turn_at: int,
    pinned: bool,
    group_id: str,
    reopened_until: int | None,
    now: int,
) -> dict[str, Any]:
    """Whether a session is closed now, and when it closed or will close."""
    open_state = {"closed": False, "closed_at": None, "closes_at": None, "reopened_until": None}
    if not settings["enabled"]:
        return open_state
    if settings["group_ids"] is not None and group_id not in settings["group_ids"]:
        return open_state
    if settings["skip_pinned"] and pinned:
        return open_state
    due = max(int(last_turn_at) + settings["close_after_days"] * DAY_SECONDS, int(reopened_until or 0))
    reopened = reopened_until if reopened_until and reopened_until > now else None
    if now >= due:
        return {"closed": True, "closed_at": due, "closes_at": None, "reopened_until": None}
    return {"closed": False, "closed_at": None, "closes_at": due, "reopened_until": reopened}


def closed_message(session_id: str, days: int) -> str:
    return (
        f"Session {session_id} is closed: it had no new exchange for {days} days. "
        "You can still read it, but nothing can be added to it. "
        "Ask the user to reopen it in Bridge, or create a new session and continue there."
    )


# ---------- the prompt ----------

def target_chars(settings: dict[str, Any], transcript: str) -> int:
    """A fifth of the session, between the floor and the owner's setting."""
    share = int(len(transcript) * SUMMARY_SHARE) // 50 * 50
    return max(MIN_SUMMARY_CHARS, min(settings["max_chars"], share))


def compose_prompt(settings: dict[str, Any], transcript: str) -> str:
    """The base prompt and the owner's instructions as one message, then Bridge's rules and the transcript."""
    parts = [BASE_PROMPT]
    if settings["instructions"]:
        parts.append(settings["instructions"])
    parts.append(f"{FORMAT_RULES} Aim to keep it under {target_chars(settings, transcript)} characters.")
    parts.append(f"Transcript:\n<<<\n{transcript}\n>>>")
    return "\n\n".join(parts)


def prompt_hash(settings: dict[str, Any]) -> str:
    source = json.dumps([BASE_PROMPT, settings["instructions"], settings["max_chars"]], ensure_ascii=False)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def render_transcript(exchanges: list[Any], *, max_chars: int = TRANSCRIPT_MAX_CHARS) -> str:
    """Every kept exchange in order; a transcript too long keeps its opening and its end."""
    blocks = []
    for number, exchange in enumerate(exchanges, start=1):
        answer = "" if exchange.assistant_masked_at is not None else exchange.assistant_response
        blocks.append(
            f"Turn {number}\nHUMAN:\n{exchange.user_message}\n{exchange.model_name.upper()}:\n{answer}".rstrip()
        )
    text = "\n\n".join(blocks)
    if len(text) <= max_chars:
        return text
    head_budget = max_chars // 3
    head, tail = [], []
    used = 0
    for block in blocks:
        if used + len(block) > head_budget:
            break
        head.append(block)
        used += len(block) + 2
    for block in reversed(blocks[len(head):]):
        if used + len(block) > max_chars - 80:
            break
        tail.insert(0, block)
        used += len(block) + 2
    skipped = len(blocks) - len(head) - len(tail)
    return "\n\n".join([*head, f"[… {skipped} turns in the middle left out for length …]", *tail])


# ---------- runtime ----------

class SessionSummaries:
    def __init__(self, store: Store, codex: Any | None, *, fallback_model: Callable[[], str]):
        self.store = store
        self.codex = codex
        self._fallback_model = fallback_model
        self._task: asyncio.Task[None] | None = None
        self._single: set[asyncio.Task[None]] = set()
        self._in_flight: set[str] = set()

    # ----- settings -----

    def settings(self) -> dict[str, Any]:
        return read_settings(self.store.get_app_setting(SUMMARY_SETTINGS_KEY))

    def save_settings(self, payload: Any) -> dict[str, Any]:
        settings = validate_settings(payload, self.settings())
        self.store.set_app_setting(SUMMARY_SETTINGS_KEY, json.dumps(settings, ensure_ascii=False))
        return settings

    def resolved_codex_model(self) -> str:
        # Imported here: session_titles imports the store, which imports this module.
        from app.session_titles import newest_luna

        chosen = self.settings()["codex_model"]
        if chosen:
            return chosen
        raw = self.store.get_app_setting(CODEX_MODELS_SETTING_KEY)
        try:
            catalog = json.loads(raw) if raw else {}
        except ValueError:
            catalog = {}
        models = catalog.get("models") if isinstance(catalog, dict) else None
        listed = [model for model in models or [] if isinstance(model, dict) and model.get("model")]
        return newest_luna(listed) or self._fallback_model()

    def payload(self, *, now: int | None = None) -> dict[str, Any]:
        current = int(now if now is not None else time.time())
        settings = self.settings()
        counts = {"closed": 0, "summarized": 0, "waiting": 0, "failed": 0}
        for state in self.states(now=current).values():
            if not state["closed"]:
                continue
            counts["closed"] += 1
            status = (state["summary"] or {}).get("status")
            if status == "done" and not state["summary"]["outdated"]:
                counts["summarized"] += 1
            elif status == "failed":
                counts["failed"] += 1
            else:
                counts["waiting"] += 1
        return {
            **settings,
            "base_prompt": BASE_PROMPT,
            "format_rules": FORMAT_RULES,
            "close_after_days_choices": list(CLOSE_AFTER_DAYS_CHOICES),
            "max_chars_range": [MIN_SUMMARY_CHARS, MAX_SUMMARY_CHARS],
            "instructions_max_chars": INSTRUCTIONS_MAX_CHARS,
            "resolved_codex_model": self.resolved_codex_model(),
            "codex_configured": self.codex is not None,
            "running": self.running(),
            "counts": counts,
        }

    # ----- state -----

    def _state(self, settings: dict[str, Any], row: dict[str, Any], summary: dict[str, Any] | None, now: int) -> dict[str, Any]:
        state = close_state(
            settings,
            last_turn_at=row["last_turn_at"],
            pinned=bool(row["pinned"]),
            group_id=row["group_id"],
            reopened_until=row.get("reopened_until"),
            now=now,
        )
        view = self._summary_view(summary, row)
        reopened_until = row.get("reopened_until") or 0
        # summary = closed: a current summary closes the session until the owner reopens it,
        # also while it is being rewritten or after a rewrite failed, since the old text still stands
        if not state["closed"] and view and view["content"] and not view["outdated"] and reopened_until <= now:
            state = {
                "closed": True,
                "closed_at": max(int(view["written_at"] or now), int(reopened_until)),
                "closes_at": None,
                "reopened_until": None,
            }
        return {
            **state,
            "close_after_days": settings["close_after_days"],
            "card_color": settings["card_color"],
            "summary": view,
        }

    @staticmethod
    def _summary_view(summary: dict[str, Any] | None, row: dict[str, Any]) -> dict[str, Any] | None:
        if summary is None:
            return None
        last = row.get("last_exchange_id")
        # judged on the text kept, so a summary being rewritten still counts until the new one lands
        outdated = bool(summary["content"]) and last is not None and (summary["upto_exchange_id"] or 0) < last
        return {**summary, "outdated": outdated}

    def states(self, *, now: int | None = None) -> dict[str, dict[str, Any]]:
        current = int(now if now is not None else time.time())
        settings = self.settings()
        summaries = self.store.list_session_summaries()
        return {
            row["session_id"]: self._state(settings, row, summaries.get(row["session_id"]), current)
            for row in self.store.list_session_activity()
        }

    def state_for(self, session_id: str, *, now: int | None = None) -> dict[str, Any] | None:
        row = self.store.get_session_activity(session_id)
        if row is None:
            return None
        current = int(now if now is not None else time.time())
        return self._state(self.settings(), row, self.store.get_session_summary(session_id), current)

    def closed_error(self, session_id: str) -> str | None:
        """The message a model gets when it tries to add to a closed session, else None."""
        state = self.state_for(session_id)
        if state is None or not state["closed"]:
            return None
        return closed_message(session_id, state["close_after_days"])

    def reopen(self, session_id: str, *, now: int | None = None) -> dict[str, Any]:
        current = int(now if now is not None else time.time())
        state = self.state_for(session_id, now=current)
        if state is None:
            raise SummaryError(f"Unknown session_id: {session_id}", code="session_unknown")
        if not state["closed"]:
            raise SummaryError("This session is not closed.", code="not_closed")
        # summary = closed, so reopening drops the summary; the next close writes a new one
        until = current + state["close_after_days"] * DAY_SECONDS
        self.store.set_session_reopened_until(session_id, until)
        self.store.delete_session_summary(session_id)
        return self.state_for(session_id, now=current) or state

    # ----- the queue -----

    def _in_scope(self, settings: dict[str, Any], group_id: str, sensitive: dict[str, bool]) -> bool:
        return settings["include_sensitive"] or not sensitive.get(group_id, False)

    def candidates(self, *, now: int | None = None) -> list[str]:
        """Closed sessions whose summary is missing or behind, oldest close first. Failed ones wait for Regenerate."""
        current = int(now if now is not None else time.time())
        settings = self.settings()
        sensitive = {group["group_id"]: bool(group.get("is_sensitive")) for group in self.store.list_session_groups()}
        summaries = self.store.list_session_summaries()
        due: list[tuple[int, str]] = []
        for row in self.store.list_session_activity():
            if not row["exchange_count"] or not self._in_scope(settings, row["group_id"], sensitive):
                continue
            state = self._state(settings, row, summaries.get(row["session_id"]), current)
            if not state["closed"]:
                continue
            summary = state["summary"]
            if summary is None or summary["status"] in ("queued", "running") or summary["outdated"]:
                due.append((state["closed_at"] or 0, row["session_id"]))
        return [session_id for _, session_id in sorted(due)]

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def recover(self) -> None:
        """A summary cut off by a restart goes back to the queue."""
        self.store.requeue_running_summaries()

    async def tick(self, *, now: int | None = None) -> bool:
        """Start working through the queue when there is one; returns True when it started."""
        if self.running() or self.codex is None:
            return False
        settings = await asyncio.to_thread(self.settings)
        if not settings["enabled"] or not (await asyncio.to_thread(self.store.get_token_features))["enabled"]:
            return False
        if not await asyncio.to_thread(self.candidates, now=now):
            return False
        self._task = asyncio.create_task(self._drain())
        return True

    async def _drain(self) -> None:
        failures = 0
        tried: set[str] = set()
        try:
            while True:
                if not (await asyncio.to_thread(self.store.get_token_features))["enabled"]:
                    return
                if not (await asyncio.to_thread(self.settings))["enabled"]:
                    return
                queue = [session_id for session_id in await asyncio.to_thread(self.candidates) if session_id not in tried]
                if not queue:
                    return
                session_id = queue[0]
                tried.add(session_id)
                try:
                    ok = await self._write(session_id)
                except CodexNotReady as exc:
                    logger.info("Summaries wait for the next check: %s", exc)
                    return
                failures = 0 if ok else failures + 1
                if failures >= FAILURE_LIMIT:
                    logger.info("Summaries stopped after %s failures in a row", FAILURE_LIMIT)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Summary queue stopped on an unexpected error")

    def regenerate(self, session_id: str) -> dict[str, Any]:
        """Write the summary now, whatever the Runs models switch says. On an open session this closes it."""
        if self.codex is None:
            raise SummaryError("Codex App Server is not configured.", code="codex_unavailable")
        state = self.state_for(session_id)
        if state is None:
            raise SummaryError(f"Unknown session_id: {session_id}", code="session_unknown")
        # An open session can be summarized by hand too; once the summary is written, it is closed.
        if session_id in self._in_flight:
            raise SummaryError("This summary is being written right now.", code="summary_running")
        self.store.queue_session_summary(session_id)
        task = asyncio.get_running_loop().create_task(self._write_quietly(session_id))
        self._single.add(task)
        task.add_done_callback(self._single.discard)
        return self.state_for(session_id) or state

    async def _write_quietly(self, session_id: str) -> None:
        try:
            await self._write(session_id)
        except CodexNotReady as exc:
            await asyncio.to_thread(self.store.fail_session_summary, session_id, str(exc))
        except Exception:
            logger.exception("Summary of %s failed", session_id)

    async def _write(self, session_id: str) -> bool:
        """Write one summary. Returns False when it failed; raises CodexNotReady to leave it queued."""
        if session_id in self._in_flight:
            return True
        self._in_flight.add(session_id)
        try:
            settings = await asyncio.to_thread(self.settings)
            exchanges = await asyncio.to_thread(self.store.list_exchanges, session_id)
            if not exchanges:
                await asyncio.to_thread(self.store.fail_session_summary, session_id, "The session has no exchanges.")
                return False
            model = await asyncio.to_thread(self.resolved_codex_model)
            transcript = render_transcript(exchanges)
            prompt = compose_prompt(settings, transcript)
            await asyncio.to_thread(self.store.start_session_summary, session_id, model)
            try:
                raw = await self.codex.extract_structured(
                    prompt,
                    model=model,
                    effort=settings["codex_effort"],
                    output_schema=json.loads(json.dumps(SUMMARY_OUTPUT_SCHEMA)),
                    timeout_seconds=SUMMARY_TIMEOUT_SECONDS,
                )
            except CodexUnavailableError as exc:
                await asyncio.to_thread(self.store.queue_session_summary, session_id)
                raise CodexNotReady(str(exc) or "Codex App Server is unavailable.") from exc
            except ValueError as exc:
                # The client raises ValueError when every conversation slot is taken.
                if "Codex" in str(exc) and ("active" in str(exc) or "in progress" in str(exc)):
                    await asyncio.to_thread(self.store.queue_session_summary, session_id)
                    raise CodexNotReady("Codex is busy. Try again in a moment.") from exc
                raise
            content = str(raw.get("summary") or "").strip()
            if not content:
                await asyncio.to_thread(self.store.fail_session_summary, session_id, "Codex returned an empty summary.")
                return False
            tokens = len(prompt) // CHARS_PER_TOKEN + PROMPT_OVERHEAD_TOKENS
            await asyncio.to_thread(
                lambda: self.store.finish_session_summary(
                    session_id,
                    content,
                    upto_exchange_id=exchanges[-1].exchange_id,
                    exchange_count=len(exchanges),
                    model=model,
                    prompt_hash=prompt_hash(settings),
                    estimated_tokens=tokens,
                )
            )
            return True
        except CodexNotReady:
            raise
        except (CodexAppServerError, ValueError) as exc:
            await asyncio.to_thread(self.store.fail_session_summary, session_id, str(exc) or exc.__class__.__name__)
            return False
        finally:
            self._in_flight.discard(session_id)

    async def wait(self) -> None:
        tasks = [task for task in (self._task, *self._single) if task is not None and not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        tasks = [task for task in (self._task, *self._single) if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
