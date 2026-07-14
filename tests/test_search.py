import sqlite3

import pytest

from app.search import SearchConfig, SearchService, chunk_text, fts_query
from app.storage import Store


def make_store(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session_group("Private", "#ef4444", "lock", group_id="private")
    store.create_session("public-session", "Public board", "manual-context")
    store.create_session("private-session", "Private board", "manual-context", group_id="private")
    return store


def test_basic_search_syncs_live_sources_and_deleted_exchanges(tmp_path):
    store = make_store(tmp_path)
    exchange = store.save_exchange(
        "public-session", "test", "Wojtek rode a skateboard in Krakow", "That happened in June"
    )
    service = SearchService(store)

    results = service.basic_search("skateboard Krakow", limit=10)

    assert any(item["source_kind"] == "conversation" for item in results)
    assert any(item["session_id"] == "public-session" for item in results)

    store.delete_exchange(exchange.exchange_id)
    assert service.basic_search("skateboard", limit=10) == []

    store.restore_exchange(exchange.exchange_id)
    assert service.basic_search("skateboard", limit=10)


def test_basic_search_includes_session_and_group_files(tmp_path):
    store = make_store(tmp_path)
    store.save_session_file(
        "public-session", "notes.md", "The walnut launch checklist", mime_type="text/markdown", created_by="owner"
    )
    store.save_group_file(
        "private", "secret.md", "A confidential capybara protocol", mime_type="text/markdown", created_by="owner"
    )
    service = SearchService(store)

    session_hit = service.basic_search("walnut", limit=10)[0]
    group_hit = service.basic_search("capybara", limit=10)[0]

    assert session_hit["source_kind"] == "session_file"
    assert session_hit["session_id"] == "public-session"
    assert group_hit["source_kind"] == "group_file"
    assert group_hit["group_id"] == "private"


def test_group_move_is_reflected_on_next_search(tmp_path):
    store = make_store(tmp_path)
    store.save_exchange("public-session", "test", "A unique marzipan note", "ack")
    service = SearchService(store)
    assert service.basic_search("marzipan")[0]["group_id"] == "uncategorized"

    store.set_session_group("public-session", "private")

    assert service.basic_search("marzipan")[0]["group_id"] == "private"


def test_unchanged_sources_skip_full_document_rescan(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.save_exchange("public-session", "test", "A stable pistachio note", "ack")
    service = SearchService(store)
    assert service.basic_search("pistachio")

    monkeypatch.setattr(
        service,
        "_source_rows",
        lambda: pytest.fail("unchanged source tables should not be rescanned"),
    )

    assert service.basic_search("pistachio")


def test_fts_query_is_literal_and_empty_safe():
    assert fts_query('"red team" OR secret:*') == '"red" OR "team" OR "OR" OR "secret"'
    assert fts_query(" --- ") == ""


def test_chunk_text_honors_token_budget_and_overlap():
    text = " ".join(f"token-{index}" for index in range(160))
    chunks = chunk_text(text, chunk_size=40, overlap=8)

    assert len(chunks) > 2
    assert all(chunk.token_count <= 40 for chunk in chunks)
    assert chunks[0].token_ids[-8:] == chunks[1].token_ids[:8]


def test_index_estimate_respects_group_consent_overlap_and_model_price(tmp_path):
    store = make_store(tmp_path)
    public_text = " ".join(f"public-{index}" for index in range(120))
    private_text = " ".join(f"private-{index}" for index in range(500))
    store.save_exchange("public-session", "test", public_text, "public reply")
    store.save_exchange("private-session", "test", private_text, "private reply")
    service = SearchService(store)
    config = SearchConfig(
        enabled=True,
        included_group_ids=("uncategorized",),
        chunk_size=64,
        chunk_overlap=16,
        embedding_model="text-embedding-3-small",
    )

    estimate = service.estimate_index(config)

    assert estimate["document_count"] == 1
    assert estimate["chunk_count"] > 1
    assert estimate["embedding_token_count"] > estimate["source_token_count"]
    assert estimate["overlap_token_count"] > 0
    assert estimate["price_usd_per_million_tokens"] == 0.02
    assert estimate["estimated_cost_usd"] == pytest.approx(
        estimate["embedding_token_count"] * 0.02 / 1_000_000
    )
    assert estimate["cohere_included"] is False

    custom = SearchConfig.from_dict({**config.to_dict(), "embedding_model": "custom-model"})
    assert service.estimate_index(custom)["estimated_cost_usd"] is None


def test_search_config_validation_and_privacy_partition():
    with pytest.raises(ValueError, match="overlap"):
        SearchConfig(chunk_size=100, chunk_overlap=100).validate()

    config = SearchConfig(enabled=True, included_group_ids=("uncategorized",))
    approved, local_only = config.partition_groups(["uncategorized", "private"])
    assert approved == {"uncategorized"}
    assert local_only == {"private"}

    with pytest.raises(TypeError, match="enabled must be a boolean"):
        SearchConfig.from_dict({"enabled": "false"})
    with pytest.raises(ValueError, match="at least one search source"):
        SearchConfig(
            include_conversations=False,
            include_session_files=False,
            include_group_files=False,
        ).validate()


def test_schema_uses_fts5_and_keeps_private_documents_local(tmp_path):
    store = make_store(tmp_path)
    store.save_exchange("public-session", "test", "shared apricot", "public answer")
    store.save_exchange("private-session", "test", "private apricot", "private answer")
    service = SearchService(store)
    service.sync_documents()

    with sqlite3.connect(store.db_path) as conn:
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'search_documents_fts'"
        ).fetchone()[0]
    assert "fts5" in schema.lower()

    config = SearchConfig(enabled=True, included_group_ids=("uncategorized",))
    approved, local_only = service.partition_basic_candidates("apricot", config)
    assert {item["group_id"] for item in approved} == {"uncategorized"}
    assert {item["group_id"] for item in local_only} == {"private"}

def test_rebuild_sends_only_approved_groups_to_openai(tmp_path):
    store = make_store(tmp_path)
    store.save_exchange("public-session", "test", "public nectarine evidence", "public reply")
    store.save_exchange("private-session", "test", "private dragonfruit evidence", "private reply")
    service = SearchService(store)
    sent: list[str] = []

    def fake_embed(api_key, texts, config):
        sent.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    service.embed_texts = fake_embed
    config = SearchConfig(
        enabled=True,
        included_group_ids=("uncategorized",),
        chunk_size=64,
        chunk_overlap=8,
        embedding_dimensions=2,
    )
    status = service.rebuild_index("test-key", config)

    assert status["status"] == "ready"
    assert status["active_generation"] == 1
    assert any("nectarine" in text for text in sent)
    assert all("dragonfruit" not in text for text in sent)


def test_failed_rebuild_preserves_previous_generation(tmp_path):
    store = make_store(tmp_path)
    store.save_exchange("public-session", "test", "stable papaya evidence", "reply")
    service = SearchService(store)
    config = SearchConfig(
        enabled=True,
        included_group_ids=("uncategorized",),
        chunk_size=64,
        chunk_overlap=8,
        embedding_dimensions=2,
    )
    service.embed_texts = lambda api_key, texts, config: [[1.0, 0.0] for _ in texts]
    first = service.rebuild_index("test-key", config)
    generation = first["active_generation"]

    store.save_exchange("public-session", "test", "new failing evidence", "reply")

    def fail_embed(api_key, texts, config):
        raise RuntimeError("provider unavailable")

    service.embed_texts = fail_embed
    with pytest.raises(RuntimeError, match="provider unavailable"):
        service.rebuild_index("test-key", config)

    status = service.index_status()
    assert status["active_generation"] == generation
    assert status["status"] == "failed"


def test_index_configuration_changes_mark_generation_stale(tmp_path):
    store = make_store(tmp_path)
    store.save_exchange("public-session", "test", "stable guava evidence", "reply")
    service = SearchService(store)
    config = SearchConfig(
        enabled=True,
        included_group_ids=("uncategorized",),
        chunk_size=64,
        chunk_overlap=8,
        embedding_dimensions=2,
    )
    service.embed_texts = lambda api_key, texts, config: [[1.0, 0.0] for _ in texts]
    service.set_config(config)
    assert service.rebuild_index("test-key", config)["needs_rebuild"] is False

    changed = SearchConfig.from_dict({**config.to_dict(), "chunk_size": 80})
    service.set_config(changed)

    assert service.index_status()["needs_rebuild"] is True
    with pytest.raises(ValueError, match="stale"):
        service.hybrid_search("guava", openai_api_key="test-key", config=changed)


def test_deleting_index_suppresses_automatic_recreation(tmp_path):
    store = make_store(tmp_path)
    store.save_exchange("public-session", "test", "stable melon evidence", "reply")
    service = SearchService(store)
    config = SearchConfig(
        enabled=True,
        included_group_ids=("uncategorized",),
        chunk_size=64,
        chunk_overlap=8,
        embedding_dimensions=2,
    )
    service.embed_texts = lambda api_key, texts, config: [[1.0, 0.0] for _ in texts]
    service.set_config(config)
    service.rebuild_index("test-key", config)

    deleted = service.delete_vector_index()
    checked = service.maybe_start_rebuild("test-key")

    assert deleted["auto_rebuild_suppressed"] is True
    assert checked["status"] == "empty"
    assert checked["active_generation"] is None


def test_cancelled_queued_build_never_calls_provider(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.save_exchange("public-session", "test", "stable quince evidence", "reply")
    service = SearchService(store)
    config = SearchConfig(enabled=True, included_group_ids=("uncategorized",))
    service.set_config(config)

    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            return None

    monkeypatch.setattr("app.search.threading.Thread", DeferredThread)
    service.start_rebuild("test-key", config)
    cancelled = service.cancel_rebuild()
    assert cancelled["cancel_requested"] is True
    assert cancelled["auto_rebuild_suppressed"] is True
    service.embed_texts = lambda *args, **kwargs: pytest.fail("provider should not be called")

    status = service.rebuild_index("test-key", config, queued_build=True)

    assert status["status"] == "empty"
    assert status["cancel_requested"] is False


def test_hybrid_search_keeps_unapproved_bm25_results_in_local_lane(tmp_path):
    store = make_store(tmp_path)
    store.save_exchange("public-session", "test", "shared lychee evidence", "public reply")
    store.save_exchange("private-session", "test", "private lychee evidence", "private reply")
    service = SearchService(store)
    config = SearchConfig(
        enabled=True,
        included_group_ids=("uncategorized",),
        chunk_size=64,
        chunk_overlap=8,
        embedding_dimensions=2,
    )
    service.embed_texts = lambda api_key, texts, config: [[1.0, 0.0] for _ in texts]
    service.rebuild_index("test-key", config)

    result = service.hybrid_search("lychee", openai_api_key="test-key", config=config)

    assert {item["group_id"] for item in result["results"]} == {"uncategorized"}
    assert {item["group_id"] for item in result["local_only_results"]} == {"private"}
    assert result["local_only_results"][0]["pipeline"] == ["BM25", "Local only"]
