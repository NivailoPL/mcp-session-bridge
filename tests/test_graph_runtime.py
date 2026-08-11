from __future__ import annotations

import asyncio

import pytest

from app.codex_app_server import (
    CodexIncompatibleVersionError,
    CodexPolicyViolationError,
    CodexProtocolError,
)
from app.graph_runtime import GraphRuntime
from app.storage import Store


def _ready_store(tmp_path) -> tuple[Store, int]:
    store = Store(tmp_path / "bridge.sqlite3")
    draft = store.unlock_graph_profile("owner")
    store.update_graph_draft({**draft, "inactivity_hours": 1}, "owner")
    store.activate_graph_draft("owner")
    store.create_session("s1", "Graph session", "manual-context")
    exchange = store.save_exchange(
        "s1", "model", "We use SQLite for the durable Graph queue.", "That is the implementation decision."
    )
    with store._connect() as connection:
        connection.execute(
            "UPDATE exchanges SET created_at = 1000, assistant_created_at = 1000 WHERE exchange_id = ?",
            (exchange.exchange_id,),
        )
    store.set_graph_enabled(True)
    return store, exchange.exchange_id


def test_runtime_publishes_valid_extraction_atomically(tmp_path) -> None:
    store, exchange_id = _ready_store(tmp_path)

    class Codex:
        async def extract_structured(self, message, *, model, effort, output_schema):
            assert f"Exchange {exchange_id}" in message
            assert model == "gpt-5.6-sol"
            assert output_schema["properties"]["concepts"]["maxItems"] == 25
            return {
                "concepts": [{
                    "canonical_name": "SQLite Graph queue",
                    "type": "technology",
                    "summary": "SQLite stores durable Graph jobs.",
                    "evidence": [{"exchange_id": exchange_id, "quote": "SQLite for the durable Graph queue"}],
                }]
            }

    runtime = GraphRuntime(store, Codex(), worker_id="test-worker")
    asyncio.run(runtime.run_once(now=10_000))

    [job] = store.list_graph_jobs()
    assert job["status"] == "completed"
    analysis = store.get_graph_analysis("s1")
    assert analysis is not None
    assert analysis["source_exchange_id"] == exchange_id
    assert analysis["concepts"][0]["canonical_name"] == "SQLite Graph queue"
    assert analysis["concepts"][0]["evidence"][0]["quote"] == "SQLite for the durable Graph queue"


def test_invalid_later_result_keeps_last_good_analysis(tmp_path) -> None:
    store, first_exchange_id = _ready_store(tmp_path)

    class FirstCodex:
        async def extract_structured(self, message, **kwargs):
            return {"concepts": [{
                "canonical_name": "First",
                "type": "topic",
                "summary": "First valid result.",
                "evidence": [{"exchange_id": first_exchange_id, "quote": "SQLite"}],
            }]}

    asyncio.run(GraphRuntime(store, FirstCodex(), worker_id="one").run_once(now=10_000))
    first_analysis = store.get_graph_analysis("s1")
    second = store.save_exchange("s1", "model", "A newer topic appears.", "Acknowledged.")
    with store._connect() as connection:
        connection.execute(
            "UPDATE exchanges SET created_at = 2000, assistant_created_at = 2000 WHERE exchange_id = ?",
            (second.exchange_id,),
        )

    class InvalidCodex:
        async def extract_structured(self, message, **kwargs):
            return {"concepts": [{
                "canonical_name": "Broken",
                "type": "topic",
                "summary": "Invalid evidence.",
                "evidence": [{"exchange_id": second.exchange_id, "quote": "not in source"}],
            }]}

    asyncio.run(GraphRuntime(store, InvalidCodex(), worker_id="two").run_once(now=20_000))

    assert store.get_graph_analysis("s1")["extraction_id"] == first_analysis["extraction_id"]
    job = store.list_graph_jobs()[0]
    assert job["status"] == "retryable_failed"
    assert job["error_code"] == "evidence_quote_not_literal"


