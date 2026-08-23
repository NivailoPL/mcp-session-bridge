from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Iterator
from urllib.parse import quote

from app.storage import SCHEMA_VERSION


EXPORT_FORMAT_VERSION = 1
EXPORT_LOCK_FILENAME = ".markdown-export.lock"
_EXPORT_LOCK = Lock()
_UNSAFE_COMPONENT = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class ExportInProgressError(RuntimeError):
    pass


def export_database_to_markdown(
    db_path: Path,
    *,
    export_root: Path | None = None,
    output: Path | None = None,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Export the conversation archive to a new, private directory.

    ``output`` is an exact destination used by the CLI. When omitted,
    ``export_root`` receives a unique timestamped directory suitable for both
    the CLI default and the admin action. Existing destinations are never
    merged or overwritten.
    """

    if not _EXPORT_LOCK.acquire(blocking=False):
        raise ExportInProgressError("A Markdown database export is already in progress.")
    try:
        resolved_export_root = Path(export_root) if export_root is not None else None
        resolved_output = Path(output) if output is not None else None
        lock_parent = _export_parent(resolved_export_root, resolved_output)
        with _process_export_lock(lock_parent):
            return _export_database_to_markdown(
                Path(db_path),
                export_root=resolved_export_root,
                output=resolved_output,
                timestamp=int(timestamp if timestamp is not None else time.time()),
            )
    finally:
        _EXPORT_LOCK.release()


def _export_database_to_markdown(
    db_path: Path,
    *,
    export_root: Path | None,
    output: Path | None,
    timestamp: int,
) -> dict[str, Any]:
    source = db_path.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Bridge database does not exist: {source}")

    if output is not None:
        final = output.expanduser().resolve()
        parent = final.parent
        if not parent.is_dir():
            raise ValueError(f"Export parent directory does not exist: {parent}")
    else:
        if export_root is None:
            raise ValueError("export_root is required when output is not provided")
        parent = export_root.expanduser().resolve()
        _ensure_export_root(parent)
        final = _unique_default_destination(parent, timestamp)

    if final.exists() or final.is_symlink():
        raise ValueError(f"Export destination already exists: {final}")

    staging = Path(tempfile.mkdtemp(prefix=f".{final.name}.staging-", dir=parent))
    _chmod(staging, 0o700)
    snapshot = staging / ".snapshot.sqlite3"
    try:
        _snapshot_database(source, snapshot)
        exported_at = _iso(timestamp)
        with sqlite3.connect(snapshot) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            _validate_snapshot(connection)
            counts, warnings = _write_archive(connection, staging, exported_at)
        snapshot.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{snapshot}{suffix}").unlink(missing_ok=True)
        size_bytes = sum(path.stat().st_size for path in staging.rglob("*") if path.is_file())
        staging.rename(final)
    except sqlite3.Error as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("SQLite database export failed.") from exc
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "format_version": EXPORT_FORMAT_VERSION,
        "operation": "export.markdown",
        "state": "complete",
        "artifact": {
            "path": str(final),
            "kind": "directory",
            "group_count": counts["group_count"],
            "session_count": counts["session_count"],
            "exchange_count": counts["exchange_count"],
            "attachment_count": counts["attachment_count"],
            "size_bytes": size_bytes,
        },
        "warnings": warnings,
    }


def _snapshot_database(source: Path, destination: Path) -> None:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)
    _chmod(destination, 0o600)


def _validate_snapshot(connection: sqlite3.Connection) -> None:
    migration_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    version = (
        connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if migration_table
        else None
    )
    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema is {version!r}; Markdown export requires {SCHEMA_VERSION}."
        )
    quick_check = [row[0] for row in connection.execute("PRAGMA quick_check")]
    if quick_check != ["ok"]:
        raise RuntimeError(f"Database snapshot failed quick_check: {quick_check}")


def _write_archive(
    connection: sqlite3.Connection,
    staging: Path,
    exported_at: str,
) -> tuple[dict[str, int], list[str]]:
    groups = connection.execute(
        "SELECT * FROM session_groups ORDER BY sort_order, lower(name), group_id"
    ).fetchall()
    sessions = connection.execute(
        "SELECT * FROM sessions ORDER BY created_at, session_id"
    ).fetchall()
    exchange_count = connection.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
    files = connection.execute(
        """
        SELECT file_id, scope_type, session_id, group_id, filename, mime_type,
               sha256, size_bytes, created_by, created_at, content_kind,
               page_count, extraction_status, extracted_text_bytes
        FROM session_files
        ORDER BY created_at, file_id
        """
    ).fetchall()

    warnings: list[str] = []
    active_groups = {row["group_id"]: row for row in groups if row["deleted_at"] is None}
    all_groups = {row["group_id"]: row for row in groups}
    session_by_id = {row["session_id"]: row for row in sessions}

    required_group_ids: set[str | None] = set(active_groups)
    required_group_ids.update(row["group_id"] for row in sessions)
    required_group_ids.update(
        row["group_id"] for row in files if row["scope_type"] == "group"
    )

    group_directories: dict[str | None, Path] = {}
    used_group_names: set[str] = set()
    ordered_group_ids = sorted(
        required_group_ids,
        key=lambda group_id: (
            0 if group_id in active_groups else 1,
            active_groups[group_id]["sort_order"] if group_id in active_groups else 0,
            str(group_id or ""),
        ),
    )
    for group_id in ordered_group_ids:
        group = active_groups.get(group_id)
        if group is not None:
            preferred = _component_join(group["group_id"], group["name"])
            identity = f"group:{group['group_id']}"
        else:
            resolved_id = str(group_id or "no-group")
            preferred = _component_join("_orphaned", resolved_id)
            identity = f"orphan:{resolved_id}"
            historical = all_groups.get(group_id)
            detail = (
                f"deleted group {historical['name']!r}"
                if historical is not None
                else "missing group"
            )
            warnings.append(
                f"Exported content for {detail} {resolved_id!r} under an orphan folder."
            )
        name = _unique_component(preferred, identity, used_group_names)
        directory = staging / name
        _mkdir_private(directory)
        group_directories[group_id] = directory

    session_group_ids = {
        row["session_id"]: row["group_id"] for row in sessions
    }
    exported_files: dict[int, tuple[Path, str]] = {}
    files_by_session: dict[str, list[sqlite3.Row]] = {}
    files_by_group: dict[str | None, list[sqlite3.Row]] = {}

    for file_row in files:
        scope_type = file_row["scope_type"]
        if scope_type == "group":
            group_id = file_row["group_id"]
            directory = group_directories[group_id]
            prefix = f"_group--file-{file_row['file_id']}"
            files_by_group.setdefault(group_id, []).append(file_row)
        elif scope_type == "session":
            session_id = file_row["session_id"]
            if session_id not in session_by_id:
                missing_key = f"missing-session-{session_id or 'none'}"
                directory = group_directories.get(missing_key)
                if directory is None:
                    name = _unique_component(
                        _component_join("_orphaned", missing_key),
                        f"orphan-file:{missing_key}",
                        used_group_names,
                    )
                    directory = staging / name
                    _mkdir_private(directory)
                    group_directories[missing_key] = directory
                    warnings.append(
                        f"Exported files for missing session {session_id!r} under an orphan folder."
                    )
                prefix = f"_orphan-session--file-{file_row['file_id']}"
            else:
                group_id = session_group_ids[session_id]
                directory = group_directories[group_id]
                prefix = f"{_safe_component(session_id)}--file-{file_row['file_id']}"
                files_by_session.setdefault(session_id, []).append(file_row)
        else:
            raise RuntimeError(
                f"Unsupported session file scope {scope_type!r} for file {file_row['file_id']}"
            )

        exported_name = _attachment_name(prefix, file_row["filename"])
        destination = directory / exported_name
        _write_attachment(connection, file_row, destination)
        exported_files[file_row["file_id"]] = (directory, exported_name)

    used_names_by_directory: dict[Path, set[str]] = {}
    for directory, exported_name in exported_files.values():
        used_names_by_directory.setdefault(directory, set()).add(exported_name.casefold())
    for session in sessions:
        group_id = session["group_id"]
        directory = group_directories[group_id]
        preferred = _component_join(session["session_id"], session["title"], suffix=".md")
        name = _unique_component(
            preferred,
            f"session:{session['session_id']}",
            used_names_by_directory.setdefault(directory, set()),
            suffix=".md",
        )
        session_exchanges = connection.execute(
            "SELECT * FROM exchanges WHERE session_id = ? ORDER BY created_at, exchange_id",
            (session["session_id"],),
        ).fetchall()
        session_events = connection.execute(
            """
            SELECT * FROM exchange_admin_events
            WHERE session_id = ?
            ORDER BY created_at, event_id
            """,
            (session["session_id"],),
        ).fetchall()
        session_files = files_by_session.get(session["session_id"], [])
        group_files = files_by_group.get(group_id, [])
        markdown = _render_session_markdown(
            session,
            active_groups.get(group_id),
            exported_at,
            session_exchanges,
            session_events,
            session_files,
            group_files,
            exported_files,
        )
        _write_private_file(directory / name, markdown.encode("utf-8"))

    counts = {
        "group_count": len(group_directories),
        "session_count": len(sessions),
        "exchange_count": exchange_count,
        "attachment_count": len(files),
    }
    return counts, warnings


def _write_attachment(
    connection: sqlite3.Connection,
    file_row: sqlite3.Row,
    destination: Path,
) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("xb") as target:
            if file_row["content_kind"] == "pdf":
                try:
                    blob = connection.blobopen(
                        "session_files", "binary_content", file_row["file_id"], readonly=True
                    )
                except sqlite3.Error as exc:
                    raise RuntimeError(
                        f"PDF file {file_row['file_id']} has no readable original content."
                    ) from exc
                with blob:
                    while block := blob.read(1024 * 1024):
                        target.write(block)
                        digest.update(block)
                        size += len(block)
            elif file_row["content_kind"] == "text":
                content_row = connection.execute(
                    "SELECT content FROM session_files WHERE file_id = ?",
                    (file_row["file_id"],),
                ).fetchone()
                if content_row is None:
                    raise RuntimeError(f"Text file {file_row['file_id']} no longer exists.")
                data = (content_row["content"] or "").encode("utf-8")
                target.write(data)
                digest.update(data)
                size = len(data)
            else:
                raise RuntimeError(
                    f"Unsupported content kind {file_row['content_kind']!r} "
                    f"for file {file_row['file_id']}"
                )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    _chmod(destination, 0o600)
    if size != file_row["size_bytes"]:
        raise RuntimeError(
            f"Attachment {file_row['file_id']} size mismatch: {size} != {file_row['size_bytes']}"
        )
    if digest.hexdigest() != file_row["sha256"]:
        raise RuntimeError(f"Attachment {file_row['file_id']} checksum mismatch.")


def _render_session_markdown(
    session: sqlite3.Row,
    group: sqlite3.Row | None,
    exported_at: str,
    exchanges: list[sqlite3.Row],
    events: list[sqlite3.Row],
    session_files: list[sqlite3.Row],
    group_files: list[sqlite3.Row],
    exported_files: dict[int, tuple[Path, str]],
) -> str:
    active_count = sum(row["deleted_at"] is None for row in exchanges)
    excluded_count = len(exchanges) - active_count
    masked_count = sum(row["assistant_masked_at"] is not None for row in exchanges)
    lines = [
        f"# {_heading(session['title'])}",
        "",
        "## Archive metadata",
        "",
        f"- **Format version:** {EXPORT_FORMAT_VERSION}",
        f"- **Exported at:** {_inline(exported_at)}",
        f"- **Session ID:** {_inline(session['session_id'])}",
        f"- **Current group ID:** {_inline(session['group_id'])}",
        f"- **Current group name:** {_inline(group['name'] if group else 'Orphaned')}",
        f"- **Sensitive group:** {'yes' if group and group['is_sensitive'] else 'no'}",
        f"- **Context pack:** {_inline(session['context_pack_id'])}",
        f"- **Context pack version:** {_inline(session['context_pack_version'])}",
        f"- **Title generated automatically:** {'yes' if session['title_is_auto'] else 'no'}",
        f"- **Created at:** {_inline(_iso(session['created_at']))}",
        f"- **Updated at:** {_inline(_iso(session['updated_at']))}",
        f"- **Exchanges:** {len(exchanges)} total; {active_count} active; "
        f"{excluded_count} excluded; {masked_count} masked",
        "",
        "## Transcript",
        "",
    ]
    if not exchanges:
        lines.extend(["_No exchanges stored._", ""])
    for exchange in exchanges:
        states = []
        if exchange["deleted_at"] is not None:
            states.append("excluded")
        else:
            states.append("active")
        if exchange["assistant_masked_at"] is not None:
            states.append("assistant response masked")
        if exchange["edited_at"] is not None:
            states.append("edited")
        lines.extend(
            [
                f"### Exchange {exchange['exchange_id']}",
                "",
                f"- **Model:** {_inline(exchange['model_name'])}",
                f"- **State:** {', '.join(states)}",
                f"- **User message created at:** {_inline(_iso(exchange['created_at']))}",
                f"- **Assistant response created at:** "
                f"{_inline(_iso(exchange['assistant_created_at'] or exchange['created_at']))}",
                f"- **Edited at:** {_inline(_iso(exchange['edited_at']))}",
                f"- **Masked at:** {_inline(_iso(exchange['assistant_masked_at']))}",
                f"- **Excluded at:** {_inline(_iso(exchange['deleted_at']))}",
                f"- **Exclusion reason:** {_inline(exchange['deleted_reason'])}",
                "",
                "#### User",
                "",
                exchange["user_message"],
                "",
                "#### Assistant",
                "",
                exchange["assistant_response"],
                "",
            ]
        )

    lines.extend(["## Files", ""])
    if not session_files and not group_files:
        lines.extend(["_No session or current-group files stored._", ""])
    else:
        for label, rows in (("Session files", session_files), ("Current-group files", group_files)):
            if not rows:
                continue
            lines.extend([f"### {label}", ""])
            for file_row in rows:
                _, exported_name = exported_files[file_row["file_id"]]
                lines.extend(
                    [
                        f"- [{_link_label(exported_name)}]({quote(exported_name)})",
                        f"  - Original name: {_inline(file_row['filename'])}",
                        f"  - File ID: {file_row['file_id']}",
                        f"  - MIME type: {_inline(file_row['mime_type'])}",
                        f"  - Size: {file_row['size_bytes']} bytes",
                        f"  - SHA-256: {_inline(file_row['sha256'])}",
                        f"  - Created by: {_inline(file_row['created_by'])}",
                        f"  - Created at: {_inline(_iso(file_row['created_at']))}",
                    ]
                )
            lines.append("")

    lines.extend(["## Exchange audit history", ""])
    if not events:
        lines.extend(["_No exchange administration events stored._", ""])
    for event in events:
        lines.extend(
            [
                f"### Event {event['event_id']}: {_heading(event['action'])}",
                "",
                f"- **Exchange ID:** {event['exchange_id']}",
                f"- **Actor:** {_inline(event['actor'])}",
                f"- **Created at:** {_inline(_iso(event['created_at']))}",
                "",
                "#### Before",
                "",
                _json_block(event["before_json"]),
                "",
                "#### After",
                "",
                _json_block(event["after_json"]),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _json_block(raw: str | None) -> str:
    value = json.loads(raw) if raw else None
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    fence = "`" * max(3, _longest_run(text, "`") + 1)
    return f"{fence}json\n{text}\n{fence}"


def _longest_run(value: str, character: str) -> int:
    longest = current = 0
    for item in value:
        if item == character:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _unique_default_destination(parent: Path, timestamp: int) -> Path:
    base = parent / f"mcp-bridge-export-{datetime.fromtimestamp(timestamp, UTC):%Y%m%dT%H%M%SZ}"
    candidate = base
    suffix = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = base.with_name(f"{base.name}-{suffix}")
        suffix += 1
    return candidate


def _component_join(*parts: Any, suffix: str = "") -> str:
    safe_parts = [_safe_component(str(part)) for part in parts]
    return _truncate_utf8("--".join(safe_parts), 180 - len(suffix.encode("utf-8"))) + suffix


def _attachment_name(prefix: str, original_name: str) -> str:
    safe_original = _safe_component(original_name, max_bytes=110)
    return _truncate_utf8(f"{prefix}--{safe_original}", 180)


def _safe_component(value: str, *, max_bytes: int = 96) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _UNSAFE_COMPONENT.sub("_", normalized)
    normalized = normalized.strip(" .")
    if not normalized or normalized in {".", ".."}:
        normalized = "unnamed"
    if normalized.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        normalized = f"_{normalized}"
    return _truncate_utf8(normalized, max_bytes)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    digest = hashlib.sha256(encoded).hexdigest()[:8]
    budget = max(1, max_bytes - len(digest) - 2)
    truncated = encoded[:budget]
    while True:
        try:
            prefix = truncated.decode("utf-8")
            break
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return f"{prefix.rstrip(' .')}--{digest}"


def _unique_component(
    preferred: str,
    identity: str,
    used: set[str],
    *,
    suffix: str = "",
) -> str:
    key = preferred.casefold()
    if key not in used:
        used.add(key)
        return preferred
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    base = preferred[: -len(suffix)] if suffix and preferred.endswith(suffix) else preferred
    candidate = _truncate_utf8(base, 180 - len(suffix.encode("utf-8")) - 10)
    candidate = f"{candidate}--{digest}{suffix}"
    used.add(candidate.casefold())
    return candidate


def _heading(value: Any) -> str:
    return str(value if value is not None else "").replace("\n", " ").replace("#", "\\#")


def _inline(value: Any) -> str:
    if value is None:
        return "—"
    text = str(value).replace("\n", " ")
    fence = "`" * max(1, _longest_run(text, "`") + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _link_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _iso(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), UTC).isoformat().replace("+00:00", "Z")


def _mkdir_private(path: Path, *, parents: bool = False) -> None:
    path.mkdir(mode=0o700, parents=parents, exist_ok=True)
    _chmod(path, 0o700)


def _ensure_export_root(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Export root must not be a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"Export root is not a directory: {path}")
        return
    _mkdir_private(path, parents=True)


def _export_parent(export_root: Path | None, output: Path | None) -> Path:
    if output is not None:
        parent = output.expanduser().resolve().parent
        if not parent.is_dir():
            raise ValueError(f"Export parent directory does not exist: {parent}")
        return parent
    if export_root is None:
        raise ValueError("export_root is required when output is not provided")
    expanded = export_root.expanduser()
    _ensure_export_root(expanded)
    return expanded.resolve()


@contextmanager
def _process_export_lock(parent: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows keeps the in-process lock.
        yield
        return

    flags = os.O_RDONLY | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(parent / EXPORT_LOCK_FILENAME, flags, 0o640)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExportInProgressError(
                "A Markdown database export is already in progress."
            ) from exc
        yield
    finally:
        os.close(descriptor)


def _write_private_file(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
    _chmod(path, 0o600)


def _chmod(path: Path, mode: int) -> None:
    if os.name != "nt":
        path.chmod(mode)
