"""Automatic chapters: the batch engine behind Fill now and the schedule."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.chapter_automation import (
    AutomationError,
    ChapterAutomation,
    default_schedule,
    next_run_at,
    validate_schedule,
)
from app.codex_app_server import CodexUnavailableError
from app.storage import Store
from app.strand_runtime import StrandRuntime


class ChapterCodex:
    """Proposes one chapter covering every exchange it is shown."""

    def __init__(self, fail: bool = False, gate: asyncio.Event | None = None):
        self.fail = fail
        self.gate = gate
        self.calls: list[str] = []

    async def extract_structured(self, message: str, *, model: str, effort: str, output_schema: dict) -> dict[str, Any]:
        self.calls.append(message)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise CodexUnavailableError("down")
        ids = [int(line.split()[1]) for line in message.splitlines() if line.startswith("Exchange ")]
        new_section = message.rsplit("NEW EXCHANGES\n", 1)
        if len(new_section) == 2:
            new_ids = [int(line.split()[1]) for line in new_section[1].splitlines() if line.startswith("Exchange ")]
            return {"extend": [], "new": [{"title": "Nowe", "ranges": [{"start": new_ids[0], "end": new_ids[-1]}]}]}
        return {"strands": [{"title": "Całość", "ranges": [{"start": ids[0], "end": ids[-1]}]}]}


def build(tmp_path, codex: Any) -> tuple[Store, ChapterAutomation, dict[str, list[str]]]:
    store = Store(tmp_path / "bridge.sqlite3")
    work = store.create_session_group("Praca", "#4f7cff", "folder")
    private = store.create_session_group("Prywatne", "#b4553f", "folder")
    store.update_session_group(private.group_id, is_sensitive=True)
    sessions: dict[str, list[str]] = {work.group_id: [], private.group_id: []}
    for group_id, count in ((work.group_id, 3), (private.group_id, 1)):
        for index in range(count):
            session_id = f"20261016-1000{index}{len(sessions[group_id])}-{group_id}-{index}"
            store.create_session(session_id, f"{group_id} {index}")
            store.set_session_group(session_id, group_id)
            for turn in range(2):
                store.save_exchange(session_id, "Claude", f"Pytanie {turn}", f"Odpowiedź {turn}")
            sessions[group_id].append(session_id)
    automation = ChapterAutomation(store, StrandRuntime(store, codex))
    return store, automation, sessions


def run_batch(automation: ChapterAutomation) -> dict[str, Any]:
    async def go():
        await automation.start()
        await automation.wait()
    asyncio.run(go())
    return automation.batch()


# ---------- schedule ----------

def test_interval_schedule_counts_from_the_moment_it_was_switched_on():
    schedule = validate_schedule({"enabled": True, "mode": "interval", "interval_hours": 6}, default_schedule(), now=1_000)
    assert next_run_at(schedule, "UTC", now=1_000) == 1_000 + 6 * 3600
    schedule["last_run_at"] = 50_000
    assert next_run_at(schedule, "UTC", now=50_001) == 50_000 + 6 * 3600


def test_daily_and_weekly_schedules_use_the_display_timezone():
    zone = ZoneInfo("Europe/Warsaw")
    switched_on = int(datetime(2026, 10, 16, 12, 0, tzinfo=zone).timestamp())  # a Friday
    daily = validate_schedule({"enabled": True, "mode": "daily", "time": "03:00"}, default_schedule(), now=switched_on)
    assert datetime.fromtimestamp(next_run_at(daily, "Europe/Warsaw", now=switched_on), tz=zone) == datetime(2026, 10, 17, 3, 0, tzinfo=zone)
    weekly = validate_schedule({"enabled": True, "mode": "weekly", "time": "21:30", "days": [0]}, default_schedule(), now=switched_on)
    assert datetime.fromtimestamp(next_run_at(weekly, "Europe/Warsaw", now=switched_on), tz=zone) == datetime(2026, 10, 19, 21, 30, tzinfo=zone)


def test_disabled_schedule_has_no_next_run_and_bad_values_are_rejected():
    assert next_run_at(default_schedule(), "UTC", now=1) is None
    for payload in ({"mode": "hourly"}, {"interval_hours": 5}, {"time": "25:00"}, {"days": [7]}, {"days": []}):
        with pytest.raises(AutomationError):
            validate_schedule(payload, default_schedule(), now=1)


# ---------- batches ----------

def test_fill_splits_every_session_in_scope_and_skips_sensitive_groups(tmp_path):
    codex = ChapterCodex()
    store, automation, sessions = build(tmp_path, codex)
    work, private = list(sessions)

    batch = run_batch(automation)

    assert batch["status"] == "done" and batch["processed"] == 3 and batch["failed"] == 0
    assert all(store.chapter_state(sid)["state"] == "complete" for sid in sessions[work])
    assert store.chapter_state(sessions[private][0])["state"] == "none"
    assert automation.history()[0]["processed"] == 3


def test_including_sensitive_groups_and_choosing_groups(tmp_path):
    store, automation, sessions = build(tmp_path, ChapterCodex())
    work, private = list(sessions)
    automation.set_fill_settings({"group_ids": [private], "include_sensitive": True})

    batch = run_batch(automation)

    assert batch["processed"] == 1
    assert store.chapter_state(sessions[private][0])["state"] == "complete"
    assert all(store.chapter_state(sid)["state"] == "none" for sid in sessions[work])


def test_without_auto_accept_proposals_wait_for_review(tmp_path):
    store, automation, sessions = build(tmp_path, ChapterCodex())
    automation.set_fill_settings({"auto_accept": False})
    run_batch(automation)
    work = list(sessions)[0]
    assert all(store.chapter_state(sid)["state"] == "review" for sid in sessions[work])
    assert automation.progress()["review"] == 3


def test_new_exchanges_are_updated_on_the_next_fill(tmp_path):
    codex = ChapterCodex()
    store, automation, sessions = build(tmp_path, codex)
    run_batch(automation)
    sid = sessions[list(sessions)[0]][0]
    store.save_exchange(sid, "Claude", "Dalej", "Odpowiedź")
    assert store.chapter_state(sid)["state"] == "behind"

    batch = run_batch(automation)

    assert batch["processed"] == 1 and "NEW EXCHANGES" in codex.calls[-1]
    assert store.chapter_state(sid)["state"] == "complete"


def test_pause_finishes_the_current_session_then_stops(tmp_path):
    gate = asyncio.Event()
    codex = ChapterCodex(gate=gate)
    store, automation, sessions = build(tmp_path, codex)

    async def go():
        await automation.start()
        while len(codex.calls) < 1:
            await asyncio.sleep(0.01)
        automation.pause()
        assert automation.batch()["status"] == "pausing"
        gate.set()
        await automation.wait()
    asyncio.run(go())

    batch = automation.batch()
    assert batch["status"] == "paused" and batch["processed"] == 1 and len(codex.calls) == 1
    assert automation.progress()["done"] == 1

    gate.set()
    resumed = run_batch(automation)
    assert resumed["status"] == "done" and resumed["processed"] == 3


def test_three_codex_errors_in_a_row_pause_the_fill(tmp_path):
    store, automation, _ = build(tmp_path, ChapterCodex(fail=True))
    batch = run_batch(automation)
    assert batch["status"] == "paused" and batch["failed"] == 3
    assert "3 Codex errors in a row" in batch["last_error"]


def test_restart_leaves_a_running_batch_paused(tmp_path):
    store, automation, _ = build(tmp_path, ChapterCodex())
    automation._write("chapters.batch", {"status": "running", "processed": 2})
    automation.recover()
    assert automation.batch()["status"] == "paused" and automation.batch()["processed"] == 2


def test_schedule_tick_starts_when_due_but_not_while_paused(tmp_path):
    store, automation, _ = build(tmp_path, ChapterCodex())
    automation.set_schedule({"enabled": True, "mode": "interval", "interval_hours": 3}, now=1_000)

    async def tick(now):
        started = await automation.tick(now=now)
        await automation.wait()
        return started

    assert asyncio.run(tick(1_000 + 3 * 3600 - 1)) is False
    assert asyncio.run(tick(1_000 + 3 * 3600)) is True
    assert automation.batch()["trigger"] == "schedule"

    automation._write("chapters.batch", {"status": "paused"})
    assert asyncio.run(tick(1_000 + 9 * 3600)) is False


def test_progress_estimates_tokens_for_remaining_work_only(tmp_path):
    store, automation, sessions = build(tmp_path, ChapterCodex())
    before = automation.progress()
    assert before["total"] == 3 and before["todo"] == 3 and before["estimated_tokens"] > 0
    run_batch(automation)
    after = automation.progress()
    assert after["done"] == 3 and after["estimated_tokens"] == 0
    assert "Codex does not report real usage" in automation.overview()["estimate_note"]


def test_sessions_that_failed_are_retried_after_resume(tmp_path):
    codex = ChapterCodex(fail=True)
    store, automation, _ = build(tmp_path, codex)
    assert run_batch(automation)["status"] == "paused"
    codex.fail = False
    resumed = run_batch(automation)
    assert resumed["status"] == "done" and automation.progress()["done"] == 3
