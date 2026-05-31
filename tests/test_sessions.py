import asyncio
import importlib
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.session_package import render_session_transcript
from app.storage import Store
from scripts.session_audit import build_viewer_payload


def test_store_saves_session_and_exchange(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Test session", "manual-context")

    exchange = store.save_exchange(
        session_id=session.session_id,
        model_name="Claude",
        user_message="Pierwsza wiadomość.",
        assistant_response="Odpowiada model Claude. Pierwsza odpowiedź.",
    )

    sessions = store.list_sessions()
    exchanges = store.list_exchanges("s1")

    assert exchange.exchange_id == 1
    assert exchange.assistant_created_at == exchange.created_at
    assert sessions[0]["exchange_count"] == 1
    assert exchanges[0].assistant_response.startswith("Odpowiada model Claude")
    assert exchanges[0].assistant_created_at == exchange.assistant_created_at


def test_store_soft_deletes_exchange_and_hides_it_from_transcript(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Correction test", "manual-context")
    first = store.save_exchange("s1", "Claude", "Pierwsza wiadomość.", "Pierwsza odpowiedź.")
    duplicate = store.save_exchange("s1", "Claude", "Duplikat.", "Druga odpowiedź dopisana drugi raz.")

    deleted = store.delete_exchange(duplicate.exchange_id, reason="duplikat", actor="wojtek")
    active_exchanges = store.list_exchanges("s1")
    all_exchanges = store.list_exchanges("s1", include_deleted=True)
    transcript = render_session_transcript(session, active_exchanges)
    sessions = store.list_sessions()
    events = store.list_exchange_events(duplicate.exchange_id)

    assert deleted.deleted_at is not None
    assert deleted.deleted_reason == "duplikat"
    assert [exchange.exchange_id for exchange in active_exchanges] == [first.exchange_id]
    assert all_exchanges[1].exchange_id == duplicate.exchange_id
    assert all_exchanges[1].deleted_at == deleted.deleted_at
    assert "Druga odpowiedź dopisana drugi raz." not in transcript["transcript_markdown"]
    assert sessions[0]["exchange_count"] == 1
    assert sessions[0]["deleted_exchange_count"] == 1
    assert events[-1]["action"] == "delete"
    assert events[-1]["actor"] == "wojtek"

    restored = store.restore_exchange(duplicate.exchange_id, actor="wojtek")

    assert restored.deleted_at is None
    assert [exchange.exchange_id for exchange in store.list_exchanges("s1")] == [first.exchange_id, duplicate.exchange_id]


def test_store_edits_exchange_and_records_admin_event(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Edit test", "manual-context")
    exchange = store.save_exchange("s1", "Claude", "Stara wiadomość.", "Stara odpowiedź.")

    edited = store.update_exchange(
        exchange.exchange_id,
        model_name="ChatGPT",
        user_message="Poprawiona wiadomość.",
        assistant_response="Poprawiona odpowiedź.",
        actor="wojtek",
    )
    transcript = render_session_transcript(session, store.list_exchanges("s1"))
    events = store.list_exchange_events(exchange.exchange_id)

    assert edited.model_name == "ChatGPT"
    assert edited.user_message == "Poprawiona wiadomość."
    assert edited.assistant_response == "Poprawiona odpowiedź."
    assert edited.edited_at is not None
    assert "Stara odpowiedź." not in transcript["transcript_markdown"]
    assert "Poprawiona odpowiedź." in transcript["transcript_markdown"]
    assert events[-1]["action"] == "edit"
    assert events[-1]["before"]["assistant_response"] == "Stara odpowiedź."
    assert events[-1]["after"]["assistant_response"] == "Poprawiona odpowiedź."

    with pytest.raises(ValueError, match="model_name"):
        store.update_exchange(exchange.exchange_id, model_name="   ")


def test_store_renames_auto_titled_session_from_first_exchange(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session("s1", "Sesja 2026-05-26 18:00 UTC", "manual-context", title_is_auto=True)

    store.save_exchange(
        session_id="s1",
        model_name="ChatGPT",
        user_message="Chcę porozmawiać o tym, jak opowiedzieć historię pracy bez korpojęzyka.",
        assistant_response="Odpowiada model ChatGPT. Jasne.",
    )

    session = store.get_session("s1")

    assert session is not None
    assert not session.title_is_auto
    assert session.title == "Chcę porozmawiać o tym, jak opowiedzieć historię pracy bez korpojęzyka"


def test_session_transcript_renders_conversation_without_context_pack(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Audit test", "manual-context")
    first_response_at = int(datetime(2026, 5, 26, 19, 21, tzinfo=ZoneInfo("Europe/Warsaw")).timestamp())
    second_response_at = int(datetime(2026, 5, 27, 8, 21, tzinfo=ZoneInfo("Europe/Warsaw")).timestamp())
    store.save_exchange(
        "s1",
        "ChatGPT",
        "Pierwsza wiadomość.",
        "Odpowiada model ChatGPT. Pierwsza odpowiedź.",
        assistant_created_at=first_response_at,
    )
    store.save_exchange(
        "s1",
        "Claude",
        "Druga wiadomość.",
        "Odpowiada model Claude. Druga odpowiedź.",
        assistant_created_at=second_response_at,
    )

    transcript = render_session_transcript(session, store.list_exchanges("s1"))

    assert transcript["turn_sequence"] == ["USER", "ChatGPT", "USER", "Claude"]
    assert transcript["exchange_count"] == 2
    assert transcript["turn_count"] == 4
    assert "context_pack_id" not in transcript["transcript_markdown"]
    assert "## Turn Sequence\n\nUSER\nChatGPT\nUSER\nClaude" in transcript["transcript_markdown"]
    assert "### ChatGPT - 19:21 (wtorek, 26 maja 2026)" in transcript["transcript_markdown"]
    assert "### Claude - 08:21 (środa, 27 maja 2026)" in transcript["transcript_markdown"]
    assert "<!-- created_at_display=19:21 (wtorek, 26 maja 2026) -->" in transcript["transcript_markdown"]
    assert "<!-- created_at_display=08:21 (środa, 27 maja 2026) -->" in transcript["transcript_markdown"]
    assert "Pierwsza wiadomość." in transcript["transcript_markdown"]
    assert "Odpowiada model Claude" in transcript["transcript_markdown"]


def test_store_migrates_existing_exchanges_with_response_timestamp(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    session = store.create_session("s1", "Migration source", "manual-context")
    store.save_exchange("s1", "Claude", "Stara wiadomość.", "Stara odpowiedź.")
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
        "Pokaż mi tę rozmowę w viewerze.",
        "Odpowiada model ChatGPT. Jasne, układam widok czatowy.",
    )

    payload = build_viewer_payload(store)

    assert payload["ok"] is True
    assert payload["schema"] == "mcp-session-bridge.viewer.v1"
    assert payload["session_count"] == 1
    assert payload["turn_count"] == 2
    assert payload["sessions"][0]["session_id"] == "s1"
    assert payload["transcripts"]["s1"]["turn_sequence"] == ["USER", "ChatGPT"]
    assert payload["transcripts"]["s1"]["turns"][0]["content"] == "Pokaż mi tę rozmowę w viewerze."


def test_public_tools_hide_context_pack_tools(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)

    tool_names = _tool_names(main)

    assert "get_session_overview" in tool_names
    assert "get_session_transcript_chunk" in tool_names
    assert "save_session_summary" in tool_names
    assert "list_session_summaries" in tool_names
    assert "list_context_packs" not in tool_names
    assert "get_session_package" not in tool_names
    assert "get_session_transcript" not in tool_names
    assert "save_context_summary" not in tool_names
    assert "export_session_markdown" not in tool_names


def test_create_session_works_without_context_pack_manifest(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)

    result = main.create_session("Manual context session")
    session = main.store.get_session(result["session_id"])

    assert result["ok"] is True
    assert result["context_source"] == "manual"
    assert "context_pack_id" not in result
    assert session is not None
    assert session.context_pack_id == "manual-context"


def test_session_overview_and_transcript_chunks_round_trip(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch, max_lines=8, max_chars=220)
    session = main.store.create_session("s1", "Chunk test", "manual-context")
    for index in range(1, 8):
        main.store.save_exchange(
            "s1",
            "Claude" if index % 2 else "ChatGPT",
            f"Wiadomość {index}\\n" + ("A" * 90),
            f"Odpowiedź {index}\\n" + ("B" * 110),
        )

    overview = main.get_session_overview("s1")
    chunks = [main.get_session_transcript_chunk("s1", index) for index in range(1, overview["transcript_chunk_count"] + 1)]
    full_transcript = render_session_transcript(session, main.store.list_exchanges("s1"))["transcript_markdown"]

    assert overview["ok"] is True
    assert overview["context_source"] == "manual"
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


def test_save_session_summary_writes_markdown_file_and_lists_metadata(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session("s1", "Summary test", "manual-context")

    empty_result = main.save_session_summary("s1", "Claude", "   ")
    unknown_result = main.save_session_summary("missing", "Claude", "## Podsumowanie")
    success_result = main.save_session_summary("s1", "Claude", "## Podsumowanie", "Własny tytuł")
    list_result = main.list_session_summaries("s1")

    assert empty_result == {"ok": False, "error": "summary_markdown must not be empty"}
    assert unknown_result == {"ok": False, "error": "Unknown session_id: missing"}
    assert success_result["ok"] is True
    assert success_result["session_id"] == "s1"
    assert success_result["title"] == "Własny tytuł"
    assert success_result["model_name"] == "Claude"
    assert Path(success_result["file_path"]).read_text(encoding="utf-8").strip() == "## Podsumowanie"
    assert list_result["summary_count"] == 1
    assert list_result["summaries"][0]["title"] == "Własny tytuł"
    assert list_result["summaries"][0]["sha256"] == success_result["sha256"]


def test_project_prompt_documents_manual_context_and_chunk_protocol() -> None:
    prompt = Path("docs/project-prompt-template.md").read_text(encoding="utf-8")

    assert "`get_session_overview`" in prompt
    assert "`get_session_transcript_chunk`" in prompt
    assert "`save_session_summary`" in prompt
    assert "`get_session_package`" not in prompt
    assert "context pack" not in prompt.lower()


def _load_main(tmp_path, monkeypatch, max_lines: int = 180, max_chars: int = 12000):
    monkeypatch.setenv("BRIDGE_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "bridge.sqlite3"))
    monkeypatch.setenv("BRIDGE_SUMMARIES_DIR", str(tmp_path / "summaries"))
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
