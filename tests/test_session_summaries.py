from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from app.codex_app_server import CodexTimeoutError, CodexUnavailableError
from app.session_summaries import (
    BASE_PROMPT,
    DAY_SECONDS,
    SUMMARY_SETTINGS_KEY,
    SessionSummaries,
    SummaryError,
    close_state,
    compose_prompt,
    default_settings,
    render_transcript,
    validate_settings,
)
from app.storage import Store

NOW = 1_800_000_000


class FakeCodex:
    def __init__(self, outcomes: list[Any]):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def extract_structured(self, message: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"message": message, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"summary": outcome}


def build(tmp_path, codex: Any = None, **settings: Any) -> tuple[Store, SessionSummaries]:
    store = Store(tmp_path / "bridge.sqlite3")
    runtime = SessionSummaries(store, codex, fallback_model=lambda: "gpt-5.6-sol")
    if settings:
        store.set_app_setting(SUMMARY_SETTINGS_KEY, json.dumps({**default_settings(), **settings}))
    return store, runtime


def session_with_turns(store: Store, session_id: str, *, days_ago: float, turns: int = 2, group_id: str = "") -> None:
    store.create_session(session_id, f"Session {session_id}", group_id=group_id)
    for index in range(turns):
        store.save_exchange(session_id, "Claude", f"Question {index}", f"Answer {index}")
    stamp = int(time.time() - days_ago * DAY_SECONDS)
    with store._connect() as conn:
        conn.execute(
            "UPDATE exchanges SET created_at = ?, assistant_created_at = ? WHERE session_id = ?",
            (stamp, stamp, session_id),
        )
        conn.execute("UPDATE sessions SET created_at = ? WHERE session_id = ?", (stamp, session_id))


# ---------- the rule ----------

def rule(**overrides: Any) -> dict[str, Any]:
    settings = {**default_settings(), "enabled": True, **overrides}
    return settings


def test_close_rule_follows_last_turn_window_pin_and_scope():
    old = NOW - 31 * DAY_SECONDS
    recent = NOW - 2 * DAY_SECONDS
    assert close_state(rule(), last_turn_at=old, pinned=False, group_id="g", reopened_until=None, now=NOW)["closed"]
    state = close_state(rule(), last_turn_at=recent, pinned=False, group_id="g", reopened_until=None, now=NOW)
    assert not state["closed"] and state["closes_at"] == recent + 30 * DAY_SECONDS
    assert not close_state(rule(enabled=False), last_turn_at=old, pinned=False, group_id="g", reopened_until=None, now=NOW)["closed"]
    assert not close_state(rule(), last_turn_at=old, pinned=True, group_id="g", reopened_until=None, now=NOW)["closed"]
    assert close_state(rule(skip_pinned=False), last_turn_at=old, pinned=True, group_id="g", reopened_until=None, now=NOW)["closed"]
    assert not close_state(rule(group_ids=["other"]), last_turn_at=old, pinned=False, group_id="g", reopened_until=None, now=NOW)["closed"]


def test_reopen_holds_until_its_time_then_closes_again():
    old = NOW - 90 * DAY_SECONDS
    reopened = close_state(rule(), last_turn_at=old, pinned=False, group_id="g", reopened_until=NOW + 5, now=NOW)
    assert not reopened["closed"] and reopened["reopened_until"] == NOW + 5 and reopened["closes_at"] == NOW + 5
    lapsed = close_state(rule(), last_turn_at=old, pinned=False, group_id="g", reopened_until=NOW - 5, now=NOW)
    assert lapsed["closed"] and lapsed["closed_at"] == NOW - 5


def test_settings_validation():
    current = default_settings()
    saved = validate_settings({"close_after_days": 7, "max_chars": 1200, "instructions": "  Lead with decisions. "}, current)
    assert saved["close_after_days"] == 7 and saved["max_chars"] == 1200 and saved["instructions"] == "Lead with decisions."
    assert validate_settings({"instructions": "x" * 8000}, current)["instructions"] == "x" * 8000
    for bad in ({"instructions": "x" * 8001}, {"close_after_days": 5}, {"max_chars": 10}, {"card_color": "blue"}, {"enabled": "yes"}, {"nope": 1}, {}):
        with pytest.raises(SummaryError):
            validate_settings(bad, current)


# ---------- the prompt ----------

def test_prompt_glues_base_and_instructions_then_rules_and_transcript():
    empty = compose_prompt(default_settings(), "Turn 1")
    assert empty.startswith(f"{BASE_PROMPT}\n\nWrite the summary in Markdown")
    assert "under 2000 characters" in empty and empty.endswith("<<<\nTurn 1\n>>>")
    extra = compose_prompt({**default_settings(), "instructions": "Lead with decisions.", "max_chars": 900}, "T")
    assert extra.startswith(f"{BASE_PROMPT}\n\nLead with decisions.\n\n") and "under 900 characters" in extra


