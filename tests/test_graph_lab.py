from __future__ import annotations

import asyncio

from app.graph_runtime import GraphRuntime
from app.storage import Store


def test_lab_run_is_append_only_and_does_not_publish_production_analysis(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session("s1", "Lab session", "manual-context")
    store.save_exchange("s1", "model", "Panda is the evaluation subject.", "Confirmed.")
    store.set_graph_enabled(True)

    class Codex:
        async def extract_structured(self, message, **kwargs):
            return {"concepts": [{
                "canonical_name": "Panda",
                "type": "topic",
                "summary": "Evaluation subject.",
            }]}

    runtime = GraphRuntime(store, Codex(), worker_id="lab-test")
    result = asyncio.run(runtime.run_lab_once("s1", {}, actor="owner"))

    assert result["status"] == "completed"
    assert result["result"]["concepts"][0]["canonical_name"] == "Panda"
    assert store.get_graph_analysis("s1") is None
    [listed] = store.list_graph_lab_runs()
    assert listed["lab_run_id"] == result["lab_run_id"]
    assert listed["settings"]["model"] == "gpt-5.6-sol"


def test_lab_requires_graph_master_switch(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session("s1", "Lab session", "manual-context")
    store.save_exchange("s1", "model", "hello", "world")

    runtime = GraphRuntime(store, object(), worker_id="lab-test")

    try:
        asyncio.run(runtime.run_lab_once("s1", {}, actor="owner"))
    except ValueError as exc:
        assert "Graph is off" in str(exc)
    else:
        raise AssertionError("Lab must honor the Graph master switch")


def test_lab_honors_sensitive_scope_and_graph_off_cancels_active_run(tmp_path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "bridge.sqlite3")
        store.create_session_group("Private", "#f59e0b", "lock", "private")
        store.update_session_group("private", is_sensitive=True)
        store.create_session("secret", "Secret", "manual-context")
        store.set_session_group("secret", "private")
        store.save_exchange("secret", "model", "private", "content")
        store.set_graph_enabled(True)
        runtime = GraphRuntime(store, object(), worker_id="lab-test")
        try:
            await runtime.run_lab_once("secret", {}, actor="owner")
        except ValueError as exc:
            assert "sensitive-group policy" in str(exc)
        else:
            raise AssertionError("Sensitive sessions must be excluded by default")

        store.create_session("public", "Public", "manual-context")
        store.save_exchange("public", "model", "public", "content")
        started = asyncio.Event()

        class SlowCodex:
            async def extract_structured(self, message, **kwargs):
                started.set()
                await asyncio.Event().wait()

        runtime = GraphRuntime(store, SlowCodex(), worker_id="lab-test")
        run = await runtime.start_lab_run("public", {}, actor="owner")
        await started.wait()
        await runtime.set_enabled(False)
        finished = store.get_graph_lab_run(run["lab_run_id"])
        assert finished["status"] == "failed"
        assert finished["error_code"] == "interrupted"

    asyncio.run(scenario())
