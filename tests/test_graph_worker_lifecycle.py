from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.graph_runtime import GraphRuntime
from app.security import password_hash
from app.storage import Store


def _load_main(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIDGE_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "bridge.sqlite3"))
    monkeypatch.setenv("BRIDGE_OWNER_USERNAME", "owner")
    monkeypatch.setenv("BRIDGE_OWNER_PASSWORD_HASH", password_hash("secret-admin-password"))
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("BRIDGE_GRAPH_EXPERIMENTAL", "true")
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def _session(store: Store, session_id: str, created_at: int = 1_000) -> int:
    store.create_session(session_id, session_id, "manual-context")
    exchange = store.save_exchange(
        session_id, "model", f"SQLite backs Graph session {session_id}.",
        f"That is the implementation decision for {session_id}.",
    )
    with store._connect() as connection:
        connection.execute(
            "UPDATE exchanges SET created_at = ?, assistant_created_at = ? WHERE exchange_id = ?",
            (created_at, created_at, exchange.exchange_id),
        )
    return exchange.exchange_id


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "bridge.sqlite3")
    draft = store.unlock_graph_profile("owner")
    store.update_graph_draft({**draft, "inactivity_hours": 1}, "owner")
    store.activate_graph_draft("owner")
    _session(store, "s1")
    store.set_graph_enabled(True)
    return store


def _result(name: str = "SQLite Graph queue") -> dict:
    return {"concepts": [{
        "canonical_name": name,
        "type": "technology",
        "summary": "SQLite stores durable Graph jobs.",
    }]}


def test_graph_monitor_runs_once_for_process_without_mcp_traffic(tmp_path, monkeypatch):
    main = _load_main(tmp_path, monkeypatch)
    started = threading.Event()
    calls = []

    async def run_once():
        calls.append(True)
        started.set()
        await asyncio.Event().wait()

    async def close_codex():
        return None

    monkeypatch.setattr(main.graph_runtime, "run_once", run_once)
    monkeypatch.setattr(main.codex, "close", close_codex)

    with TestClient(main.app, base_url="http://127.0.0.1:8787") as client:
        assert started.wait(timeout=1)
        assert client.get("/healthz").status_code == 200
        assert client.get("/admin/graph", follow_redirects=False).status_code == 303
        time.sleep(0.05)
        assert len(calls) == 1

    assert main.mcp.settings.lifespan is None