def test_long_transcript_keeps_its_start_and_end(tmp_path):
    store, _ = build(tmp_path)
    store.create_session("s1", "Long")
    for index in range(40):
        store.save_exchange("s1", "Claude", f"Question {index} " + "x" * 200, f"Answer {index}")
    text = render_transcript(store.list_exchanges("s1"), max_chars=3000)
    assert len(text) <= 3000 and "Question 0 " in text and "Question 39 " in text and "left out for length" in text


def test_a_summary_over_the_target_length_is_kept_whole(tmp_path):
    long_text = ("Para one. " * 30) + "\n\n" + ("Para two. " * 60) + "\n\n## Stan na koniec\n\nOtwarte."
    codex = FakeCodex([f"  {long_text}\n"])
    store, runtime = build(tmp_path, codex, enabled=True, max_chars=300)
    session_with_turns(store, "old", days_ago=40)

    async def scenario():
        assert await runtime.tick()
        await runtime.wait()

    asyncio.run(scenario())
    assert "Aim to keep it under 300 characters." in codex.calls[0]["message"]
    assert runtime.state_for("old")["summary"]["content"] == long_text


# ---------- the runtime ----------

def test_states_reopen_and_closed_error(tmp_path):
    store, runtime = build(tmp_path, enabled=True)
    session_with_turns(store, "old", days_ago=40)
    session_with_turns(store, "new", days_ago=1)
    states = runtime.states()
    assert states["old"]["closed"] and not states["new"]["closed"]
    assert "is closed" in runtime.closed_error("old") and runtime.closed_error("new") is None
    reopened = runtime.reopen("old")
    assert not reopened["closed"] and reopened["reopened_until"] > time.time() + 29 * DAY_SECONDS
    assert runtime.closed_error("old") is None
    with pytest.raises(SummaryError):
        runtime.reopen("new")


def test_closing_off_never_blocks(tmp_path):
    store, runtime = build(tmp_path)
    session_with_turns(store, "old", days_ago=400)
    assert runtime.closed_error("old") is None and runtime.candidates() == []


def test_queue_writes_summaries_for_closed_sessions_only(tmp_path):
    codex = FakeCodex(["## Cel\n\nTest odczytu."])
    store, runtime = build(tmp_path, codex, enabled=True, instructions="Lead with decisions.")
    session_with_turns(store, "old", days_ago=40)
    session_with_turns(store, "new", days_ago=1)

    async def scenario():
        assert await runtime.tick()
        await runtime.wait()

    asyncio.run(scenario())
    assert len(codex.calls) == 1
    call = codex.calls[0]
    assert call["message"].startswith(f"{BASE_PROMPT}\n\nLead with decisions.") and "Question 1" in call["message"]
    assert call["model"] == "gpt-5.6-sol" and call["effort"] == "low"
    summary = runtime.state_for("old")["summary"]
    assert summary["status"] == "done" and summary["content"].startswith("## Cel") and summary["exchange_count"] == 2
    assert not summary["outdated"] and runtime.candidates() == []
    assert runtime.state_for("new")["summary"] is None


def test_queue_waits_for_runs_models_and_sensitive_groups(tmp_path):
    codex = FakeCodex([])
    store, runtime = build(tmp_path, codex, enabled=True)
    group = store.create_session_group("Private", "#b4553f", "folder")
    store.update_session_group(group.group_id, is_sensitive=True)
    session_with_turns(store, "secret", days_ago=40, group_id=group.group_id)
    assert runtime.candidates() == []
    assert runtime.state_for("secret")["closed"]

    session_with_turns(store, "plain", days_ago=40)
    store.set_token_features({"enabled": False})
    assert not asyncio.run(runtime.tick())


def test_failure_waits_for_regenerate_and_new_turns_make_it_outdated(tmp_path):
    codex = FakeCodex([CodexTimeoutError("Codex timed out."), "Second try.", "Third."])
    store, runtime = build(tmp_path, codex, enabled=True)
    session_with_turns(store, "old", days_ago=40)

    async def first():
        await runtime.tick()
        await runtime.wait()

    asyncio.run(first())
    failed = runtime.state_for("old")["summary"]
    assert failed["status"] == "failed" and "timed out" in failed["error"]
    assert runtime.candidates() == []

    async def regenerate():
        runtime.regenerate("old")
        await runtime.wait()

    asyncio.run(regenerate())
    assert runtime.state_for("old")["summary"]["content"] == "Second try."

    runtime.reopen("old")
    store.save_exchange("old", "Claude", "Later question", "Later answer")
    with store._connect() as conn:
        conn.execute("UPDATE exchanges SET created_at = 0, assistant_created_at = 0 WHERE session_id = 'old'")
    store.set_session_reopened_until("old", int(time.time()) - 1)
    assert runtime.state_for("old")["summary"]["outdated"]
    assert runtime.candidates() == ["old"]


def test_unavailable_codex_leaves_summaries_queued(tmp_path):
    codex = FakeCodex([CodexUnavailableError("Codex App Server is unavailable.")])
    store, runtime = build(tmp_path, codex, enabled=True)
    session_with_turns(store, "a", days_ago=40)
    session_with_turns(store, "b", days_ago=50)

    async def scenario():
        await runtime.tick()
        await runtime.wait()

    asyncio.run(scenario())
    assert len(codex.calls) == 1
    assert runtime.state_for("b")["summary"]["status"] == "queued"
    assert runtime.state_for("a")["summary"] is None
    assert runtime.candidates() == ["b", "a"]


