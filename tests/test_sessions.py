import asyncio
import hashlib
import importlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from datetime import UTC

import pytest

from app.session_package import MASKED_MODEL_RESPONSE, render_session_transcript
from app.time_format import DISPLAY_TIMEZONE_SETTING_KEY
from app.storage import (
    MAX_SESSION_FILE_BYTES,
    PdfStorageQuotaError,
    SessionFileConflictError,
    Store,
    session_file_payload,
)
from app.pdf_files import extract_pdf_text_isolated
from scripts.session_audit import build_viewer_payload
from tests.pdf_samples import make_pdf


def test_store_saves_session_and_exchange(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Test session", "manual-context")

    exchange = store.save_exchange(
        session_id=session.session_id,
        model_name="Claude",
        user_message="First message.",
        assistant_response="Response from model Claude. First answer.",
    )

    sessions = store.list_sessions()
    exchanges = store.list_exchanges("s1")

    assert exchange.exchange_id == 1
    assert exchange.assistant_created_at == exchange.created_at
    assert session.group_id == "uncategorized"
    assert sessions[0]["group_id"] == "uncategorized"
    assert sessions[0]["group"]["icon_key"] == "folder"
    assert sessions[0]["exchange_count"] == 1
    assert exchanges[0].assistant_response.startswith("Response from model Claude")
    assert exchanges[0].assistant_created_at == exchange.assistant_created_at


def test_store_sorts_sessions_by_last_active_turn_not_admin_update(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session("older", "Older", "manual-context")
    store.create_session("newer", "Newer", "manual-context")
    older_exchange = store.save_exchange(
        "older",
        "Claude",
        "Older conversation.",
        "Older answer.",
        assistant_created_at=100,
    )
    newer_exchange = store.save_exchange(
        "newer",
        "Claude",
        "Newer conversation.",
        "Newer answer.",
        assistant_created_at=200,
    )

    assert [session["session_id"] for session in store.list_sessions()[:2]] == ["newer", "older"]

    store.set_session_title("older", "Renamed older conversation")
    sessions = store.list_sessions()

    assert [session["session_id"] for session in sessions[:2]] == ["newer", "older"]
    assert sessions[0]["last_turn_at"] == newer_exchange.assistant_created_at
    assert sessions[1]["last_turn_at"] == older_exchange.assistant_created_at
    assert sessions[0]["last_turn_at_iso"]


def test_store_manages_session_groups_and_reassigns_deleted_group(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")

    groups = store.list_session_groups()
    assert [group["group_id"] for group in groups] == ["uncategorized"]
    store.create_session_group("Brainstorming", "#3b82f6", "brain", group_id="brainstorming")
    store.create_session_group("Health", "#ef4444", "medical_plus", group_id="health")

    ideas = store.create_session_group("Ideas", "#22c55e", "ideas")
    session = store.create_session("s1", "Grouped", "manual-context", group_id=ideas.group_id)

    assert ideas.group_id == "ideas"
    assert ideas.is_system is False
    assert ideas.is_sensitive is False
    assert session.group_id == "ideas"
    assert store.list_sessions()[0]["group"]["name"] == "Ideas"

    updated = store.update_session_group(
        "ideas",
        name="Idea Lab",
        color="#0ea5e9",
        icon_key="brain",
        is_sensitive=True,
    )
    assert updated.name == "Idea Lab"
    assert updated.color == "#0ea5e9"
    assert updated.icon_key == "brain"
    assert updated.is_sensitive is True
    assert store.list_sessions()[0]["group"]["is_sensitive"] is True

    with pytest.raises(ValueError, match="System session groups cannot be edited"):
        store.update_session_group("uncategorized", name="Inbox")
    with pytest.raises(ValueError, match="System session groups cannot be deleted"):
        store.delete_session_group("uncategorized", destination_group_id="health")
    with pytest.raises(ValueError, match="session group name already exists"):
        store.create_session_group("idea lab", "#22c55e", "ideas", group_id="idea-lab-2")
    with pytest.raises(ValueError, match="Unknown session group"):
        store.create_session("s2", "Bad group", "manual-context", group_id="missing")

    store.set_session_group("s1", "brainstorming")
    assert store.get_session("s1").group_id == "brainstorming"

    store.set_session_group("s1", "ideas")
    deleted = store.delete_session_group("ideas", destination_group_id="health")
    assert deleted.deleted_at is not None
    assert store.get_session("s1").group_id == "health"
    assert "ideas" not in {group["group_id"] for group in store.list_session_groups()}
    assert "ideas" in {group["group_id"] for group in store.list_session_groups(include_deleted=True)}


def test_store_migrates_legacy_session_groups_with_sensitive_default(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE session_groups (
                group_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                color TEXT NOT NULL,
                icon_key TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_system INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                deleted_at REAL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO session_groups (
                group_id, name, color, icon_key, sort_order, is_system, created_at, updated_at
            ) VALUES ('legacy', 'Legacy', '#334455', 'archive', 10, 0, 1, 1)
            """
        )

    store = Store(db_path)
    groups = {group["group_id"]: group for group in store.list_session_groups()}

    assert groups["legacy"]["is_sensitive"] is False
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(session_groups)")}
    assert "is_sensitive" in columns


def test_store_demotes_legacy_non_default_system_groups(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)

    with sqlite3.connect(db_path) as conn:
        now = 1_700_000_000
        conn.executemany(
            """
            INSERT OR REPLACE INTO session_groups (
                group_id, name, color, icon_key, sort_order, is_system,
                created_at, updated_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, NULL)
            """,
            (
                ("brainstorming", "Brainstorming", "#3b82f6", "brain", 10, now, now),
                ("health", "Health", "#ef4444", "medical_plus", 20, now, now),
            ),
        )
    store.create_session("legacy-session", "Legacy group", "manual-context", group_id="brainstorming")

    reopened = Store(db_path)
    groups = {group["group_id"]: group for group in reopened.list_session_groups()}

    assert groups["uncategorized"]["is_system"] is True
    assert groups["brainstorming"]["is_system"] is False
    assert groups["health"]["is_system"] is False
    assert reopened.get_session("legacy-session").group_id == "brainstorming"

    updated = reopened.update_session_group("health", name="Wellness")
    deleted = reopened.delete_session_group("brainstorming")

    assert updated.name == "Wellness"
    assert deleted.deleted_at is not None
    assert reopened.get_session("legacy-session").group_id == "uncategorized"


def test_store_migrates_legacy_text_file_rows_before_saving_pdfs(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    store.create_session("s1", "Legacy files", "manual-context")
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE session_files")
        conn.execute(
            """
            CREATE TABLE session_files (
                file_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type TEXT NOT NULL,
                session_id TEXT,
                group_id TEXT,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_by TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO session_files (
                scope_type, session_id, group_id, filename, mime_type, content,
                sha256, size_bytes, created_by, created_at
            ) VALUES ('session', 's1', NULL, 'legacy.md', 'text/markdown',
                      'Legacy text', 'legacy-hash', 11, 'owner', 1700000000)
            """
        )

    reopened = Store(db_path)
    legacy = reopened.get_session_file(1)
    extraction = extract_pdf_text_isolated(make_pdf("Migrated PDF"))
    saved_pdf = reopened.save_session_pdf(
        "s1",
        "new.pdf",
        make_pdf("Migrated PDF"),
        extracted_text=extraction.content,
        page_count=extraction.page_count,
        extraction_status=extraction.extraction_status,
        extracted_text_bytes=extraction.extracted_text_bytes,
    )

    assert legacy is not None
    assert legacy.content == "Legacy text"
    assert legacy.content_kind == "text"
    assert legacy.page_count is None
    assert legacy.extraction_status == "not_applicable"
    assert legacy.extracted_text_bytes == 0
    assert session_file_payload(legacy, include_content=True) == {
        "file_id": 1,
        "scope_type": "session",
        "session_id": "s1",
        "group_id": None,
        "filename": "legacy.md",
        "mime_type": "text/markdown",
        "sha256": "legacy-hash",
        "size_bytes": 11,
        "created_by": "owner",
        "created_at": 1700000000,
        "content_kind": "text",
        "page_count": None,
        "extraction_status": "not_applicable",
        "extracted_text_bytes": 0,
        "text_available": True,
        "content": "Legacy text",
    }
    assert saved_pdf.content_kind == "pdf"
    assert "Migrated PDF" in saved_pdf.content


def test_store_soft_deletes_exchange_and_hides_it_from_transcript(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Correction test", "manual-context")
    first = store.save_exchange("s1", "Claude", "First message.", "First answer.")
    duplicate = store.save_exchange("s1", "Claude", "Duplicate.", "Second answer accidentally saved twice.")

    deleted = store.delete_exchange(duplicate.exchange_id, reason="duplicate", actor="owner")
    active_exchanges = store.list_exchanges("s1")
    all_exchanges = store.list_exchanges("s1", include_deleted=True)
    transcript = render_session_transcript(session, active_exchanges)
    sessions = store.list_sessions()
    events = store.list_exchange_events(duplicate.exchange_id)

    assert deleted.deleted_at is not None
    assert deleted.deleted_reason == "duplicate"
    assert [exchange.exchange_id for exchange in active_exchanges] == [first.exchange_id]
    assert all_exchanges[1].exchange_id == duplicate.exchange_id
    assert all_exchanges[1].deleted_at == deleted.deleted_at
    assert "Second answer accidentally saved twice." not in transcript["transcript_markdown"]
    assert sessions[0]["exchange_count"] == 1
    assert sessions[0]["deleted_exchange_count"] == 1
    assert events[-1]["action"] == "delete"
    assert events[-1]["actor"] == "owner"

    restored = store.restore_exchange(duplicate.exchange_id, actor="owner")

    assert restored.deleted_at is None
    assert [exchange.exchange_id for exchange in store.list_exchanges("s1")] == [first.exchange_id, duplicate.exchange_id]


def test_store_masks_assistant_response_without_hiding_exchange(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Masking test", "manual-context")
    exchange = store.save_exchange("s1", "GPT-5", "Give me a fresh opinion.", "A strongly anchoring answer.")

    masked = store.mask_exchange_response(exchange.exchange_id, actor="owner")
    transcript = render_session_transcript(session, store.list_exchanges("s1"))
    events = store.list_exchange_events(exchange.exchange_id)

    assert masked.assistant_masked_at is not None
    assert "Give me a fresh opinion." in transcript["transcript_markdown"]
    assert "A strongly anchoring answer." not in transcript["transcript_markdown"]
    assert MASKED_MODEL_RESPONSE in transcript["transcript_markdown"]
    assert transcript["turn_sequence"] == ["USER", "GPT-5"]
    assert events[-1]["action"] == "mask_response"

    store.delete_exchange(exchange.exchange_id, actor="owner")
    restored = store.restore_exchange(exchange.exchange_id, actor="owner")
    assert restored.assistant_masked_at == masked.assistant_masked_at

    unmasked = store.unmask_exchange_response(exchange.exchange_id, actor="owner")
    restored_transcript = render_session_transcript(session, store.list_exchanges("s1"))
    assert unmasked.assistant_masked_at is None
    assert "A strongly anchoring answer." in restored_transcript["transcript_markdown"]
    assert MASKED_MODEL_RESPONSE not in restored_transcript["transcript_markdown"]
    assert store.list_exchange_events(exchange.exchange_id)[-1]["action"] == "unmask_response"


def test_store_migrates_existing_exchanges_with_unmasked_default(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    store.create_session("s1", "Migration test", "manual-context")
    exchange = store.save_exchange("s1", "Claude", "Question.", "Answer.")

    with store._connect() as conn:
        conn.execute("ALTER TABLE exchanges DROP COLUMN assistant_masked_at")

    migrated = Store(db_path).get_exchange(exchange.exchange_id)
    assert migrated is not None
    assert migrated.assistant_masked_at is None


def test_get_latest_exchange_returns_newest_active_exchange(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session("s1", "Latest speaker", "manual-context")

    assert store.get_latest_exchange("s1") is None

    store.save_exchange("s1", "Claude", "First.", "First answer.")
    second = store.save_exchange("s1", "ChatGPT", "Second.", "Second answer.")

    latest = store.get_latest_exchange("s1")
    assert latest is not None
    assert latest.exchange_id == second.exchange_id
    assert latest.model_name == "ChatGPT"

    store.delete_exchange(second.exchange_id, reason="cleanup", actor="owner")
    after_delete = store.get_latest_exchange("s1")
    assert after_delete is not None
    assert after_delete.model_name == "Claude"


def test_store_edits_exchange_and_records_admin_event(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Edit test", "manual-context")
    exchange = store.save_exchange("s1", "Claude", "Old message.", "Old answer.")

    edited = store.update_exchange(
        exchange.exchange_id,
        model_name="ChatGPT",
        user_message="Corrected message.",
        assistant_response="Corrected answer.",
        actor="owner",
    )
    transcript = render_session_transcript(session, store.list_exchanges("s1"))
    events = store.list_exchange_events(exchange.exchange_id)

    assert edited.model_name == "ChatGPT"
    assert edited.user_message == "Corrected message."
    assert edited.assistant_response == "Corrected answer."
    assert edited.edited_at is not None
    assert "Old answer." not in transcript["transcript_markdown"]
    assert "Corrected answer." in transcript["transcript_markdown"]
    assert events[-1]["action"] == "edit"
    assert events[-1]["before"]["assistant_response"] == "Old answer."
    assert events[-1]["after"]["assistant_response"] == "Corrected answer."

    with pytest.raises(ValueError, match="model_name"):
        store.update_exchange(exchange.exchange_id, model_name="   ")


def test_store_renames_auto_titled_session_from_first_exchange(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session("s1", "Session 2026-05-26 18:00 UTC", "manual-context", title_is_auto=True)

    store.save_exchange(
        session_id="s1",
        model_name="ChatGPT",
        user_message="I want to discuss how to explain a work story without corporate jargon.",
        assistant_response="Response from model ChatGPT. Sure.",
    )

    session = store.get_session("s1")

    assert session is not None
    assert not session.title_is_auto
    assert session.title == "I want to discuss how to explain a work story without corporate jargon"


def test_session_transcript_renders_conversation_without_context_pack(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Audit test", "manual-context")
    first_response_at = int(datetime(2026, 5, 26, 19, 21, tzinfo=UTC).timestamp())
    second_response_at = int(datetime(2026, 5, 27, 8, 21, tzinfo=UTC).timestamp())
    store.save_exchange(
        "s1",
        "ChatGPT",
        "First message.",
        "Response from model ChatGPT. First answer.",
        assistant_created_at=first_response_at,
    )
    store.save_exchange(
        "s1",
        "Claude",
        "Second message.",
        "Response from model Claude. Second answer.",
        assistant_created_at=second_response_at,
    )

    transcript = render_session_transcript(session, store.list_exchanges("s1"))

    assert transcript["turn_sequence"] == ["USER", "ChatGPT", "USER", "Claude"]
    assert transcript["exchange_count"] == 2
    assert transcript["turn_count"] == 4
    assert "context_pack_id" not in transcript["transcript_markdown"]
    assert "## Turn Sequence\n\nUSER\nChatGPT\nUSER\nClaude" in transcript["transcript_markdown"]
    assert "### ChatGPT - 19:21 (Tuesday, May 26, 2026)" in transcript["transcript_markdown"]
    assert "### Claude - 08:21 (Wednesday, May 27, 2026)" in transcript["transcript_markdown"]
    assert "<!-- created_at_display=19:21 (Tuesday, May 26, 2026) -->" in transcript["transcript_markdown"]
    assert "<!-- created_at_display=08:21 (Wednesday, May 27, 2026) -->" in transcript["transcript_markdown"]
    assert "First message." in transcript["transcript_markdown"]
    assert "Response from model Claude" in transcript["transcript_markdown"]


def test_session_transcript_uses_configured_display_timezone(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Timezone test", "manual-context")
    response_at = int(datetime(2026, 5, 26, 19, 21, tzinfo=UTC).timestamp())
    store.save_exchange(
        "s1",
        "ChatGPT",
        "First message.",
        "Response from model ChatGPT. First answer.",
        assistant_created_at=response_at,
    )

    transcript = render_session_transcript(session, store.list_exchanges("s1"), timezone_name="Europe/Paris")

    assert "response_display_timezone: Europe/Paris" in transcript["transcript_markdown"]
    assert "### ChatGPT - 21:21 (Tuesday, May 26, 2026)" in transcript["transcript_markdown"]


def test_store_migrates_existing_exchanges_with_response_timestamp(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    session = store.create_session("s1", "Migration source", "manual-context")
    store.save_exchange("s1", "Claude", "Old message.", "Old answer.")
    original_exchange = store.list_exchanges(session.session_id)[0]

    with store._connect() as conn:
        conn.execute("ALTER TABLE exchanges RENAME TO exchanges_old")
        conn.execute(
            """
            CREATE TABLE exchanges (
                exchange_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                user_message TEXT NOT NULL,
                assistant_response TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            INSERT INTO exchanges (
                exchange_id, session_id, model_name, user_message,
                assistant_response, created_at
            )
            SELECT
                exchange_id, session_id, model_name, user_message,
                assistant_response, created_at
            FROM exchanges_old
            """
        )
        conn.execute("DROP TABLE exchanges_old")

    migrated_store = Store(db_path)
    migrated_exchange = migrated_store.list_exchanges(session.session_id)[0]

    assert migrated_exchange.assistant_created_at == original_exchange.created_at


def test_session_audit_builds_offline_viewer_payload(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session("s1", "Viewer test", "manual-context")
    store.save_exchange(
        "s1",
        "ChatGPT",
        "Show me this conversation in the viewer.",
        "Response from model ChatGPT. Sure, I am building the chat view.",
    )

    payload = build_viewer_payload(store)

    assert payload["ok"] is True
    assert payload["schema"] == "mcp-session-bridge.viewer.v1"
    assert payload["session_count"] == 1
    assert payload["turn_count"] == 2
    assert payload["sessions"][0]["session_id"] == "s1"
    assert payload["transcripts"]["s1"]["turn_sequence"] == ["USER", "ChatGPT"]
    assert payload["transcripts"]["s1"]["turns"][0]["content"] == "Show me this conversation in the viewer."


def test_output_probe_mode_migration_marks_historical_runs_as_maximum_compatibility(tmp_path) -> None:
    db_path = tmp_path / "legacy-probes.sqlite3"
    store = Store(db_path)
    store.create_output_probe_run(
        {
            "run_id": "legacy-probe",
            "harness_label": "codex",
            "content_profile": "transcript_markdown",
            "target_chars": 4_096,
            "payload_char_count": 4_096,
            "server_result_json_chars": 4_200,
            "block_count": 4,
            "block_size": 1_024,
            "tool_output_mode": "optimized",
            "blocks": [],
            "created_by": "test",
        }
    )
    with store._connect() as conn:
        conn.execute("ALTER TABLE output_probe_runs DROP COLUMN tool_output_mode")

    migrated = Store(db_path)
    run = migrated.get_output_probe_run("legacy-probe")

    assert run is not None
    assert run["tool_output_mode"] == "maximum_compatibility"


def test_public_tools_hide_context_pack_tools(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)

    tool_names = _tool_names(main)

    assert "get_session_overview" in tool_names
    assert "get_last_speaker" in tool_names
    assert "get_session_transcript_chunk" in tool_names
    assert "list_session_groups" in tool_names
    assert "upload_session_file" in tool_names
    assert "upload_group_file" in tool_names
    assert "upload_session_pdf" in tool_names
    assert "upload_group_pdf" in tool_names
    assert "list_session_files" in tool_names
    assert "download_session_file" in tool_names
    assert "run_output_probe" in tool_names
    assert "submit_output_probe_observation" in tool_names
    assert "list_sessions" not in tool_names
    assert "save_session_summary" not in tool_names
    assert "list_session_summaries" not in tool_names
    assert "list_context_packs" not in tool_names
    assert "get_session_package" not in tool_names
    assert "get_session_transcript" not in tool_names
    assert "save_context_summary" not in tool_names
    assert "export_session_markdown" not in tool_names
    assert "move_session_file" not in tool_names
    assert "edit_session_file" not in tool_names
    assert "update_session_file" not in tool_names
    assert "delete_session_file" not in tool_names


def test_large_tool_results_default_to_optimized_unstructured_output(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session("shape", "Result shape", "manual-context")
    main.store.save_exchange("shape", "Codex", "hello", "world")

    async def inspect_tools():
        tools = {tool.name: tool for tool in await main.mcp.list_tools()}
        result = await main.mcp._tool_manager.call_tool(
            "get_session_transcript_chunk",
            {"session_id": "shape", "chunk_index": 1},
            convert_result=True,
        )
        return tools, result

    tools, result = asyncio.run(inspect_tools())
    for tool_name in {
        "get_session_transcript_chunk",
        "run_output_probe",
        "download_session_file",
    }:
        assert tools[tool_name].outputSchema is None

    assert tools["get_session_overview"].outputSchema is not None
    assert main.ACTIVE_TOOL_OUTPUT_MODE == "optimized"
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].type == "text"


def test_invalid_persisted_tool_output_mode_falls_back_to_optimized(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.set_app_setting("mcp.tool_output_mode", "invalid-mode")
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")

    assert main.ACTIVE_TOOL_OUTPUT_MODE == "optimized"


def test_large_tool_results_can_restore_maximum_compatibility(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session("shape", "Result shape", "manual-context")
    main.store.save_exchange("shape", "Codex", "hello", "world")
    main.store.set_app_setting("mcp.tool_output_mode", "maximum_compatibility")
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")

    async def inspect_tools():
        tools = {tool.name: tool for tool in await main.mcp.list_tools()}
        result = await main.mcp._tool_manager.call_tool(
            "get_session_transcript_chunk",
            {"session_id": "shape", "chunk_index": 1},
            convert_result=True,
        )
        return tools, result

    tools, result = asyncio.run(inspect_tools())
    for tool_name in {
        "get_session_transcript_chunk",
        "run_output_probe",
        "download_session_file",
    }:
        assert tools[tool_name].outputSchema is not None
    assert main.ACTIVE_TOOL_OUTPUT_MODE == "maximum_compatibility"
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0][0].type == "text"
    assert result[1]["transcript_markdown"]


def test_public_file_tools_require_session_scoping(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)

    async def tools_by_name():
        return {tool.name: tool for tool in await main.mcp.list_tools()}

    tools = asyncio.run(tools_by_name())
    list_schema = tools["list_session_files"].inputSchema
    download_schema = tools["download_session_file"].inputSchema

    assert set(list_schema["required"]) == {"session_id"}
    assert set(list_schema["properties"]) == {"session_id"}
    assert set(download_schema["required"]) == {"session_id", "file_id"}
    assert set(download_schema["properties"]) == {"session_id", "file_id"}


def test_output_probe_round_trip_records_verified_result(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)

    started = main.run_output_probe(
        harness_label="chatgpt-web",
        target_chars=12_000,
        content_profile="transcript_markdown",
    )
    stored = main.store.get_output_probe_run(started["run_id"])

    assert started["ok"] is True
    assert len(started["probe_payload"]) == 12_000
    assert started["tool_output_mode"] == "optimized"
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["tool_output_mode"] == "optimized"
    assert started["server_result_json_chars"] > started["payload_char_count"]
    assert started["server_result_json_chars"] == len(
        json.dumps(started, ensure_ascii=False, separators=(",", ":"))
    )

    checkpoint_canaries = [
        block["canary"]
        for block in stored["blocks"]
        if block["checkpoint_labels"]
    ]
    last = stored["blocks"][-1]
    reported = main.submit_output_probe_observation(
        run_id=started["run_id"],
        observed_checkpoint_canaries=checkpoint_canaries,
        last_complete_block_index=last["index"],
        last_complete_canary=last["canary"],
    )

    assert reported["ok"] is True
    assert reported["result_class"] == "complete"
    assert reported["recommended_chunk_chars"] == 9_000
    completed = main.store.get_output_probe_run(started["run_id"])
    assert completed is not None
    assert completed["status"] == "reported"
    assert completed["result_class"] == "complete"


def test_output_probe_rejects_invalid_requests_and_unseen_canaries(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)

    assert main.run_output_probe("", 12_000)["ok"] is False
    assert main.run_output_probe("chatgpt-web", 4_095)["ok"] is False
    assert main.run_output_probe("chatgpt-web", 12_000, "bible")["ok"] is False

    started = main.run_output_probe("chatgpt-web", 12_000)
    failed = main.submit_output_probe_observation(
        started["run_id"],
        ["INVENTED-CANARY"],
        1,
        "INVENTED-CANARY",
        noticed_truncation=True,
    )

    assert failed["result_class"] == "verification_failed"
    assert failed["recommended_chunk_chars"] is None
    stored = main.store.get_output_probe_run(started["run_id"])
    assert stored is not None
    assert stored["noticed_truncation"] is True


def test_output_probe_report_is_immutable_and_retry_is_idempotent(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    started = main.run_output_probe("chatgpt-web", 12_000)
    stored = main.store.get_output_probe_run(started["run_id"])
    assert stored is not None
    checkpoints = [
        block["canary"] for block in stored["blocks"] if block["checkpoint_labels"]
    ]
    last = stored["blocks"][-1]

    first = main.submit_output_probe_observation(
        started["run_id"], checkpoints, last["index"], last["canary"]
    )
    retry = main.submit_output_probe_observation(
        started["run_id"], ["INVENTED-CANARY"], 1, "INVENTED-CANARY"
    )

    assert first["already_reported"] is False
    assert retry["already_reported"] is True
    assert retry["result_class"] == "complete"
    persisted = main.store.get_output_probe_run(started["run_id"])
    assert persisted is not None
    assert persisted["result_class"] == "complete"
    assert persisted["observed_canaries"] == checkpoints


def test_runtime_transcript_chunk_setting_overrides_environment(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch, max_lines=180, max_chars=12_000)
    session = main.store.create_session("runtime-chunks", "Runtime chunks", "manual-context")
    main.store.save_exchange(
        session.session_id,
        "Claude",
        "A" * 2_400,
        "B" * 2_400,
    )

    main.store.set_app_setting("transcript.chunk_max_chars", "1000")
    main.store.set_app_setting("transcript.chunk_max_lines", "500")
    overview = main.get_session_overview(session.session_id)
    chunks = [
        main.get_session_transcript_chunk(session.session_id, index)
        for index in range(1, overview["transcript_chunk_count"] + 1)
    ]

    assert overview["chunk_max_chars"] == 1000
    assert overview["chunk_max_lines"] == 500
    assert all(chunk["chunk_char_count"] <= 1000 for chunk in chunks)


def test_transcript_chunks_mask_model_response_for_every_consumer(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    session = main.store.create_session("masked-chunk", "Masked chunk", "manual-context")
    exchange = main.store.save_exchange(
        session.session_id,
        "GPT-5",
        "Give each model an independent view.",
        "This answer must not anchor another model.",
    )
    main.store.mask_exchange_response(exchange.exchange_id, actor="owner")

    overview = main.get_session_overview(session.session_id)
    chunks = [
        main.get_session_transcript_chunk(session.session_id, index)
        for index in range(1, overview["transcript_chunk_count"] + 1)
    ]
    transcript = "".join(chunk["transcript_markdown"] for chunk in chunks)

    assert "Give each model an independent view." in transcript
    assert MASKED_MODEL_RESPONSE in transcript
    assert "This answer must not anchor another model." not in transcript


def test_get_last_speaker_reports_continuity_decision(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session("s1", "Continuity", "manual-context")

    unknown = main.get_last_speaker("missing", "Claude")
    assert unknown["ok"] is False

    empty = main.get_last_speaker("s1", "Claude")
    assert empty["ok"] is True
    assert empty["has_exchanges"] is False
    assert empty["latest_model_name"] is None
    assert empty["should_fetch_transcript"] is True

    main.store.save_exchange("s1", "Claude", "Hi.", "Hello from Claude.")

    same = main.get_last_speaker("s1", "claude")
    assert same["same_model"] is True
    assert same["should_fetch_transcript"] is False
    assert same["latest_model_name"] == "Claude"

    diff = main.get_last_speaker("s1", "ChatGPT")
    assert diff["same_model"] is False
    assert diff["should_fetch_transcript"] is True

    blank = main.get_last_speaker("s1", "")
    assert blank["same_model"] is False
    assert blank["should_fetch_transcript"] is True

    second = main.store.save_exchange("s1", "ChatGPT", "More.", "Reply from ChatGPT.")
    main.store.delete_exchange(second.exchange_id, reason="cleanup", actor="owner")
    after_delete = main.get_last_speaker("s1", "Claude")
    assert after_delete["latest_model_name"] == "Claude"
    assert after_delete["should_fetch_transcript"] is False


def test_create_session_works_without_context_pack_manifest(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)

    main.store.create_session_group("Ideas", "#22c55e", "ideas")
    main.store.update_session_group("ideas", is_sensitive=True)
    groups_result = main.list_session_groups()
    result = main.create_session("Manual context session", group_id="ideas")
    invalid_result = main.create_session("Manual context session", group_id="missing")
    session = main.store.get_session(result["session_id"])
    overview = main.get_session_overview(result["session_id"])

    assert groups_result["ok"] is True
    assert "ideas" in {group["group_id"] for group in groups_result["groups"]}
    assert all("is_sensitive" not in group for group in groups_result["groups"])
    assert result["ok"] is True
    assert result["group_id"] == "ideas"
    assert result["group"]["name"] == "Ideas"
    assert "is_sensitive" not in result["group"]
    assert "is_sensitive" not in overview["group"]
    assert invalid_result["ok"] is False
    assert invalid_result["error"] == "Unknown session group: missing"
    assert result["context_source"] == "manual"
    assert "context_pack_id" not in result
    assert session is not None
    assert session.context_pack_id == "manual-context"
    assert session.group_id == "ideas"


def test_session_overview_and_transcript_chunks_round_trip(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch, max_lines=8, max_chars=220)
    session = main.store.create_session("s1", "Chunk test", "manual-context")
    for index in range(1, 8):
        main.store.save_exchange(
            "s1",
            "Claude" if index % 2 else "ChatGPT",
            f"Message {index}\\n" + ("A" * 90),
            f"Answer {index}\\n" + ("B" * 110),
        )

    overview = main.get_session_overview("s1")
    chunks = [main.get_session_transcript_chunk("s1", index) for index in range(1, overview["transcript_chunk_count"] + 1)]
    full_transcript = render_session_transcript(session, main.store.list_exchanges("s1"))["transcript_markdown"]

    assert overview["ok"] is True
    assert overview["context_source"] == "manual"
    assert overview["group_id"] == "uncategorized"
    assert overview["group"]["name"] == "Uncategorized"
    assert overview["exchange_count"] == 7
    assert overview["turn_count"] == 14
    assert overview["transcript_chunk_count"] > 1
    assert "transcript_markdown" not in overview
    assert "turns" not in overview
    assert all(chunk["ok"] for chunk in chunks)
    assert all(chunk["chunk_char_count"] <= 220 for chunk in chunks)
    assert all(chunk["chunk_line_count"] <= 8 for chunk in chunks)
    assert "".join(chunk["transcript_markdown"] for chunk in chunks) == full_transcript
    assert chunks[-1]["has_more"] is False
    assert chunks[-1]["next_chunk_index"] is None


def test_mcp_session_files_are_visible_only_through_their_session(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session_group("Ideas", "#22c55e", "ideas")
    main.store.create_session_group("Health", "#ef4444", "medical_plus", group_id="health")
    main.store.create_session("s1", "File test", "manual-context", group_id="ideas")
    main.store.create_session("s2", "Other session", "manual-context", group_id="ideas")
    main.store.create_session("s3", "Health session", "manual-context", group_id="health")

    session_file = main.upload_session_file("s1", "plan.md", "# Plan\n\nDo the thing.")
    other_session_file = main.upload_session_file("s2", "private.md", "Other session context.")
    group_file = main.upload_group_file("ideas", "context.md", "Shared group context.")
    other_group_file = main.upload_group_file("health", "health.md", "Shared health context.")
    listed = main.list_session_files(session_id="s1")
    downloaded = main.download_session_file(
        session_id="s1",
        file_id=session_file["file"]["file_id"],
    )
    downloaded_group = main.download_session_file(
        session_id="s1",
        file_id=group_file["file"]["file_id"],
    )
    hidden_session = main.download_session_file(
        session_id="s1",
        file_id=other_session_file["file"]["file_id"],
    )
    hidden_group = main.download_session_file(
        session_id="s1",
        file_id=other_group_file["file"]["file_id"],
    )
    missing = main.download_session_file(session_id="s1", file_id=999_999)
    overview = main.get_session_overview("s1")
    invalid = main.upload_group_file("missing", "x.md", "Nope.")

    assert session_file["ok"] is True
    assert session_file["file"]["scope_type"] == "session"
    assert session_file["file"]["filename"] == "plan.md"
    assert group_file["ok"] is True
    assert group_file["file"]["scope_type"] == "group"
    assert {file["filename"] for file in listed["files"]} == {"plan.md", "context.md"}
    assert downloaded["file"]["content"] == "# Plan\n\nDo the thing."
    assert downloaded_group["file"]["content"] == "Shared group context."
    assert hidden_session == hidden_group == missing
    assert hidden_session == {"ok": False, "error": "File is unavailable for this session."}
    assert overview["files"]["session"][0]["filename"] == "plan.md"
    assert overview["files"]["group"][0]["filename"] == "context.md"
    assert invalid["ok"] is False
    assert invalid["error"] == "Unknown session group: missing"

    main.store.set_session_group("s1", "health")

    moved_listing = main.list_session_files(session_id="s1")
    assert {file["filename"] for file in moved_listing["files"]} == {"plan.md", "health.md"}
    assert main.download_session_file(
        session_id="s1",
        file_id=group_file["file"]["file_id"],
    ) == missing
    assert main.download_session_file(
        session_id="s1",
        file_id=other_group_file["file"]["file_id"],
    )["file"]["content"] == "Shared health context."
    assert main.list_session_files(session_id="missing") == {
        "ok": False,
        "error": "Unknown session_id: missing",
    }


def test_mcp_uploads_pdf_and_returns_extracted_text_without_original_bytes(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session_group("Ideas", "#22c55e", "ideas")
    main.store.create_session("s1", "PDF test", "manual-context", group_id="ideas")
    raw = make_pdf("PDF context for RAG")
    encoded = __import__("base64").b64encode(raw).decode("ascii")

    session_pdf = asyncio.run(main.upload_session_pdf("s1", "brief.pdf", encoded))
    group_pdf = asyncio.run(main.upload_group_pdf("ideas", "shared.pdf", encoded))
    downloaded = main.download_session_file(
        session_id="s1",
        file_id=session_pdf["file"]["file_id"],
    )

    assert session_pdf["ok"] is True
    assert session_pdf["file"]["content_kind"] == "pdf"
    assert session_pdf["file"]["page_count"] == 1
    assert session_pdf["file"]["extraction_status"] == "ready"
    assert session_pdf["file"]["text_available"] is True
    assert session_pdf["file"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert session_pdf["file"]["size_bytes"] == len(raw)
    assert group_pdf["file"]["scope_type"] == "group"
    assert "PDF context for RAG" in downloaded["file"]["content"]
    assert downloaded["file"]["extracted_text_bytes"] == len(
        downloaded["file"]["content"].encode("utf-8")
    )
    assert "binary_content" not in downloaded["file"]
    assert "content_base64" not in downloaded["file"]


def test_mcp_accepts_image_only_pdf_but_marks_it_unindexed(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session("s1", "PDF test", "manual-context")
    encoded = __import__("base64").b64encode(make_pdf(None)).decode("ascii")

    uploaded = asyncio.run(main.upload_session_pdf("s1", "scan.pdf", encoded))
    downloaded = main.download_session_file(
        session_id="s1",
        file_id=uploaded["file"]["file_id"],
    )

    assert uploaded["ok"] is True
    assert uploaded["file"]["extraction_status"] == "no_text"
    assert uploaded["file"]["text_available"] is False
    assert downloaded["file"]["content"] == ""


def test_mcp_rejects_invalid_pdf_payloads(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session("s1", "PDF test", "manual-context")

    malformed = asyncio.run(main.upload_session_pdf("s1", "bad.pdf", "%%%"))
    not_pdf = asyncio.run(
        main.upload_session_pdf(
            "s1",
            "bad.pdf",
            __import__("base64").b64encode(b"not a pdf").decode("ascii"),
        )
    )
    wrong_extension = asyncio.run(
        main.upload_session_pdf(
            "s1",
            "bad.txt",
            __import__("base64").b64encode(make_pdf()).decode("ascii"),
        )
    )

    assert malformed["ok"] is False
    assert "base64" in malformed["error"]
    assert not_pdf["ok"] is False
    assert "valid PDF" in not_pdf["error"]
    assert wrong_extension["ok"] is False
    assert "filename" in wrong_extension["error"]


def test_mcp_pdf_upload_reports_worker_saturation(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session("s1", "PDF test", "manual-context")

    async def busy_worker(*args, **kwargs):
        raise main.PdfWorkerBusyError("PDF processing is busy; retry shortly")

    monkeypatch.setattr(main, "run_pdf_worker", busy_worker)

    result = asyncio.run(main.upload_session_pdf("s1", "brief.pdf", "unused"))

    assert result == {
        "ok": False,
        "error": "PDF processing is busy; retry shortly",
        "error_code": "pdf_worker_busy",
        "retryable": True,
        "retry_after_seconds": 2,
    }


def test_pdf_storage_quota_is_enforced_before_insert(tmp_path) -> None:
    raw = make_pdf("Quota text")
    extraction = extract_pdf_text_isolated(raw)
    quota = len(raw) + extraction.extracted_text_bytes
    store = Store(tmp_path / "bridge.sqlite3", pdf_storage_max_bytes=quota)
    store.create_session("s1", "Quota", "manual-context")

    store.save_session_pdf(
        "s1",
        "first.pdf",
        raw,
        extracted_text=extraction.content,
        page_count=extraction.page_count,
        extraction_status=extraction.extraction_status,
        extracted_text_bytes=extraction.extracted_text_bytes,
    )
    with pytest.raises(PdfStorageQuotaError, match="quota exceeded"):
        store.save_session_pdf(
            "s1",
            "second.pdf",
            raw,
            extracted_text=extraction.content,
            page_count=extraction.page_count,
            extraction_status=extraction.extraction_status,
            extracted_text_bytes=extraction.extracted_text_bytes,
        )

    assert len(store.list_session_files(session_id="s1")) == 1


def test_mcp_overview_reads_session_and_group_files_in_one_snapshot(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session_group("Ideas", "#22c55e", "ideas")
    main.store.create_session("s1", "File test", "manual-context", group_id="ideas")
    main.store.save_session_file("s1", "plan.md", "# Plan")
    main.store.save_group_file("ideas", "context.md", "Shared context")
    calls: list[dict[str, str | None]] = []
    original_list = main.store.list_session_files

    def tracked_list(**kwargs):
        calls.append(kwargs)
        return original_list(**kwargs)

    monkeypatch.setattr(main.store, "list_session_files", tracked_list)

    overview = main.get_session_overview("s1")

    assert calls == [{"session_id": "s1", "group_id": "ideas"}]
    assert [file["filename"] for file in overview["files"]["session"]] == ["plan.md"]
    assert [file["filename"] for file in overview["files"]["group"]] == ["context.md"]


def test_list_session_files_does_not_select_content_bodies(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session("s1", "File test", "manual-context")
    store.save_session_file("s1", "large.md", "x" * 100_000)
    statements: list[str] = []
    original_connect = store._connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    store._connect = traced_connect  # type: ignore[method-assign]

    listed = store.list_session_files(session_id="s1")

    select = next(
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT") and "FROM session_files" in statement
    )
    projection = select.split("FROM session_files", 1)[0].lower()
    assert " content," not in projection
    assert "binary_content" not in projection
    assert "*" not in projection
    assert listed[0]["filename"] == "large.md"

    store.get_session_file(listed[0]["file_id"])
    detail_select = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT") and "FROM session_files" in statement
    ][-1]
    assert "binary_content" not in detail_select.split("FROM session_files", 1)[0].lower()


def test_store_moves_session_files_without_changing_identity_or_metadata(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session_group("Ideas", "#22c55e", "ideas")
    store.create_session("s1", "First", "manual-context", group_id="ideas")
    store.create_session("s2", "Peer", "manual-context", group_id="ideas")
    saved = store.save_session_file("s1", "plan.md", "# Plan", created_by="test-owner")
    immutable_metadata = (
        saved.file_id,
        saved.filename,
        saved.mime_type,
        saved.content,
        saved.sha256,
        saved.size_bytes,
        saved.created_by,
        saved.created_at,
    )

    moved_to_group = store.move_session_file(saved.file_id, scope_type="group", group_id="ideas")
    assert moved_to_group.scope_type == "group"
    assert moved_to_group.session_id is None
    assert moved_to_group.group_id == "ideas"
    assert (
        moved_to_group.file_id,
        moved_to_group.filename,
        moved_to_group.mime_type,
        moved_to_group.content,
        moved_to_group.sha256,
        moved_to_group.size_bytes,
        moved_to_group.created_by,
        moved_to_group.created_at,
    ) == immutable_metadata
    assert store.move_session_file(saved.file_id, scope_type="group", group_id="ideas") == moved_to_group
    assert store.list_session_files(session_id="s1") == []
    assert [item["file_id"] for item in store.list_session_files(group_id="ideas")] == [saved.file_id]

    moved_to_session = store.move_session_file(saved.file_id, scope_type="session", session_id="s1")
    assert moved_to_session.scope_type == "session"
    assert moved_to_session.session_id == "s1"
    assert moved_to_session.group_id is None
    assert [item["file_id"] for item in store.list_session_files(session_id="s1")] == [saved.file_id]
    assert store.list_session_files(session_id="s2") == []
    assert store.list_session_files(group_id="ideas") == []


def test_store_file_mutations_recheck_admin_visibility_atomically(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session_group("Ideas", "#22c55e", "ideas")
    store.create_session_group("Other", "#ef4444", "camera")
    store.create_session("s1", "First", "manual-context", group_id="ideas")
    store.create_session("s2", "Other", "manual-context", group_id="other")
    edit_file = store.save_session_file("s1", "edit.md", "Original")
    move_file = store.save_session_file("s1", "move.md", "Original")
    delete_file = store.save_session_file("s1", "delete.md", "Original")

    for saved in (edit_file, move_file, delete_file):
        assert store.get_session_file(saved.file_id) == saved
        store.move_session_file(saved.file_id, scope_type="session", session_id="s2")

    with pytest.raises(SessionFileConflictError, match="no longer visible"):
        store.update_session_file(
            edit_file.file_id,
            "Changed",
            expected_sha256=edit_file.sha256,
            visible_session_id="s1",
            visible_group_id="ideas",
        )
    with pytest.raises(SessionFileConflictError, match="no longer visible"):
        store.move_session_file(
            move_file.file_id,
            scope_type="group",
            group_id="ideas",
            visible_session_id="s1",
            visible_group_id="ideas",
        )
    with pytest.raises(SessionFileConflictError, match="no longer visible"):
        store.delete_session_file(
            delete_file.file_id,
            visible_session_id="s1",
            visible_group_id="ideas",
        )

    for saved in (edit_file, move_file, delete_file):
        current = store.get_session_file(saved.file_id)
        assert current is not None
        assert current.session_id == "s2"
        assert current.content == "Original"


def test_store_rejects_invalid_file_moves_without_partial_updates(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session_group("Ideas", "#22c55e", "ideas")
    store.create_session_group("Archive", "#64748b", "archive", group_id="archive-test")
    store.delete_session_group("archive-test")
    store.create_session("s1", "First", "manual-context", group_id="ideas")
    saved = store.save_session_file("s1", "plan.md", "# Plan")

    invalid_moves = (
        {"scope_type": "other", "session_id": "s1"},
        {"scope_type": "session"},
        {"scope_type": "session", "session_id": "missing"},
        {"scope_type": "session", "session_id": "s1", "group_id": "ideas"},
        {"scope_type": "group"},
        {"scope_type": "group", "group_id": "missing"},
        {"scope_type": "group", "group_id": "archive-test"},
        {"scope_type": "group", "group_id": "ideas", "session_id": "s1"},
    )
    for target in invalid_moves:
        with pytest.raises(ValueError):
            store.move_session_file(saved.file_id, **target)
        assert store.get_session_file(saved.file_id) == saved

    with pytest.raises(ValueError, match="Unknown file_id: 99999"):
        store.move_session_file(99999, scope_type="session", session_id="s1")
    assert store.get_session_file(saved.file_id) == saved


def test_store_edits_session_file_with_hash_guard_and_atomic_validation(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session("s1", "First", "manual-context")
    saved = store.save_session_file("s1", "plan.md", "Old content", created_by="test-owner")

    edited = store.update_session_file(saved.file_id, "New content", expected_sha256=saved.sha256)

    assert edited.file_id == saved.file_id
    assert edited.created_at == saved.created_at
    assert edited.created_by == saved.created_by
    assert edited.filename == saved.filename
    assert edited.mime_type == saved.mime_type
    assert edited.content == "New content"
    assert edited.sha256 != saved.sha256
    assert edited.size_bytes == len("New content".encode("utf-8"))

    with pytest.raises(SessionFileConflictError, match="changed since it was opened"):
        store.update_session_file(saved.file_id, "Stale overwrite", expected_sha256=saved.sha256)
    assert store.get_session_file(saved.file_id) == edited

    with pytest.raises(ValueError, match="content must not be empty"):
        store.update_session_file(edited.file_id, "", expected_sha256=edited.sha256)
    assert store.get_session_file(saved.file_id) == edited

    with pytest.raises(ValueError, match="bytes or fewer"):
        store.update_session_file(
            edited.file_id,
            "x" * (MAX_SESSION_FILE_BYTES + 1),
            expected_sha256=edited.sha256,
        )
    assert store.get_session_file(saved.file_id) == edited


def test_store_hard_deletes_file_and_preserves_existing_payload_keys(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session("s1", "File test", "manual-context")
    uploaded = main.upload_session_file("s1", "plan.md", "# Plan")
    file_id = uploaded["file"]["file_id"]
    expected_manifest_keys = {
        "file_id",
        "scope_type",
        "session_id",
        "group_id",
        "filename",
        "mime_type",
        "sha256",
        "size_bytes",
        "created_by",
        "created_at",
        "content_kind",
        "page_count",
        "extraction_status",
        "extracted_text_bytes",
        "text_available",
    }

    assert set(uploaded["file"]) == expected_manifest_keys
    assert set(main.list_session_files(session_id="s1")["files"][0]) == expected_manifest_keys
    assert set(main.download_session_file(session_id="s1", file_id=file_id)["file"]) == (
        expected_manifest_keys | {"content"}
    )

    deleted = main.store.delete_session_file(file_id)

    assert deleted.file_id == file_id
    assert main.store.get_session_file(file_id) is None
    with pytest.raises(ValueError, match=f"Unknown file_id: {file_id}"):
        main.store.delete_session_file(file_id)
    assert main.list_session_files(session_id="s1")["files"] == []
    assert main.get_session_overview("s1")["files"]["session"] == []
    assert main.download_session_file(session_id="s1", file_id=file_id) == {
        "ok": False,
        "error": "File is unavailable for this session.",
    }

    replacement = main.upload_session_file("s1", "plan.md", "Replacement")
    assert replacement["file"]["file_id"] != file_id


def test_display_timezone_setting_controls_mcp_timestamps(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.set_app_setting(DISPLAY_TIMEZONE_SETTING_KEY, "Europe/Paris")
    main.store.create_session("s1", "Timezone setting", "manual-context")

    result = main.save_exchange(
        "s1",
        "ChatGPT",
        "First message.",
        "Response from model ChatGPT. First answer.",
    )
    overview = main.get_session_overview("s1")
    chunk = main.get_session_transcript_chunk("s1", 1)

    assert result["assistant_created_at_timezone"] == "Europe/Paris"
    assert overview["response_display_timezone"] == "Europe/Paris"
    assert "response_display_timezone: Europe/Paris" in chunk["transcript_markdown"]


def test_project_prompt_documents_manual_context_and_chunk_protocol() -> None:
    prompt = Path("docs/project-prompt-template.md").read_text(encoding="utf-8")

    assert (
        '"Response from model <who you are>\n'
        'HH:MM (weekday, Month D, YYYY)\n'
        'Session: <session_id>"'
    ) in prompt
    assert "`get_session_overview`" in prompt
    assert "`get_last_speaker`" in prompt
    assert "`get_session_transcript_chunk`" in prompt
    assert "`list_session_groups`" in prompt
    assert "`save_session_summary`" not in prompt
    assert "`upload_session_file`" in prompt
    assert "`upload_group_file`" in prompt
    assert "`upload_session_pdf`" in prompt
    assert "`upload_group_pdf`" in prompt
    assert "`download_session_file`" in prompt
    assert "`get_session_package`" not in prompt
    assert "context pack" not in prompt.lower()


def test_public_docs_describe_explicit_mutable_file_context() -> None:
    readme = Path("README.md").read_text(encoding="utf-8").lower()
    limitations = Path("docs/limitations.md").read_text(encoding="utf-8").lower()
    instructions = Path("docs/model-instructions.md").read_text(encoding="utf-8").lower()
    prompt = Path("docs/project-prompt-template.md").read_text(encoding="utf-8").lower()

    assert "admin ui" in readme
    assert "`upload_session_file`" in readme
    assert "`upload_group_file`" in readme
    assert "`upload_session_pdf`" in readme
    assert "ocr" in limitations
    assert "does not automatically ingest" in limitations
    assert "external files or directories" in limitations

    for model_doc in (instructions, prompt):
        assert "current file manifest is authoritative" in model_doc
        assert "not automatically notified" in model_doc
        assert "`list_session_files(session_id=...)`" in model_doc
        assert "`download_session_file(session_id=..., file_id=...)`" in model_doc


def test_public_docs_describe_unlisted_sessions_and_scoped_file_reads() -> None:
    readme = Path("README.md").read_text(encoding="utf-8").lower()
    prompt = Path("docs/project-prompt-template.md").read_text(encoding="utf-8").lower()
    instructions = Path("docs/model-instructions.md").read_text(encoding="utf-8").lower()
    security = Path("docs/security.md").read_text(encoding="utf-8").lower()
    limitations = Path("docs/limitations.md").read_text(encoding="utf-8").lower()
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8").lower()

    for active_doc in (readme, prompt, instructions):
        assert "list_sessions" not in active_doc

    for model_doc in (prompt, instructions):
        assert "ask the user" in model_doc
        assert "never enumerate" in model_doc
        assert "`list_session_files(session_id=...)`" in model_doc
        assert "`download_session_file(session_id=..., file_id=...)`" in model_doc

    assert "not global across projects" not in prompt
    assert "does not know" in prompt
    assert "project boundaries" in prompt
    assert "not access control" in security
    assert "not access control" in limitations
    assert "list_sessions" in changelog
    assert "refresh" in changelog


def test_server_instructions_are_publication_ready(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)

    assert len(main.SERVER_INSTRUCTIONS) <= 512
    assert "MCP Session Bridge" in main.SERVER_INSTRUCTIONS
    assert "WW" + "-MCP" not in main.SERVER_INSTRUCTIONS
    assert "Woj" + "tek" not in main.SERVER_INSTRUCTIONS
    assert "user" in main.SERVER_INSTRUCTIONS.lower()
    assert "get_session_overview" in main.SERVER_INSTRUCTIONS
    assert "get_last_speaker" in main.SERVER_INSTRUCTIONS
    assert "get_session_transcript_chunk" in main.SERVER_INSTRUCTIONS
    assert "list_session_groups" in main.SERVER_INSTRUCTIONS
    assert "save_exchange" in main.SERVER_INSTRUCTIONS
    assert "list_sessions" not in main.SERVER_INSTRUCTIONS
    assert "ask the user" in main.SERVER_INSTRUCTIONS.lower()
    assert "never enumerate" in main.SERVER_INSTRUCTIONS.lower()


def _load_main(tmp_path, monkeypatch, max_lines: int = 180, max_chars: int = 12000):
    monkeypatch.setenv("BRIDGE_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "bridge.sqlite3"))
    monkeypatch.setenv("BRIDGE_TRANSCRIPT_CHUNK_MAX_LINES", str(max_lines))
    monkeypatch.setenv("BRIDGE_TRANSCRIPT_CHUNK_MAX_CHARS", str(max_chars))
    monkeypatch.setenv("BRIDGE_OWNER_PASSWORD_HASH", "not-used-in-this-test")
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "test-secret")

    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def _tool_names(main) -> set[str]:
    async def list_names() -> set[str]:
        tools = await main.mcp.list_tools()
        return {tool.name for tool in tools}

    return asyncio.run(list_names())
