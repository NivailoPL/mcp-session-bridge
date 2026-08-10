from __future__ import annotations

import asyncio

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
    assert store.list_graph_jobs()[0]["status"] == "terminal_failed"


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
    assert store.list_graph_jobs()[0]["status"] == "terminal_failed"
