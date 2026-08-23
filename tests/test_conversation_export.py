from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.conversation_export import export_database_to_markdown
from app.storage import Store


def test_export_writes_complete_audit_archive_and_original_files(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    group = store.create_session_group("Work / Research", "#123456", "book", "work")
    store.create_session(
        "session-001",
        "Project: Alpha",
        "manual-context",
        context_pack_version="7",
        group_id=group.group_id,
    )
    exchange = store.save_exchange(
        "session-001",
        "gpt-test",
        "User content with ``` inside",
        "RAW-MASKED-ASSISTANT",
        assistant_created_at=1_700_000_001,
    )
    store.update_exchange(exchange.exchange_id, user_message="Edited user content", actor="owner")
    store.mask_exchange_response(exchange.exchange_id, actor="owner")
    store.delete_exchange(exchange.exchange_id, reason="excluded by owner", actor="owner")
    session_file = store.save_session_file(
        "session-001", "notes/unsafe?.md", "session attachment", created_by="owner"
    )
    group_file = store.save_group_pdf(
        group.group_id,
        "source.pdf",
        b"%PDF-1.4\nORIGINAL-BYTES\n%%EOF",
        extracted_text="PDF text",
        page_count=1,
        extraction_status="ready",
        extracted_text_bytes=8,
        created_by="owner",
    )

    result = export_database_to_markdown(
        db_path,
        export_root=tmp_path / "exports",
        timestamp=1_700_000_100,
    )

    export_path = Path(result["artifact"]["path"])
    assert export_path.name == "mcp-bridge-export-20231114T221500Z"
    assert result["operation"] == "export.markdown"
    assert result["state"] == "complete"
    assert result["artifact"]["session_count"] == 1
    assert result["artifact"]["exchange_count"] == 1
    assert result["artifact"]["attachment_count"] == 2

    group_dir = export_path / "work--Work _ Research"
    transcript = group_dir / "session-001--Project_ Alpha.md"
    markdown = transcript.read_text(encoding="utf-8")
    assert "RAW-MASKED-ASSISTANT" in markdown
    assert "Edited user content" in markdown
    assert "excluded by owner" in markdown
    assert "mask_response" in markdown
    assert '"before"' not in markdown  # before/after are headings, not a lossy wrapper
    assert '"user_message": "User content with ``` inside"' in markdown
    assert f"session-001--file-{session_file.file_id}--unsafe_.md" in markdown
    assert f"_group--file-{group_file.file_id}--source.pdf" in markdown
    assert (
        group_dir / f"session-001--file-{session_file.file_id}--unsafe_.md"
    ).read_text(encoding="utf-8") == "session attachment"
    assert (
        group_dir / f"_group--file-{group_file.file_id}--source.pdf"
    ).read_bytes() == b"%PDF-1.4\nORIGINAL-BYTES\n%%EOF"


def test_export_keeps_empty_groups_and_puts_missing_groups_in_orphan_folder(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    store.create_session_group("Empty", "#123456", "folder", "empty")
    store.create_session("orphan-session", "Orphan", "manual-context")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "UPDATE sessions SET group_id = 'missing-group' WHERE session_id = 'orphan-session'"
        )

    result = export_database_to_markdown(db_path, export_root=tmp_path / "exports")
    export_path = Path(result["artifact"]["path"])

    assert (export_path / "empty--Empty").is_dir()
    orphan_dirs = list(export_path.glob("_orphaned--missing-group*"))
    assert len(orphan_dirs) == 1
    assert list(orphan_dirs[0].glob("orphan-session--*.md"))
    assert result["warnings"]


def test_export_rejects_existing_destination_without_changing_it(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    Store(db_path)
    destination = tmp_path / "already-there"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        export_database_to_markdown(db_path, output=destination)

    assert marker.read_text(encoding="utf-8") == "keep"


def test_export_checksum_failure_leaves_no_final_or_staging_artifact(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    store.create_session("session-001", "Session", "manual-context")
    file = store.save_session_file("session-001", "notes.md", "actual")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE session_files SET sha256 = ? WHERE file_id = ?",
            ("0" * 64, file.file_id),
        )
    export_root = tmp_path / "exports"

    with pytest.raises(RuntimeError, match="checksum"):
        export_database_to_markdown(db_path, export_root=export_root)

    assert list(export_root.iterdir()) == []


def test_export_disambiguates_transcript_and_attachment_name_collision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    store.create_session(
        "session-001", "file-1--unsafe_", "manual-context"
    )
    store.save_session_file("session-001", "unsafe?.md", "attachment")

    result = export_database_to_markdown(db_path, export_root=tmp_path / "exports")
    group_dir = Path(result["artifact"]["path"]) / "uncategorized--Uncategorized"
    matching = sorted(path.name for path in group_dir.glob("session-001--file-1--unsafe_*.md"))

    assert len(matching) == 2
    assert "session-001--file-1--unsafe_.md" in matching
    assert any(name != "session-001--file-1--unsafe_.md" for name in matching)
