from __future__ import annotations

import pytest

from app.storage import GRAPH_FAILURE_RECOVERY_SETTING, Store


def _session_with_exchange(store: Store, session_id: str, *, created_at: int) -> int:
    store.create_session(session_id, session_id, "manual-context")
    exchange = store.save_exchange(session_id, "model", f"user {session_id}", f"assistant {session_id}")
    with store._connect() as connection:
        connection.execute(
            "UPDATE exchanges SET created_at = ?, assistant_created_at = ? WHERE exchange_id = ?",
            (created_at, created_at, exchange.exchange_id),
        )
    return exchange.exchange_id


def test_graph_jobs_has_latest_session_lookup_index(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")

    with store._connect() as connection:
        columns = connection.execute(
            "PRAGMA index_info(idx_graph_jobs_latest_session)"
        ).fetchall()

    assert [column["name"] for column in columns] == ["session_id", "created_at", "job_id"]
    with store._connect() as connection:
        plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT job_id FROM graph_jobs
            WHERE session_id = ?
            ORDER BY created_at DESC, job_id DESC LIMIT 1
            """,
            ("session",),
        ).fetchall()
    assert any(
        "USING COVERING INDEX idx_graph_jobs_latest_session" in row["detail"] for row in plan
    )


def test_store_caps_existing_pending_jobs_at_two_attempts(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    _session_with_exchange(store, "pending", created_at=1_000)
    store.set_graph_enabled(True)
    [job] = store.enqueue_eligible_graph_jobs(now=100_000)
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE graph_jobs
            SET status = 'retryable_failed', attempts = 2, max_attempts = 3
            WHERE job_id = ?
            """,
            (job["job_id"],),
        )

    migrated = Store(db_path)
    [capped] = migrated.list_graph_jobs()

    assert capped["status"] == "terminal_failed"
    assert capped["max_attempts"] == 2
    assert capped["error_code"] == "attempt_limit_reduced"
    assert migrated.claim_graph_job("worker", now=100_001) is None


