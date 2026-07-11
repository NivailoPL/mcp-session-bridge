import asyncio
import importlib
import sys
from datetime import datetime
from pathlib import Path
from datetime import UTC

import pytest

from app.session_package import render_session_transcript
from app.time_format import DISPLAY_TIMEZONE_SETTING_KEY
from app.storage import MAX_SESSION_FILE_BYTES, SessionFileConflictError, Store
from scripts.session_audit import build_viewer_payload


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
    assert [group["group_id"] for group in groups[:3]] == ["uncategorized", "brainstorming", "health"]

    ideas = store.create_session_group("Ideas", "#22c55e", "ideas")
    session = store.create_session("s1", "Grouped", "manual-context", group_id=ideas.group_id)

    assert ideas.group_id == "ideas"
    assert ideas.is_system is False
    assert session.group_id == "ideas"
    assert store.list_sessions()[0]["group"]["name"] == "Ideas"

    updated = store.update_session_group("ideas", name="Idea Lab", color="#0ea5e9", icon_key="brain")
    assert updated.name == "Idea Lab"
    assert updated.color == "#0ea5e9"
    assert updated.icon_key == "brain"

    with pytest.raises(ValueError, match="System session groups cannot be edited"):
        store.update_session_group("health", name="Wellness")
    with pytest.raises(ValueError, match="System session groups cannot be deleted"):
        store.delete_session_group("health")
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


def test_public_tools_hide_context_pack_tools(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)

    tool_names = _tool_names(main)

    assert "get_session_overview" in tool_names
    assert "get_last_speaker" in tool_names
    assert "get_session_transcript_chunk" in tool_names
    assert "list_session_groups" in tool_names
    assert "upload_session_file" in tool_names
    assert "upload_group_file" in tool_names
    assert "list_session_files" in tool_names
    assert "download_session_file" in tool_names
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
    groups_result = main.list_session_groups()
    result = main.create_session("Manual context session", group_id="ideas")
    invalid_result = main.create_session("Manual context session", group_id="missing")
    session = main.store.get_session(result["session_id"])

    assert groups_result["ok"] is True
    assert "ideas" in {group["group_id"] for group in groups_result["groups"]}
    assert result["ok"] is True
    assert result["group_id"] == "ideas"
    assert result["group"]["name"] == "Ideas"
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


def test_mcp_session_files_upload_list_download_and_show_in_overview(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session_group("Ideas", "#22c55e", "ideas")
    main.store.create_session("s1", "File test", "manual-context", group_id="ideas")

    session_file = main.upload_session_file("s1", "plan.md", "# Plan\n\nDo the thing.")
    group_file = main.upload_group_file("ideas", "context.md", "Shared group context.")
    listed = main.list_session_files(session_id="s1", group_id="ideas")
    downloaded = main.download_session_file(session_file["file"]["file_id"])
    overview = main.get_session_overview("s1")
    invalid = main.upload_group_file("missing", "x.md", "Nope.")

    assert session_file["ok"] is True
    assert session_file["file"]["scope_type"] == "session"
    assert session_file["file"]["filename"] == "plan.md"
    assert group_file["ok"] is True
    assert group_file["file"]["scope_type"] == "group"
    assert {file["filename"] for file in listed["files"]} == {"plan.md", "context.md"}
    assert downloaded["file"]["content"] == "# Plan\n\nDo the thing."
    assert overview["files"]["session"][0]["filename"] == "plan.md"
    assert overview["files"]["group"][0]["filename"] == "context.md"
    assert invalid["ok"] is False
    assert invalid["error"] == "Unknown session group: missing"


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
    assert "content" not in projection
    assert "*" not in projection
    assert listed[0]["filename"] == "large.md"


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
    }

    assert set(uploaded["file"]) == expected_manifest_keys
    assert set(main.list_session_files(session_id="s1")["files"][0]) == expected_manifest_keys
    assert set(main.download_session_file(file_id)["file"]) == expected_manifest_keys | {"content"}

    deleted = main.store.delete_session_file(file_id)

    assert deleted.file_id == file_id
    assert main.store.get_session_file(file_id) is None
    with pytest.raises(ValueError, match=f"Unknown file_id: {file_id}"):
        main.store.delete_session_file(file_id)
    assert main.list_session_files(session_id="s1")["files"] == []
    assert main.get_session_overview("s1")["files"]["session"] == []
    assert main.download_session_file(file_id) == {"ok": False, "error": f"Unknown file_id: {file_id}"}

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

    assert "`get_session_overview`" in prompt
    assert "`get_last_speaker`" in prompt
    assert "`get_session_transcript_chunk`" in prompt
    assert "`list_session_groups`" in prompt
    assert "`save_session_summary`" not in prompt
    assert "`upload_session_file`" in prompt
    assert "`upload_group_file`" in prompt
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
    assert "does not automatically ingest" in limitations
    assert "external files or directories" in limitations

    for model_doc in (instructions, prompt):
        assert "current file manifest is authoritative" in model_doc
        assert "not automatically notified" in model_doc
        assert "`list_session_files`" in model_doc
        assert "`download_session_file`" in model_doc


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
