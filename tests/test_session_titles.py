from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.codex_app_server import CodexTimeoutError, CodexUnavailableError
from app.session_titles import (
    TITLE_SETTINGS_KEY,
    SessionTitleRuntime,
    TitleError,
    newest_luna,
)
from app.storage import Store


class FakeCodex:
    def __init__(self, outcomes: list[Any]):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def extract_structured(self, message: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"message": message, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"title": outcome}


def build(tmp_path, codex: Any, *, api_key: bool = False, api_title: str = "Tytuł z API"):
    store = Store(tmp_path / "bridge.sqlite3")
    api_calls: list[str] = []

    def api_suggest(message: str, max_words: int) -> str:
        api_calls.append(message)
        return api_title

    runtime = SessionTitleRuntime(
        store,
        codex,
        api_configured=lambda: api_key,
        api_suggest=api_suggest,
        fallback_model=lambda: "gpt-5.6-sol",
    )
    runtime.busy_wait_seconds = 0
    return store, runtime, api_calls


def new_session(store: Store, session_id: str = "s1", *, group_id: str = "") -> None:
    store.create_session(session_id, "20261019 placeholder", title_is_auto=True, group_id=group_id)


async def drain(runtime: SessionTitleRuntime) -> None:
    while runtime._tasks:
        await asyncio.gather(*tuple(runtime._tasks))


def test_newest_luna_prefers_highest_version_then_catalog_default():
    models = [
        {"model": "gpt-5.6-sol", "is_default": True},
        {"model": "gpt-5.5-luna"},
        {"model": "gpt-5.10-luna"},
        {"model": "gpt-5.6-luna"},
    ]
    assert newest_luna(models) == "gpt-5.10-luna"
    assert newest_luna([{"model": "gpt-5.6-sol", "is_default": True}]) == "gpt-5.6-sol"
    assert newest_luna([]) == ""


def test_turning_on_records_the_moment_and_off_clears_it(tmp_path):
    _, runtime, _ = build(tmp_path, None)
    on = runtime.save_settings({"auto_enabled": True})
    assert on["enabled_since"] is not None
    again = runtime.save_settings({"codex_effort": "medium"})
    assert again["enabled_since"] == on["enabled_since"]
    off = runtime.save_settings({"auto_enabled": False})
    assert off["enabled_since"] is None
    with pytest.raises(TitleError):
        runtime.save_settings({"codex_model": "bad model!"})
    with pytest.raises(TitleError):
        runtime.save_settings({"unknown": True})


def test_new_session_is_named_by_codex_after_its_first_exchange(tmp_path):
    codex = FakeCodex(["Zastosowania AI w firmie"])
    store, runtime, api_calls = build(tmp_path, codex)
    store.set_app_setting(
        "codex.models.catalog", json.dumps({"models": [{"model": "gpt-5.5-luna"}, {"model": "gpt-5.6-luna"}]})
    )
    runtime.save_settings({"auto_enabled": True})
    new_session(store)
    store.save_exchange("s1", "Claude", "Sesja o AI i jego zastosowaniach w firmie", "OK")

    async def scenario():
        runtime.notify_exchange_saved("s1")
        await drain(runtime)

    asyncio.run(scenario())
    session = store.get_session("s1")
    assert session.title == "Zastosowania AI w firmie"
    assert session.title_source == "auto"
    assert codex.calls[0]["model"] == "gpt-5.6-luna"
    assert codex.calls[0]["effort"] == "low"
    assert "Sesja o AI" in codex.calls[0]["message"]
    assert api_calls == []


def test_sessions_from_before_switching_on_are_left_alone(tmp_path):
    codex = FakeCodex(["Nie powinno się zdarzyć"])
    store, runtime, _ = build(tmp_path, codex)
    new_session(store)
    store.save_exchange("s1", "Claude", "Stara sesja", "OK")
    runtime.save_settings({"auto_enabled": True})
    settings = runtime.get_settings()
    settings["enabled_since"] = store.get_session("s1").created_at + 1
    store.set_app_setting(TITLE_SETTINGS_KEY, json.dumps(settings))
    assert runtime.auto_candidate("s1") is None
    assert codex.calls == []