@pytest.mark.parametrize("error_code", ["invalid_extraction", "lease_exhausted"])
def test_legacy_terminal_graph_failures_are_recovered_once(tmp_path, error_code) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    _session_with_exchange(store, "legacy", created_at=1_000)
    store.set_graph_enabled(True)
    [queued] = store.enqueue_eligible_graph_jobs(now=100_000)
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE graph_jobs SET status = 'terminal_failed', attempts = max_attempts,
                error_code = ?, error_message = 'legacy failure', completed_at = 100001
            WHERE job_id = ?
            """,
            (error_code, queued["job_id"]),
        )
        connection.execute("DELETE FROM app_settings WHERE key = ?", (GRAPH_FAILURE_RECOVERY_SETTING,))

    migrated = Store(db_path)
    [job] = migrated.list_graph_jobs()
    assert job["status"] == "retryable_failed"
    assert job["attempts"] == 0
    assert job["completed_at"] is None
    retry_now = job["available_at"] + 1
    assert migrated.claim_graph_job("worker", now=retry_now) is not None

    migrated.fail_graph_job(
        job["job_id"], lease_owner="worker", error_code=error_code,
        error_message="failed again", retryable=False, now=retry_now + 1,
    )
    assert Store(db_path).list_graph_jobs()[0]["status"] == "terminal_failed"


def test_eligibility_uses_per_session_inactivity_and_is_idempotent(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    draft = store.unlock_graph_profile("owner")
    store.update_graph_draft({**draft, "inactivity_hours": 1}, "owner")
    store.activate_graph_draft("owner")
    old_exchange = _session_with_exchange(store, "old", created_at=1_000)
    _session_with_exchange(store, "recent", created_at=9_000)
    store.set_graph_enabled(True)

    first = store.enqueue_eligible_graph_jobs(now=10_000)
    second = store.enqueue_eligible_graph_jobs(now=10_000)

    assert [(job["session_id"], job["source_exchange_id"]) for job in first] == [("old", old_exchange)]
    assert second == []
    assert len(store.list_graph_jobs()) == 1
    assert first[0]["max_attempts"] == 2


def test_reset_graph_scan_deletes_production_results_and_queues_every_allowed_session(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    old_exchange = _session_with_exchange(store, "old", created_at=1_000)
    recent_exchange = _session_with_exchange(store, "recent", created_at=9_900)
    store.create_session_group("Private", "#f59e0b", "lock", "private")
    store.update_session_group("private", is_sensitive=True)
    _session_with_exchange(store, "secret", created_at=1_000)
    store.set_session_group("secret", "private")
    draft = store.unlock_graph_profile("owner")
    store.update_graph_draft({**draft, "inactivity_hours": 1}, "owner")
    store.activate_graph_draft("owner")
    store.set_graph_enabled(True)
    [queued] = store.enqueue_eligible_graph_jobs(now=10_000)
    lab_run = store.create_graph_lab_run(
        "old",
        old_exchange,
        {"model": "gpt-5.6-sol", "effort": "high"},
        "owner",
        now=10_000,
    )
    running = store.claim_graph_job("worker", now=10_000, lease_seconds=30)
    assert running is not None
    store.publish_graph_extraction(
        running["job_id"],
        {
            "concepts": [
                {
                    "canonical_name": "Old result",
                    "type": "decision",
                    "summary": "This result should be deleted by the reset.",
                    "evidence": [{"exchange_id": old_exchange, "quote": "user old"}],
                }
            ]
        },
        lease_owner="worker",
        now=10_001,
    )

    result = store.reset_graph_scan(now=10_100)

    assert result == {"deleted_jobs": 1, "deleted_extractions": 1, "queued_jobs": 2}
    jobs = sorted(store.list_graph_jobs(), key=lambda job: job["session_id"])
    assert [(job["session_id"], job["source_exchange_id"], job["status"]) for job in jobs] == [
        ("old", old_exchange, "queued"),
        ("recent", recent_exchange, "queued"),
    ]
    assert all(job["job_id"] != queued["job_id"] for job in jobs)
    assert all(job["max_attempts"] == 2 for job in jobs)
    assert store.get_graph_analysis("old") is None
    assert store.get_graph_lab_run(lab_run["lab_run_id"]) == lab_run
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_extractions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM graph_extraction_concepts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM graph_extraction_evidence").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM graph_session_current").fetchone()[0] == 0


def test_reset_graph_scan_requires_graph_to_be_enabled(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")

    with pytest.raises(ValueError, match="Enable Graph before starting a fresh scan"):
        store.reset_graph_scan(now=100_000)


def test_reset_graph_scan_waits_for_the_previous_running_lease(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    first_runtime = Store(db_path)
    _session_with_exchange(first_runtime, "one", created_at=1_000)
    first_runtime.set_graph_enabled(True)
    first_runtime.enqueue_eligible_graph_jobs(now=100_000)
    running = first_runtime.claim_graph_job(
        "worker-a", now=100_000, lease_seconds=30
    )
    assert running is not None
    assert running["lease_expires_at"] == 100_030

    second_runtime = Store(db_path)
    second_runtime.reset_graph_scan(now=100_010)

    [fresh_job] = second_runtime.list_graph_jobs()
    assert fresh_job["available_at"] == 100_030
    assert second_runtime.claim_graph_job("worker-b", now=100_029) is None
    claimed = second_runtime.claim_graph_job("worker-b", now=100_030)
    assert claimed is not None
    assert claimed["job_id"] == fresh_job["job_id"]


def test_sensitive_sessions_are_excluded_by_default(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session_group("Private", "#f59e0b", "lock", "private")
    store.update_session_group("private", is_sensitive=True)
    _session_with_exchange(store, "secret", created_at=1_000)
    store.set_session_group("secret", "private")
    store.set_graph_enabled(True)

    assert store.enqueue_eligible_graph_jobs(now=100_000) == []


def test_job_claim_is_leased_and_expired_lease_is_recoverable(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    _session_with_exchange(store, "one", created_at=1_000)
    store.set_graph_enabled(True)
    [queued] = store.enqueue_eligible_graph_jobs(now=100_000)

    claimed = store.claim_graph_job("worker-a", now=100_000, lease_seconds=30)
    assert claimed["job_id"] == queued["job_id"]
    assert claimed["status"] == "running"
    assert store.claim_graph_job("worker-b", now=100_010, lease_seconds=30) is None

    reclaimed = store.claim_graph_job("worker-b", now=100_031, lease_seconds=30)
    assert reclaimed["job_id"] == queued["job_id"]
    assert reclaimed["lease_owner"] == "worker-b"
    assert reclaimed["attempts"] == 2


def test_graph_off_interrupts_running_jobs_and_blocks_claims(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    _session_with_exchange(store, "one", created_at=1_000)
    store.set_graph_enabled(True)
    store.enqueue_eligible_graph_jobs(now=100_000)
    running = store.claim_graph_job("worker", now=100_000)
    assert running is not None

    store.set_graph_enabled(False)

    [job] = store.list_graph_jobs()
    assert job["status"] == "interrupted"
    assert store.claim_graph_job("worker", now=100_100) is None