def test_a_summary_written_by_hand_closes_an_open_session(tmp_path):
    codex = FakeCodex(["Trying the prompt."])
    store, runtime = build(tmp_path, codex, enabled=True, codex_model="gpt-5.6-sol", codex_effort="high")
    session_with_turns(store, "fresh", days_ago=1)

    async def scenario():
        runtime.regenerate("fresh")
        await runtime.wait()

    asyncio.run(scenario())
    state = runtime.state_for("fresh")
    assert state["closed"] and state["summary"]["content"] == "Trying the prompt."
    assert codex.calls[0]["model"] == "gpt-5.6-sol" and codex.calls[0]["effort"] == "high"
    assert "is closed" in runtime.closed_error("fresh") and runtime.candidates() == []

    # reopening lets models in again; a new exchange makes the summary outdated, so it stays open
    runtime.reopen("fresh")
    assert runtime.closed_error("fresh") is None
    store.save_exchange("fresh", "Claude", "One more", "Answer")
    store.set_session_reopened_until("fresh", int(time.time()) - 1)
    state = runtime.state_for("fresh")
    assert not state["closed"] and state["summary"]["outdated"]


def test_a_summary_being_rewritten_keeps_the_session_closed(tmp_path):
    store, runtime = build(tmp_path)
    session_with_turns(store, "s", days_ago=1)
    store.finish_session_summary(
        "s", "Old.", upto_exchange_id=store.list_exchanges("s")[-1].exchange_id,
        exchange_count=2, model="m", prompt_hash="h", estimated_tokens=1,
    )
    for step in (store.queue_session_summary, lambda sid: store.start_session_summary(sid, "m"),
                 lambda sid: store.fail_session_summary(sid, "timed out")):
        step("s")
        assert runtime.state_for("s")["closed"] and runtime.closed_error("s")


def test_a_summary_closes_even_with_automatic_closing_off(tmp_path):
    store, runtime = build(tmp_path)
    session_with_turns(store, "s", days_ago=1)
    store.finish_session_summary(
        "s", "Done.", upto_exchange_id=store.list_exchanges("s")[-1].exchange_id,
        exchange_count=2, model="m", prompt_hash="h", estimated_tokens=1,
    )
    assert runtime.state_for("s")["closed"] and runtime.closed_error("s")


def test_restart_requeues_a_running_summary(tmp_path):
    store, runtime = build(tmp_path, FakeCodex([]), enabled=True)
    session_with_turns(store, "old", days_ago=40)
    store.start_session_summary("old", "gpt-5.6-sol")
    runtime.recover()
    assert runtime.state_for("old")["summary"]["status"] == "queued"
    assert runtime.candidates() == ["old"]


# ---------- MCP and admin ----------

def test_models_can_read_but_not_write_a_closed_session(load_main):
    main = load_main()
    main.store.set_app_setting(SUMMARY_SETTINGS_KEY, json.dumps({**default_settings(), "enabled": True}))
    session_with_turns(main.store, "old", days_ago=40)
    main.store.finish_session_summary(
        "old", "**Done.**", upto_exchange_id=main.store.list_exchanges("old")[-1].exchange_id,
        exchange_count=2, model="m", prompt_hash="h", estimated_tokens=10,
    )

    with pytest.raises(ValueError, match="is closed"):
        main.save_exchange("old", "Claude", "More", "Answer")
    uploaded = main.upload_session_file("old", "note.md", "text")
    assert uploaded["ok"] is False and uploaded["error_code"] == "session_closed"
    overview = main.get_session_overview("old")
    assert overview["ok"] and overview["closed"] is True
    assert overview["summary"]["content"] == "**Done.**" and overview["summary"]["outdated"] is False
    assert main.get_session_transcript_chunk("old", 1)["ok"]

    main.admin.summaries.reopen("old")
    assert main.save_exchange("old", "Claude", "More", "Answer")["ok"]
    assert "closed" not in main.get_session_overview("old")


def test_admin_settings_reopen_and_session_payloads(admin_client):
    client, csrf = admin_client()
    headers = {"X-CSRF-Token": csrf}
    saved = client.put(
        "/admin/api/settings/summaries",
        json={"enabled": True, "close_after_days": 7, "card_color": "paper", "max_chars": 1500, "instructions": "Be brief."},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    summaries = saved.json()["settings"]["summaries"]
    assert summaries["enabled"] and summaries["close_after_days"] == 7 and summaries["card_color"] == "paper"
    assert summaries["base_prompt"] == BASE_PROMPT and summaries["instructions"] == "Be brief."
    assert client.put("/admin/api/settings/summaries", json={"max_chars": 5}, headers=headers).status_code == 400
    assert client.post("/admin/api/sessions/missing/reopen", headers=headers).status_code == 404