def test_evidence_cannot_cite_transcript_framing_labels(tmp_path) -> None:
    store, exchange_id = _ready_store(tmp_path)

    class LabelCitingCodex:
        async def extract_structured(self, message, **kwargs):
            return {"concepts": [{
                "canonical_name": "Framing",
                "type": "other",
                "summary": "Invalid framing citation.",
                "evidence": [{"exchange_id": exchange_id, "quote": "USER:"}],
            }]}

    asyncio.run(GraphRuntime(store, LabelCitingCodex(), worker_id="labels").run_once(now=10_000))

    assert store.get_graph_analysis("s1") is None
    job = store.list_graph_jobs()[0]
    assert job["status"] == "retryable_failed"
    assert job["error_code"] == "evidence_quote_not_literal"
    assert job["error_message"] == "Evidence quote must appear literally in its source exchange."


def test_codex_protocol_failure_is_retryable_and_specific(tmp_path) -> None:
    store, _ = _ready_store(tmp_path)

    class Codex:
        async def extract_structured(self, message, **kwargs):
            raise CodexProtocolError("Codex returned invalid structured output.")

    asyncio.run(GraphRuntime(store, Codex(), worker_id="protocol").run_once(now=10_000))

    job = store.list_graph_jobs()[0]
    assert job["status"] == "retryable_failed"
    assert job["error_code"] == "codex_protocol_error"
    assert job["error_message"] == "Codex returned invalid structured output."


def test_codex_policy_violation_is_terminal_and_specific(tmp_path) -> None:
    store, _ = _ready_store(tmp_path)

    class Codex:
        async def extract_structured(self, message, **kwargs):
            raise CodexPolicyViolationError("Codex tools are disabled for this chat.")

    asyncio.run(GraphRuntime(store, Codex(), worker_id="policy").run_once(now=10_000))

    job = store.list_graph_jobs()[0]
    assert job["status"] == "terminal_failed"
    assert job["error_code"] == "codex_policy_violation"
    assert job["error_message"] == "Codex tools are disabled for this chat."


def test_codex_version_mismatch_is_terminal_and_specific(tmp_path) -> None:
    store, _ = _ready_store(tmp_path)

    class Codex:
        async def extract_structured(self, message, **kwargs):
            raise CodexIncompatibleVersionError("Incompatible Codex App Server version.")

    asyncio.run(GraphRuntime(store, Codex(), worker_id="version").run_once(now=10_000))

    job = store.list_graph_jobs()[0]
    assert job["status"] == "terminal_failed"
    assert job["error_code"] == "codex_version_incompatible"
    assert job["error_message"] == "Incompatible Codex App Server version."


@pytest.mark.parametrize(
    ("source_error", "expected_code", "expected_message"),
    [
        ("oversized", "source_exchange_too_large", "A single exchange exceeds the Graph source limit."),
        ("empty", "source_transcript_empty", "The session has no eligible source exchanges."),
    ],
)
def test_source_failures_are_terminal_and_specific(
    tmp_path, monkeypatch, source_error, expected_code, expected_message
) -> None:
    store, exchange_id = _ready_store(tmp_path)
    if source_error == "oversized":
        monkeypatch.setattr("app.graph_runtime.MAX_GRAPH_SOURCE_CHARS", 10)
    else:
        store.enqueue_eligible_graph_jobs(now=10_000)
        with store._connect() as connection:
            connection.execute(
                "UPDATE exchanges SET deleted_at = 9999 WHERE exchange_id = ?", (exchange_id,)
            )

    class Codex:
        async def extract_structured(self, message, **kwargs):
            raise AssertionError("Codex must not run for invalid source input")

    asyncio.run(GraphRuntime(store, Codex(), worker_id="source").run_once(now=10_000))

    job = store.list_graph_jobs()[0]
    assert job["status"] == "terminal_failed"
    assert job["error_code"] == expected_code
    assert job["error_message"] == expected_message