def test_cancelled_runtime_releases_job_for_retry(tmp_path):
    store = _store(tmp_path)
    started = asyncio.Event()

    class Codex:
        async def extract_structured(self, message, **kwargs):
            started.set()
            await asyncio.Event().wait()

    async def run():
        task = asyncio.create_task(
            GraphRuntime(store, Codex(), worker_id="cancel").run_once(now=10_000)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    [job] = store.list_graph_jobs()
    assert job["status"] == "retryable_failed"
    assert job["error_code"] == "worker_interrupted"
    assert job["lease_owner"] is None
    assert job["lease_expires_at"] is None


def test_reset_cancels_active_job_without_stopping_runtime_and_requeues_all(tmp_path):
    store = _store(tmp_path)
    _session(store, "recent", created_at=9_900)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    class Codex:
        async def extract_structured(self, message, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                return _result("Fresh scan after reset")
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def run():
        runtime = GraphRuntime(store, Codex(), worker_id="reset")
        worker = asyncio.create_task(runtime.run_once(now=10_000))
        await started.wait()
        result = await runtime.reset_all(now=10_100)
        await worker
        await runtime.run_once(now=10_101)
        return result

    result = asyncio.run(run())

    assert result == {"deleted_jobs": 1, "deleted_extractions": 0, "queued_jobs": 2}
    assert cancelled.is_set()
    assert sorted(job["session_id"] for job in store.list_graph_jobs()) == ["recent", "s1"]
    assert sorted(job["status"] for job in store.list_graph_jobs()) == ["completed", "queued"]


def test_cancellation_waits_for_claim_and_releases_claimed_job(tmp_path, monkeypatch):
    store = _store(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_claim = store.claim_graph_job

    def blocked_claim(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=1)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(store, "claim_graph_job", blocked_claim)

    class Codex:
        async def extract_structured(self, message, **kwargs):
            raise AssertionError("Codex must not start after runtime cancellation.")

    async def run():
        task = asyncio.create_task(
            GraphRuntime(store, Codex(), worker_id="claim-cancel").run_once(now=10_000)
        )
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    [job] = store.list_graph_jobs()
    assert job["status"] == "retryable_failed"
    assert job["error_code"] == "worker_interrupted"
    assert job["lease_owner"] is None


def test_cancellation_during_context_lookup_releases_job(tmp_path, monkeypatch):
    store = _store(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_context = store.get_graph_job_context

    def blocked_context(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=1)
        return original_context(*args, **kwargs)

    monkeypatch.setattr(store, "get_graph_job_context", blocked_context)

    class Codex:
        async def extract_structured(self, message, **kwargs):
            raise AssertionError("Codex must not start after runtime cancellation.")

    async def run():
        task = asyncio.create_task(
            GraphRuntime(store, Codex(), worker_id="context-cancel").run_once(now=10_000)
        )
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    [job] = store.list_graph_jobs()
    assert job["status"] == "retryable_failed"
    assert job["error_code"] == "worker_interrupted"
    assert job["lease_owner"] is None


def test_runtime_never_executes_two_production_jobs_concurrently(tmp_path):
    store = _store(tmp_path)
    _session(store, "s2")
    active = 0
    maximum_active = 0
    started = asyncio.Event()
    release = asyncio.Event()

    class Codex:
        async def extract_structured(self, message, **kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            started.set()
            try:
                await release.wait()
                return _result()
            finally:
                active -= 1

    async def run():
        runtime = GraphRuntime(store, Codex(), worker_id="serial")
        first = asyncio.create_task(runtime.run_once(now=10_000))
        await started.wait()
        second = asyncio.create_task(runtime.run_once(now=10_000))
        await asyncio.sleep(0.05)
        assert maximum_active == 1
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(run())
    assert maximum_active == 1


def test_storage_guard_serializes_two_runtime_instances(tmp_path):
    first_store = _store(tmp_path)
    _session(first_store, "s2")
    second_store = Store(tmp_path / "bridge.sqlite3")
    active = 0
    maximum_active = 0
    started = asyncio.Event()
    release = asyncio.Event()

    class Codex:
        async def extract_structured(self, message, **kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            started.set()
            try:
                await release.wait()
                return _result("Global worker guard")
            finally:
                active -= 1

    async def run():
        first = asyncio.create_task(
            GraphRuntime(first_store, Codex(), worker_id="process-a").run_once(now=10_000)
        )
        await started.wait()
        second = asyncio.create_task(
            GraphRuntime(second_store, Codex(), worker_id="process-b").run_once(now=10_000)
        )
        await asyncio.sleep(0.05)
        assert maximum_active == 1
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(run())
    assert maximum_active == 1
    assert sum(job["status"] == "completed" for job in first_store.list_graph_jobs()) == 1


def test_runtime_renews_lease_while_codex_turn_runs(tmp_path, monkeypatch):
    store = _store(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    renewed = asyncio.Event()
    original_renew = store.renew_graph_job_lease

    def record_renewal(*args, **kwargs):
        result = original_renew(*args, **kwargs)
        renewed.set()
        return result

    monkeypatch.setattr(store, "renew_graph_job_lease", record_renewal)

    class Codex:
        async def extract_structured(self, message, **kwargs):
            started.set()
            await release.wait()
            return _result("Lease heartbeat")

    async def run():
        runtime = GraphRuntime(
            store, Codex(), worker_id="heartbeat",
            lease_seconds=30, heartbeat_interval_seconds=0.01,
        )
        task = asyncio.create_task(runtime.run_once())
        await started.wait()
        await asyncio.wait_for(renewed.wait(), timeout=1)
        release.set()
        await task

    asyncio.run(run())
    assert store.list_graph_jobs()[0]["status"] == "completed"


def test_heartbeat_lease_loss_cancels_turn_and_releases_job(tmp_path, monkeypatch):
    store = _store(tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    def lose_lease(*args, **kwargs):
        return False

    monkeypatch.setattr(store, "renew_graph_job_lease", lose_lease)

    class Codex:
        async def extract_structured(self, message, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def run():
        runtime = GraphRuntime(
            store, Codex(), worker_id="lease-loss",
            lease_seconds=30, heartbeat_interval_seconds=0.01,
        )
        task = asyncio.create_task(runtime.run_once())
        await started.wait()
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        await task

    asyncio.run(run())
    [job] = store.list_graph_jobs()
    assert job["status"] == "retryable_failed"
    assert job["error_code"] == "worker_interrupted"
    assert store.get_graph_analysis("s1") is None


def test_lease_renewal_requires_current_owner(tmp_path):
    store = _store(tmp_path)
    store.enqueue_eligible_graph_jobs(now=100_000)
    job = store.claim_graph_job("worker-a", now=100_000, lease_seconds=30)
    assert job is not None
    assert store.renew_graph_job_lease(
        job["job_id"], "worker-b", now=100_020, lease_seconds=30
    ) is False
    assert store.renew_graph_job_lease(
        job["job_id"], "worker-a", now=100_020, lease_seconds=30
    ) is True
    assert store.list_graph_jobs()[0]["lease_expires_at"] == 100_050


def test_expired_lease_cannot_exceed_max_attempts(tmp_path):
    store = _store(tmp_path)
    store.enqueue_eligible_graph_jobs(now=100_000)
    with store._connect() as connection:
        connection.execute("UPDATE graph_jobs SET max_attempts = 1")
    assert store.claim_graph_job("worker-a", now=100_000, lease_seconds=30)
    assert store.claim_graph_job("worker-b", now=100_031, lease_seconds=30) is None
    [job] = store.list_graph_jobs()
    assert job["status"] == "terminal_failed"
    assert job["error_code"] == "lease_exhausted"


def test_late_worker_cannot_publish_after_lease_reclaimed(tmp_path):
    store = _store(tmp_path)
    [queued] = store.enqueue_eligible_graph_jobs(now=100_000)
    first = store.claim_graph_job("worker-a", now=100_000, lease_seconds=30)
    second = store.claim_graph_job("worker-b", now=100_031, lease_seconds=30)
    assert first is not None and second is not None
    result = _result("Lease fencing")
    result["concepts"][0]["evidence"] = []
    with pytest.raises(ValueError, match="no longer publishable"):
        store.publish_graph_extraction(
            first["job_id"], result, lease_owner="worker-a", now=100_032
        )
    store.publish_graph_extraction(
        second["job_id"], result, lease_owner="worker-b", now=100_032
    )
    assert store.list_graph_jobs()[0]["status"] == "completed"


def test_expired_owner_cannot_fail_job_before_reclaim(tmp_path):
    store = _store(tmp_path)
    store.enqueue_eligible_graph_jobs(now=100_000)
    job = store.claim_graph_job("worker-a", now=100_000, lease_seconds=30)
    assert job is not None
    unchanged = store.fail_graph_job(
        job["job_id"],
        lease_owner="worker-a",
        error_code="invalid_extraction",
        error_message="Late stale result.",
        retryable=False,
        now=100_031,
    )
    assert unchanged is not None
    assert unchanged["status"] == "running"
    reclaimed = store.claim_graph_job("worker-b", now=100_031, lease_seconds=30)
    assert reclaimed is not None
    assert reclaimed["lease_owner"] == "worker-b"
    assert reclaimed["attempts"] == 2
