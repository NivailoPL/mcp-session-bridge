from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from app import conversation_export
from app.conversation_export import (
    EXPORT_LOCK_FILENAME,
    ExportInProgressError,
    export_database_to_markdown,
)
from app.storage import Store


def test_export_writes_complete_audit_archive_and_original_files(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    group = store.create_session_group("Work / Research", "#123456", "book", "work")
    store.create_session(
        "session-001",
        "Project: Alpha",
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
    store.create_session("orphan-session", "Orphan")
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
    store.create_session("session-001", "Session")
    file = store.save_session_file("session-001", "notes.md", "actual")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE session_files SET sha256 = ? WHERE file_id = ?",
            ("0" * 64, file.file_id),
        )
    export_root = tmp_path / "exports"

    with pytest.raises(RuntimeError, match="checksum"):
        export_database_to_markdown(db_path, export_root=export_root)

    assert [
        path for path in export_root.iterdir() if path.name != EXPORT_LOCK_FILENAME
    ] == []


def test_export_rejects_lock_held_by_another_process_boundary(tmp_path: Path) -> None:
    try:
        import fcntl
    except ImportError:
        pytest.skip("POSIX file locking is not available")
    db_path = tmp_path / "bridge.sqlite3"
    Store(db_path)
    export_root = tmp_path / "exports"
    export_root.mkdir()
    descriptor = os.open(
        export_root / EXPORT_LOCK_FILENAME, os.O_RDONLY | os.O_CREAT, 0o640
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with pytest.raises(ExportInProgressError, match="already in progress"):
            export_database_to_markdown(db_path, export_root=export_root)
    finally:
        os.close(descriptor)


def test_export_normalizes_sqlite_failures_and_cleans_staging(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    Store(db_path)
    export_root = tmp_path / "exports"

    def fail_snapshot(_source: Path, _destination: Path) -> None:
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(conversation_export, "_snapshot_database", fail_snapshot)

    with pytest.raises(RuntimeError, match="SQLite database export failed"):
        export_database_to_markdown(db_path, export_root=export_root)

    assert [
        path for path in export_root.iterdir() if path.name != EXPORT_LOCK_FILENAME
    ] == []


def test_export_loads_text_attachment_content_only_when_writing_each_file(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    store.create_session("session-001", "Session")
    store.save_session_file("session-001", "notes.md", "content")
    observed_keys: list[set[str]] = []
    original_write_attachment = conversation_export._write_attachment

    def record_metadata(connection, file_row, destination) -> None:
        observed_keys.append(set(file_row.keys()))
        original_write_attachment(connection, file_row, destination)

    monkeypatch.setattr(conversation_export, "_write_attachment", record_metadata)

    export_database_to_markdown(db_path, export_root=tmp_path / "exports")

    assert observed_keys
    assert all("text_content" not in keys and "content" not in keys for keys in observed_keys)


def test_export_rejects_symlink_export_root(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    Store(db_path)
    real_root = tmp_path / "real-exports"
    real_root.mkdir()
    symlink_root = tmp_path / "exports"
    symlink_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        export_database_to_markdown(db_path, export_root=symlink_root)


def test_export_disambiguates_transcript_and_attachment_name_collision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    store.create_session(
        "session-001", "file-1--unsafe_"
    )
    store.save_session_file("session-001", "unsafe?.md", "attachment")

    result = export_database_to_markdown(db_path, export_root=tmp_path / "exports")
    group_dir = Path(result["artifact"]["path"]) / "uncategorized--Uncategorized"
    matching = sorted(path.name for path in group_dir.glob("session-001--file-1--unsafe_*.md"))

    assert len(matching) == 2
    assert "session-001--file-1--unsafe_.md" in matching
    assert any(name != "session-001--file-1--unsafe_.md" for name in matching)


def test_export_preserves_existing_shared_export_root_permissions(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    Store(db_path)
    export_root = tmp_path / "exports"
    export_root.mkdir(mode=0o770)
    export_root.chmod(0o770)

    export_database_to_markdown(db_path, export_root=export_root)

    assert export_root.stat().st_mode & 0o777 == 0o770


def test_export_reads_one_snapshot_even_if_live_database_changes(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    store.create_session("before-snapshot", "Before")
    original_snapshot = conversation_export._snapshot_database

    def snapshot_then_write(source: Path, destination: Path) -> None:
        original_snapshot(source, destination)
        store.create_session("after-snapshot", "After")

    monkeypatch.setattr(conversation_export, "_snapshot_database", snapshot_then_write)

    result = export_database_to_markdown(db_path, export_root=tmp_path / "exports")
    markdown_files = list(Path(result["artifact"]["path"]).glob("*/*.md"))

    assert result["artifact"]["session_count"] == 1
    assert any(path.name.startswith("before-snapshot--") for path in markdown_files)
    assert not any(path.name.startswith("after-snapshot--") for path in markdown_files)


def test_export_artifact_permissions_are_private(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not available on Windows")
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    store.create_session("session-001", "Session")
    store.save_session_file("session-001", "notes.md", "private")

    result = export_database_to_markdown(db_path, export_root=tmp_path / "exports")
    export_path = Path(result["artifact"]["path"])

    assert export_path.stat().st_mode & 0o777 == 0o700
    for directory in [path for path in export_path.rglob("*") if path.is_dir()]:
        assert directory.stat().st_mode & 0o777 == 0o700
    for file_path in [path for path in export_path.rglob("*") if path.is_file()]:
        assert file_path.stat().st_mode & 0o777 == 0o600