def test_off_master_switch_sensitive_group_and_named_sessions_are_skipped(tmp_path):
    store, runtime, _ = build(tmp_path, FakeCodex([]))
    runtime.save_settings({"auto_enabled": True})
    private = store.create_session_group("Prywatne", "#b4553f", "folder")
    store.update_session_group(private.group_id, is_sensitive=True)
    new_session(store, "secret", group_id=private.group_id)
    store.save_exchange("secret", "Claude", "Tajne", "OK")
    assert runtime.auto_candidate("secret") is None

    store.create_session("named", "Mój tytuł")
    store.save_exchange("named", "Claude", "Coś", "OK")
    assert runtime.auto_candidate("named") is None

    new_session(store, "s1")
    store.save_exchange("s1", "Claude", "Pierwsza", "OK")
    assert runtime.auto_candidate("s1") == "Pierwsza"
    store.set_token_features({"enabled": False})
    assert runtime.auto_candidate("s1") is None


def test_attempts_stop_after_the_first_few_exchanges(tmp_path):
    store, runtime, _ = build(tmp_path, FakeCodex([]))
    runtime.save_settings({"auto_enabled": True})
    new_session(store)
    for turn in range(3):
        store.save_exchange("s1", "Claude", f"Pytanie {turn}", "OK")
    assert runtime.auto_candidate("s1") == "Pytanie 0"
    store.save_exchange("s1", "Claude", "Pytanie 3", "OK")
    assert runtime.auto_candidate("s1") is None


def test_failed_codex_falls_back_to_api_only_when_allowed(tmp_path):
    codex = FakeCodex([CodexTimeoutError("slow"), CodexTimeoutError("slow")])
    store, runtime, api_calls = build(tmp_path, codex, api_key=True)
    runtime.save_settings({"auto_enabled": True})
    new_session(store, "s1")
    store.save_exchange("s1", "Claude", "Pierwsza", "OK")

    async def run(session_id: str):
        runtime.notify_exchange_saved(session_id)
        await drain(runtime)

    asyncio.run(run("s1"))
    assert store.get_session("s1").title_source == "derived"
    assert api_calls == []

    runtime.save_settings({"api_fallback": True})
    asyncio.run(run("s1"))
    assert store.get_session("s1").title == "Tytuł z API"
    assert api_calls == ["Pierwsza"]


def test_busy_codex_is_waited_for_not_replaced_by_the_api(tmp_path):
    busy = ValueError("Too many Codex conversations are active.")
    codex = FakeCodex([busy, busy, "Po czekaniu"])
    store, runtime, api_calls = build(tmp_path, codex, api_key=True)
    runtime.save_settings({"auto_enabled": True, "api_fallback": True})
    new_session(store)
    store.save_exchange("s1", "Claude", "Pierwsza", "OK")

    async def scenario():
        runtime.notify_exchange_saved("s1")
        await drain(runtime)

    asyncio.run(scenario())
    assert store.get_session("s1").title == "Po czekaniu"
    assert api_calls == []
    assert len(codex.calls) == 3


def test_a_manual_rename_meanwhile_wins(tmp_path):
    store, _, _ = build(tmp_path, None)
    new_session(store)
    store.set_session_title("s1", "Ręcznie")
    assert store.set_auto_session_title("s1", "Z automatu") is False
    assert store.get_session("s1").title == "Ręcznie"


def test_rename_button_uses_codex_then_api(tmp_path):
    codex = FakeCodex([CodexUnavailableError("down")])
    store, runtime, api_calls = build(tmp_path, codex, api_key=True)
    store.create_session("s1", "Cokolwiek")
    store.save_exchange("s1", "Claude", "Pierwsza", "OK")
    session, provider = asyncio.run(runtime.rename_now("s1"))
    assert (session.title, session.title_source, provider) == ("Tytuł z API", "ai", "api")
    assert api_calls == ["Pierwsza"]


def test_rename_button_without_codex_or_key_says_what_to_set_up(tmp_path):
    store, runtime, _ = build(tmp_path, FakeCodex([CodexUnavailableError("down")]))
    store.create_session("s1", "Cokolwiek")
    store.save_exchange("s1", "Claude", "Pierwsza", "OK")
    with pytest.raises(TitleError) as raised:
        asyncio.run(runtime.rename_now("s1"))
    assert raised.value.code == "not_configured"
