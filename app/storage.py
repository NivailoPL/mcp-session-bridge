from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from app.anchor_store import ANCHOR_LATER_COLUMNS, ANCHOR_SCHEMA_SQL, AnchorStoreMixin
from app.topic_store import TOPIC_LATER_COLUMNS, TOPIC_SCHEMA_SQL, TopicStoreMixin
from app.session_summaries import SUMMARY_SCHEMA_SQL
from app.image_files import ValidatedImage

from app.time_format import format_timestamp_iso
from app.tool_output import (
    DEFAULT_TOOL_OUTPUT_MODE,
    TOOL_OUTPUT_MODE_MAXIMUM_COMPATIBILITY,
    TOOL_OUTPUT_MODE_SETTING,
    TOOL_OUTPUT_MODES,
    TOOL_OUTPUT_RESTART_PENDING_SETTING,
)

UNCATEGORIZED_GROUP_ID = "uncategorized"
SCHEMA_VERSION = 2

# Written into the retired sessions.context_pack_id column, which is NOT NULL and
# cannot be dropped without a schema bump. Nothing reads this value.
RETIRED_CONTEXT_PACK_ID = "manual-context"

SYSTEM_SESSION_GROUPS = (
    {
        "group_id": UNCATEGORIZED_GROUP_ID,
        "name": "Uncategorized",
        "color": "#8b93a7",
        "icon_key": "folder",
        "sort_order": 0,
    },
)

SESSION_GROUP_ICON_KEYS = {
    "folder",
    "money",
    "book",
    "graduation",
    "pencil",
    "code",
    "terminal",
    "music",
    "food",
    "palette",
    "medical_plus",
    "tools",
    "travel",
    "world",
    "legal",
    "science",
    "ideas",
    "heart",
    "plants",
    "brain",
    "archive",
    "star",
    "calendar",
    "clock",
    "chat",
    "users",
    "person",
    "home",
    "briefcase",
    "camera",
    "video",
    "microphone",
    "phone",
    "mail",
    "shopping",
    "gift",
    "game",
    "rocket",
    "shield",
    "lock",
    "key",
    "map",
    "pin",
    "car",
    "plane",
    "coffee",
    "fitness",
    "pet",
}

MAX_SESSION_FILE_BYTES = 1_000_000
SESSION_FILE_MANIFEST_COLUMNS = """
    file_id, scope_type, session_id, group_id, filename, mime_type,
    sha256, size_bytes, created_by, created_at, content_kind, page_count,
    extraction_status, extracted_text_bytes
"""
SESSION_FILE_RECORD_COLUMNS = f"{SESSION_FILE_MANIFEST_COLUMNS}, content"
FILE_LABEL_COLORS = ("yellow", "purple", "pink", "sky", "lime", "orange")
MAX_FILE_LABELS_PER_GROUP = 24
MAX_FILE_LABEL_NAME_CHARS = 40
MAX_CONTEXT_NAME_CHARS = 80
MAX_CONTEXTS = 200
CONTEXT_ID_PREFIX = "ctx_"
CONTEXT_ID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
CONTEXT_ID_LENGTH = 16
CONTEXT_BLOCK_COLUMNS = (
    "block_id, context_id, position, source_type, source_key, source_session_id, "
    "source_file_id, title, source_hash, found_ids, token_count, snapshot_at, created_at, source_meta"
)
CONTEXT_SOURCE_TYPES = ("session", "file", "topic")
FILE_PREVIEW_CHARS = 600


class SessionFileConflictError(ValueError):
    """Raised when a guarded file edit targets content that has changed."""


class PdfStorageQuotaError(ValueError):
    """Raised when saving a PDF would exceed the configured durable quota."""


@dataclass(frozen=True)
class ClientRecord:
    client_id: str
    client_secret_hash: str | None
    auth_method: str
    redirect_uris: list[str]
    grant_types: list[str]
    response_types: list[str]
    scope: str
    client_name: str | None
    secret_expires_at: int | None
    created_at: int | None = None
    # Set the first time the owner signs a client in; the connection lock spares it after that.
    approved_at: int | None = None


@dataclass(frozen=True)
class ChallengeRecord:
    challenge: str
    client_id: str
    redirect_uri: str
    scope: str
    state: str | None
    code_challenge: str
    resource: str | None
    expires_at: int


@dataclass(frozen=True)
class CodeRecord:
    code_hash: str
    client_id: str
    redirect_uri: str
    scopes: list[str]
    code_challenge: str
    resource: str | None
    expires_at: int
    used_at: int | None


@dataclass(frozen=True)
class TokenRecord:
    token_hash: str
    client_id: str
    scopes: list[str]
    resource: str | None
    expires_at: int | None
    revoked_at: int | None


@dataclass(frozen=True)
class SessionGroupRecord:
    group_id: str
    name: str
    color: str
    icon_key: str
    sort_order: int
    is_system: bool
    is_sensitive: bool
    created_at: int
    updated_at: int
    deleted_at: int | None = None


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    title: str
    group_id: str
    title_is_auto: bool
    created_at: int
    updated_at: int
    pinned_at: int | None = None
    title_source: str | None = None


@dataclass(frozen=True)
class ExchangeRecord:
    exchange_id: int
    session_id: str
    model_name: str
    user_message: str
    assistant_response: str
    assistant_created_at: int
    created_at: int
    assistant_masked_at: int | None = None
    deleted_at: int | None = None
    deleted_reason: str | None = None
    edited_at: int | None = None


@dataclass(frozen=True)
class SessionFileRecord:
    file_id: int
    scope_type: str
    session_id: str | None
    group_id: str | None
    filename: str
    mime_type: str
    content: str
    sha256: str
    size_bytes: int
    created_by: str
    created_at: int
    content_kind: str
    page_count: int | None
    extraction_status: str
    extracted_text_bytes: int


class ImageStorageQuotaError(ValueError):
    pass


class ContextNotFoundError(ValueError):
    pass


class ContextLockedError(ValueError):
    pass


class ContextBlockConflictError(ValueError):
    pass


@dataclass(frozen=True)
class ContextBlockSnapshot:
    """The text of one source at the moment it was dropped into a context."""

    source_type: str
    source_key: str
    source_session_id: str | None
    source_file_id: int | None
    title: str
    content: str
    source_hash: str
    found_ids: dict[str, list[str]]
    token_count: int
    # Topic blocks only: which turns came from which session, the map version and options.
    meta: dict[str, Any] = field(default_factory=dict)


class Store(AnchorStoreMixin, TopicStoreMixin):
    def __init__(
        self,
        db_path: Path,
        *,
        pdf_storage_max_bytes: int = 1_000_000_000,
        image_storage_max_bytes: int = 1_000_000_000,
        allow_startup_migrations: bool = True,
    ):
        self.db_path = db_path
        self.pdf_storage_max_bytes = pdf_storage_max_bytes
        if image_storage_max_bytes <= 0:
            raise ValueError("Image storage quota must be positive")
        self.image_storage_max_bytes = image_storage_max_bytes
        self._lock = Lock()
        if not allow_startup_migrations:
            self._require_current_schema()
        parent_created = not self.db_path.parent.exists()
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent_created and os.name != "nt":
            self.db_path.parent.chmod(0o700)
        self._harden_runtime_permissions()
        if allow_startup_migrations:
            self._init_schema()
        self._harden_runtime_permissions()

    def _harden_runtime_permissions(self) -> None:
        if os.name == "nt":
            return

        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            if path.exists():
                path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._harden_runtime_permissions()
        return conn

    def _require_current_schema(self) -> None:
        if not self.db_path.exists():
            raise RuntimeError(
                "Database is not initialized. Run mcp-bridge migrate before starting Bridge."
            )
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
                version = (
                    conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                    if table
                    else None
                )
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Could not read database schema. Run mcp-bridge migrate: {exc}"
            ) from exc
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema is {version!r}; Bridge requires {SCHEMA_VERSION}. "
                "Run mcp-bridge migrate before starting Bridge."
            )

    def schema_version(self) -> int | None:
        with sqlite3.connect(self.db_path) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not table:
                return None
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            return row[0] if row else None

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    client_secret_hash TEXT,
                    auth_method TEXT NOT NULL,
                    redirect_uris TEXT NOT NULL,
                    grant_types TEXT NOT NULL,
                    response_types TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    client_name TEXT,
                    created_at INTEGER NOT NULL,
                    secret_expires_at INTEGER,
                    approved_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS auth_challenges (
                    challenge TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    state TEXT,
                    code_challenge TEXT NOT NULL,
                    resource TEXT,
                    expires_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    resource TEXT,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS access_tokens (
                    token_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    resource TEXT,
                    expires_at INTEGER,
                    created_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    resource TEXT,
                    expires_at INTEGER,
                    created_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS probe (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS output_probe_runs (
                    run_id TEXT PRIMARY KEY,
                    harness_label TEXT NOT NULL,
                    content_profile TEXT NOT NULL,
                    target_chars INTEGER NOT NULL,
                    payload_char_count INTEGER NOT NULL,
                    server_result_json_chars INTEGER NOT NULL,
                    block_count INTEGER NOT NULL,
                    block_size INTEGER NOT NULL,
                    tool_output_mode TEXT NOT NULL DEFAULT '{TOOL_OUTPUT_MODE_MAXIMUM_COMPATIBILITY}',
                    blocks_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_class TEXT,
                    observed_canaries_json TEXT NOT NULL DEFAULT '[]',
                    matched_checkpoints_json TEXT NOT NULL DEFAULT '[]',
                    last_complete_block_index INTEGER,
                    last_complete_canary TEXT,
                    noticed_truncation INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    reported_at INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_output_probe_runs_created
                    ON output_probe_runs(created_at DESC, run_id DESC);

                CREATE TABLE IF NOT EXISTS session_groups (
                    group_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    color TEXT NOT NULL,
                    icon_key TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 100,
                    is_system INTEGER NOT NULL DEFAULT 0,
                    is_sensitive INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    deleted_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT 'uncategorized',
                    -- Retired context-pack columns. Nothing reads them; they are kept
                    -- so this schema stays version 2 and a rollback to an older Bridge
                    -- still opens the database. Drop them only with a schema bump.
                    context_pack_id TEXT NOT NULL,
                    context_pack_version TEXT,
                    title_is_auto INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    pinned_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS exchanges (
                    exchange_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    assistant_response TEXT NOT NULL,
                    assistant_created_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    assistant_masked_at INTEGER,
                    deleted_at INTEGER,
                    deleted_reason TEXT,
                    edited_at INTEGER,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS session_files (
                    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_type TEXT NOT NULL,
                    session_id TEXT,
                    group_id TEXT,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    binary_content BLOB,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    content_kind TEXT NOT NULL DEFAULT 'text',
                    page_count INTEGER,
                    extraction_status TEXT NOT NULL DEFAULT 'not_applicable',
                    extracted_text_bytes INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_exchanges_session_created
                    ON exchanges(session_id, created_at, exchange_id);

                CREATE INDEX IF NOT EXISTS idx_session_files_scope
                    ON session_files(scope_type, session_id, group_id, created_at);

                -- Admin-only file labels. They belong to a session group, are never
                -- shown to models, and older Bridge versions simply ignore them.
                CREATE TABLE IF NOT EXISTS file_labels (
                    label_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    color TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_file_labels_group_name
                    ON file_labels(group_id, name COLLATE NOCASE);

                CREATE TABLE IF NOT EXISTS session_file_labels (
                    file_id INTEGER NOT NULL,
                    label_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(file_id, label_id),
                    FOREIGN KEY(file_id) REFERENCES session_files(file_id) ON DELETE CASCADE,
                    FOREIGN KEY(label_id) REFERENCES file_labels(label_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_session_file_labels_label
                    ON session_file_labels(label_id);

                -- Contexts: admin-built bundles of session and file snapshots that a
                -- model reads by context_id. Blocks keep their own copy of the source
                -- text, so they hold no foreign key to sessions or files and survive
                -- the source being edited or deleted. Older Bridge versions ignore both.
                CREATE TABLE IF NOT EXISTS contexts (
                    context_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    color TEXT NOT NULL,
                    is_locked INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS context_blocks (
                    block_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    context_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    source_type TEXT NOT NULL CHECK (source_type IN ('session', 'file', 'topic')),
                    source_key TEXT NOT NULL,
                    source_session_id TEXT,
                    source_file_id INTEGER,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    found_ids TEXT NOT NULL DEFAULT '{{}}',
                    token_count INTEGER NOT NULL DEFAULT 0,
                    snapshot_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    source_meta TEXT NOT NULL DEFAULT '{{}}',
                    FOREIGN KEY(context_id) REFERENCES contexts(context_id) ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_context_blocks_source
                    ON context_blocks(context_id, source_key);

                CREATE INDEX IF NOT EXISTS idx_context_blocks_position
                    ON context_blocks(context_id, position);

                CREATE TABLE IF NOT EXISTS exchange_admin_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    created_at INTEGER NOT NULL
                );

                -- Titles written by automatic naming, for the Codex activity panel.
                -- Additive and ignored by older Bridges, so it needs no schema bump.
                CREATE TABLE IF NOT EXISTS session_title_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    old_title TEXT NOT NULL,
                    new_title TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    estimated_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_session_title_events_created
                    ON session_title_events(created_at);
                """
            )
            # Admin-only highlights and strands (app.anchor_store); additive, no schema bump.
            conn.executescript(ANCHOR_SCHEMA_SQL)
            conn.executescript(TOPIC_SCHEMA_SQL)
            conn.executescript(SUMMARY_SCHEMA_SQL)
            self._allow_topic_context_blocks(conn)
            for table, column, declaration in TOPIC_LATER_COLUMNS:
                self._ensure_column(conn, table, column, declaration)
            for table, column, declaration in ANCHOR_LATER_COLUMNS:
                self._ensure_column(conn, table, column, declaration)
            self._ensure_column(conn, "refresh_tokens", "resource", "TEXT")
            if self._ensure_column(conn, "clients", "approved_at", "INTEGER"):
                # Clients that already hold tokens were signed in by the owner; keep them connected.
                conn.execute(
                    """
                    UPDATE clients SET approved_at = created_at
                    WHERE client_id IN (
                        SELECT client_id FROM access_tokens UNION SELECT client_id FROM refresh_tokens
                    )
                    """
                )
            self._ensure_column(conn, "sessions", "context_pack_version", "TEXT")
            self._ensure_column(conn, "sessions", "title_is_auto", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sessions", "group_id", f"TEXT NOT NULL DEFAULT '{UNCATEGORIZED_GROUP_ID}'")
            # Nullable and ignored by older Bridges, so it needs no schema bump.
            self._ensure_column(conn, "sessions", "pinned_at", "INTEGER")
            # Who wrote the title: "derived" (cut from the first message), "manual", "ai" or "auto".
            # NULL on older rows; automatic naming only touches "derived" titles.
            self._ensure_column(conn, "sessions", "title_source", "TEXT")
            # Until when the owner reopened a closed session; NULL when never reopened.
            self._ensure_column(conn, "sessions", "reopened_until", "INTEGER")
            self._ensure_column(conn, "session_groups", "is_sensitive", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "exchanges", "assistant_created_at", "INTEGER")
            self._ensure_column(conn, "exchanges", "assistant_masked_at", "INTEGER")
            self._ensure_column(conn, "exchanges", "deleted_at", "INTEGER")
            self._ensure_column(conn, "exchanges", "deleted_reason", "TEXT")
            self._ensure_column(conn, "exchanges", "edited_at", "INTEGER")
            self._ensure_column(
                conn,
                "output_probe_runs",
                "tool_output_mode",
                f"TEXT NOT NULL DEFAULT '{TOOL_OUTPUT_MODE_MAXIMUM_COMPATIBILITY}'",
            )
            self._ensure_column(conn, "session_files", "binary_content", "BLOB")
            self._ensure_column(conn, "session_files", "content_kind", "TEXT NOT NULL DEFAULT 'text'")
            self._ensure_column(conn, "session_files", "page_count", "INTEGER")
            self._ensure_column(
                conn,
                "session_files",
                "extraction_status",
                "TEXT NOT NULL DEFAULT 'not_applicable'",
            )
            self._ensure_column(
                conn,
                "session_files",
                "extracted_text_bytes",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._seed_system_session_groups(conn)
            self._demote_legacy_system_session_groups(conn)
            conn.execute(
                """
                UPDATE sessions
                SET group_id = ?
                WHERE group_id IS NULL OR group_id = ''
                """,
                (UNCATEGORIZED_GROUP_ID,),
            )
            conn.execute(
                """
                UPDATE exchanges
                SET assistant_created_at = created_at
                WHERE assistant_created_at IS NULL
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, int(time.time())),
            )

    @staticmethod
    def _allow_topic_context_blocks(conn: sqlite3.Connection) -> None:
        """Older databases limit context blocks to sessions and files with a CHECK, which
        SQLite cannot alter; rebuild the table once with topics allowed. Additive: every
        row and index is kept, so no schema bump."""
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'context_blocks'").fetchone()
        if row is None or "'topic'" in row[0]:
            return
        columns = [info["name"] for info in conn.execute("PRAGMA table_info(context_blocks)")]
        keep = ", ".join(column for column in columns if column != "source_meta")
        conn.executescript(
            f"""
            BEGIN;
            CREATE TABLE context_blocks_rebuilt (
                block_id INTEGER PRIMARY KEY AUTOINCREMENT,
                context_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                source_type TEXT NOT NULL CHECK (source_type IN ('session', 'file', 'topic')),
                source_key TEXT NOT NULL,
                source_session_id TEXT,
                source_file_id INTEGER,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                found_ids TEXT NOT NULL DEFAULT '{{}}',
                token_count INTEGER NOT NULL DEFAULT 0,
                snapshot_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                source_meta TEXT NOT NULL DEFAULT '{{}}',
                FOREIGN KEY(context_id) REFERENCES contexts(context_id) ON DELETE CASCADE
            );
            INSERT INTO context_blocks_rebuilt ({keep}) SELECT {keep} FROM context_blocks;
            DROP TABLE context_blocks;
            ALTER TABLE context_blocks_rebuilt RENAME TO context_blocks;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_context_blocks_source ON context_blocks(context_id, source_key);
            CREATE INDEX IF NOT EXISTS idx_context_blocks_position ON context_blocks(context_id, position);
            COMMIT;
            """
        )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> bool:
        """Add the column when it is missing; return True when it was added just now."""
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in columns:
            return False
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        return True

    def register_client(self, record: ClientRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO clients (
                    client_id, client_secret_hash, auth_method, redirect_uris,
                    grant_types, response_types, scope, client_name, created_at,
                    secret_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.client_id,
                    record.client_secret_hash,
                    record.auth_method,
                    json.dumps(record.redirect_uris),
                    json.dumps(record.grant_types),
                    json.dumps(record.response_types),
                    record.scope,
                    record.client_name,
                    int(time.time()),
                    record.secret_expires_at,
                ),
            )
    def get_client(self, client_id: str) -> ClientRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        if not row:
            return None
        return ClientRecord(
            client_id=row["client_id"],
            client_secret_hash=row["client_secret_hash"],
            auth_method=row["auth_method"],
            redirect_uris=json.loads(row["redirect_uris"]),
            grant_types=json.loads(row["grant_types"]),
            response_types=json.loads(row["response_types"]),
            scope=row["scope"],
            client_name=row["client_name"],
            secret_expires_at=row["secret_expires_at"],
            created_at=row["created_at"],
            approved_at=row["approved_at"],
        )

    def approve_client(self, client_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE clients SET approved_at = ? WHERE client_id = ? AND approved_at IS NULL",
                (int(time.time()), client_id),
            )

    def list_connected_clients(self) -> list[dict[str, Any]]:
        """Approved clients with their newest token, newest activity first."""
        now = int(time.time())
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.client_id, c.client_name, c.redirect_uris, c.created_at, c.approved_at,
                       -- tokens are issued whenever the harness signs in or refreshes; access tokens
                       -- are purged soon after they expire, so the rotating refresh token counts too
                       (
                           SELECT MAX(issued) FROM (
                               SELECT MAX(created_at) AS issued FROM access_tokens a WHERE a.client_id = c.client_id
                               UNION ALL
                               SELECT MAX(created_at) FROM refresh_tokens r WHERE r.client_id = c.client_id
                           )
                       ) AS last_active_at,
                       EXISTS (
                           SELECT 1 FROM refresh_tokens r
                           WHERE r.client_id = c.client_id AND r.revoked_at IS NULL
                             AND (r.expires_at IS NULL OR r.expires_at > ?)
                       ) AS has_live_token
                FROM clients c
                WHERE c.approved_at IS NOT NULL
                ORDER BY COALESCE(last_active_at, c.approved_at) DESC, c.client_id
                """,
                (now,),
            ).fetchall()
        return [
            {
                "client_id": row["client_id"],
                "client_name": row["client_name"],
                "redirect_uris": json.loads(row["redirect_uris"]),
                "created_at": row["created_at"],
                "approved_at": row["approved_at"],
                "last_active_at": row["last_active_at"],
                "has_live_token": bool(row["has_live_token"]),
            }
            for row in rows
        ]

    def disconnect_client(self, client_id: str) -> bool:
        """Delete a client with every code and token it holds; its bearer tokens stop working at once."""
        with self._lock, self._connect() as conn:
            for table in ("access_tokens", "refresh_tokens", "auth_codes", "auth_challenges"):
                conn.execute(f"DELETE FROM {table} WHERE client_id = ?", (client_id,))
            return conn.execute("DELETE FROM clients WHERE client_id = ?", (client_id,)).rowcount > 0

    def count_pending_oauth(self) -> dict[str, int]:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            challenges = conn.execute(
                "SELECT COUNT(*) FROM auth_challenges WHERE expires_at >= ?", (now,)
            ).fetchone()[0]
            unapproved = conn.execute("SELECT COUNT(*) FROM clients WHERE approved_at IS NULL").fetchone()[0]
        return {"challenges": int(challenges), "unapproved_clients": int(unapproved)}

    def purge_oauth(self, *, unapproved_client_seconds: int = 24 * 60 * 60, revoked_grace_seconds: int = 24 * 60 * 60) -> dict[str, int]:
        """Drop expired OAuth state and clients that registered but were never signed in."""
        now = int(time.time())
        with self._lock, self._connect() as conn:
            removed = {
                "challenges": conn.execute("DELETE FROM auth_challenges WHERE expires_at < ?", (now,)).rowcount,
                "codes": conn.execute("DELETE FROM auth_codes WHERE expires_at < ?", (now,)).rowcount,
            }
            for table in ("access_tokens", "refresh_tokens"):
                removed[table] = conn.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE (expires_at IS NOT NULL AND expires_at < ?)
                       OR (revoked_at IS NOT NULL AND revoked_at < ?)
                    """,
                    (now, now - revoked_grace_seconds),
                ).rowcount
            removed["clients"] = conn.execute(
                """
                DELETE FROM clients
                WHERE approved_at IS NULL AND created_at < ?
                  AND client_id NOT IN (SELECT client_id FROM auth_challenges)
                  AND client_id NOT IN (SELECT client_id FROM auth_codes)
                """,
                (now - unapproved_client_seconds,),
            ).rowcount
        return removed

    def create_challenge(self, record: ChallengeRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_challenges (
                    challenge, client_id, redirect_uri, scope, state,
                    code_challenge, resource, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.challenge,
                    record.client_id,
                    record.redirect_uri,
                    record.scope,
                    record.state,
                    record.code_challenge,
                    record.resource,
                    record.expires_at,
                ),
            )

    def get_challenge(self, challenge: str) -> ChallengeRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM auth_challenges WHERE challenge = ?", (challenge,)).fetchone()
        if not row:
            return None
        return ChallengeRecord(
            challenge=row["challenge"],
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            scope=row["scope"],
            state=row["state"],
            code_challenge=row["code_challenge"],
            resource=row["resource"],
            expires_at=row["expires_at"],
        )

    def consume_challenge(self, challenge: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM auth_challenges WHERE challenge = ?", (challenge,))

    def create_code(self, record: CodeRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_codes (
                    code_hash, client_id, redirect_uri, scopes, code_challenge,
                    resource, expires_at, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.code_hash,
                    record.client_id,
                    record.redirect_uri,
                    json.dumps(record.scopes),
                    record.code_challenge,
                    record.resource,
                    record.expires_at,
                    record.used_at,
                ),
            )

    def get_code(self, code_hash: str) -> CodeRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM auth_codes WHERE code_hash = ?", (code_hash,)).fetchone()
        if not row:
            return None
        return CodeRecord(
            code_hash=row["code_hash"],
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            scopes=json.loads(row["scopes"]),
            code_challenge=row["code_challenge"],
            resource=row["resource"],
            expires_at=row["expires_at"],
            used_at=row["used_at"],
        )

    def mark_code_used(self, code_hash: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE auth_codes SET used_at = ? WHERE code_hash = ?",
                (int(time.time()), code_hash),
            )

    def save_access_token(self, record: TokenRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO access_tokens (
                    token_hash, client_id, scopes, resource, expires_at,
                    created_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.token_hash,
                    record.client_id,
                    json.dumps(record.scopes),
                    record.resource,
                    record.expires_at,
                    int(time.time()),
                    record.revoked_at,
                ),
            )

    def save_refresh_token(self, record: TokenRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO refresh_tokens (
                    token_hash, client_id, scopes, resource, expires_at,
                    created_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.token_hash,
                    record.client_id,
                    json.dumps(record.scopes),
                    record.resource,
                    record.expires_at,
                    int(time.time()),
                    record.revoked_at,
                ),
            )

    def get_access_token(self, token_hash: str) -> TokenRecord | None:
        return self._get_token("access_tokens", token_hash)

    def get_refresh_token(self, token_hash: str) -> TokenRecord | None:
        return self._get_token("refresh_tokens", token_hash)

    def revoke_refresh_token(self, token_hash: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE token_hash = ?",
                (int(time.time()), token_hash),
            )

    def _get_token(self, table: str, token_hash: str) -> TokenRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE token_hash = ?", (token_hash,)).fetchone()
        if not row:
            return None
        return TokenRecord(
            token_hash=row["token_hash"],
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            resource=row["resource"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    def save_probe(self, key: str, value: str, updated_by: str) -> dict[str, Any]:
        updated_at = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO probe (key, value, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (key, value, updated_by, updated_at),
            )
        return {"key": key, "value": value, "updated_by": updated_by, "updated_at": updated_at}

    def read_probe(self, key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM probe WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return {
            "key": row["key"],
            "value": row["value"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
        }

    def get_app_setting(self, key: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def delete_app_setting(self, key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))

    def set_app_setting(self, key: str, value: str) -> dict[str, Any]:
        updated_at = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, updated_at),
            )
        return {"key": key, "value": value, "updated_at": updated_at}

    def set_tool_output_mode_if_restart_not_pending(self, mode: str) -> bool:
        updated_at = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute(
                "SELECT 1 FROM app_settings WHERE key = ?",
                (TOOL_OUTPUT_RESTART_PENDING_SETTING,),
            ).fetchone()
            if pending:
                return False
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (TOOL_OUTPUT_MODE_SETTING, mode, updated_at),
            )
        return True

    def reserve_tool_output_restart(self, active_mode: str) -> tuple[str, str]:
        updated_at = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (TOOL_OUTPUT_RESTART_PENDING_SETTING,),
            ).fetchone()
            if pending:
                return "pending", str(pending["value"])
            configured = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (TOOL_OUTPUT_MODE_SETTING,),
            ).fetchone()
            configured_mode = str(configured["value"]) if configured else DEFAULT_TOOL_OUTPUT_MODE
            if configured_mode not in TOOL_OUTPUT_MODES:
                configured_mode = DEFAULT_TOOL_OUTPUT_MODE
            if configured_mode == active_mode:
                return "not_required", configured_mode
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (TOOL_OUTPUT_RESTART_PENDING_SETTING, configured_mode, updated_at),
            )
        return "reserved", configured_mode

    def clear_tool_output_restart_pending(self) -> None:
        self.delete_app_setting(TOOL_OUTPUT_RESTART_PENDING_SETTING)

    def create_output_probe_run(self, run: dict[str, Any]) -> dict[str, Any]:
        created_at = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO output_probe_runs (
                    run_id, harness_label, content_profile, target_chars,
                    payload_char_count, server_result_json_chars, block_count,
                    block_size, tool_output_mode, blocks_json, status, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    run["run_id"],
                    run["harness_label"],
                    run["content_profile"],
                    run["target_chars"],
                    run["payload_char_count"],
                    run["server_result_json_chars"],
                    run["block_count"],
                    run["block_size"],
                    run["tool_output_mode"],
                    json.dumps(run["blocks"], separators=(",", ":")),
                    run["created_by"],
                    created_at,
                ),
            )
            conn.execute(
                """
                DELETE FROM output_probe_runs
                WHERE run_id NOT IN (
                    SELECT run_id FROM output_probe_runs
                    ORDER BY created_at DESC, run_id DESC
                    LIMIT 500
                )
                """
            )
        saved = self.get_output_probe_run(str(run["run_id"]))
        if saved is None:
            raise RuntimeError("output probe run was not persisted")
        return saved

    def get_output_probe_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM output_probe_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _output_probe_row(row) if row else None

    def list_output_probe_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    run_id, harness_label, content_profile, target_chars,
                    payload_char_count, server_result_json_chars, block_count,
                    block_size, tool_output_mode, '[]' AS blocks_json, status, result_class,
                    '[]' AS observed_canaries_json, matched_checkpoints_json,
                    last_complete_block_index, NULL AS last_complete_canary,
                    noticed_truncation, notes, created_by, created_at, reported_at
                FROM output_probe_runs
                ORDER BY created_at DESC, run_id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [_output_probe_row(row) for row in rows]

    def report_output_probe_run(
        self,
        run_id: str,
        *,
        result_class: str,
        observed_canaries: list[str],
        matched_checkpoints: list[str],
        last_complete_block_index: int,
        last_complete_canary: str,
        noticed_truncation: bool,
        notes: str,
    ) -> dict[str, Any] | None:
        reported_at = int(time.time())
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE output_probe_runs
                SET status = 'reported', result_class = ?, observed_canaries_json = ?,
                    matched_checkpoints_json = ?, last_complete_block_index = ?,
                    last_complete_canary = ?, noticed_truncation = ?, notes = ?, reported_at = ?
                WHERE run_id = ? AND status = 'pending'
                """,
                (
                    result_class,
                    json.dumps(observed_canaries, separators=(",", ":")),
                    json.dumps(matched_checkpoints, separators=(",", ":")),
                    last_complete_block_index,
                    last_complete_canary,
                    int(noticed_truncation),
                    notes,
                    reported_at,
                    run_id,
                ),
            )
        if cursor.rowcount == 0:
            return None
        return self.get_output_probe_run(run_id)

    def list_session_groups(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        deleted_clause = "" if include_deleted else "WHERE deleted_at IS NULL"
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM session_groups
                {deleted_clause}
                ORDER BY sort_order ASC, lower(name) ASC, group_id ASC
                """
            ).fetchall()
        return [_session_group_payload(_session_group_from_row(row)) for row in rows]

    def get_session_group(self, group_id: str, include_deleted: bool = False) -> SessionGroupRecord | None:
        resolved_group_id = group_id.strip()
        if not resolved_group_id:
            return None
        deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM session_groups WHERE group_id = ? {deleted_clause}",
                (resolved_group_id,),
            ).fetchone()
        return _session_group_from_row(row) if row else None

    def create_session_group(
        self,
        name: str,
        color: str,
        icon_key: str,
        group_id: str = "",
    ) -> SessionGroupRecord:
        resolved_name = _validate_group_name(name)
        resolved_color = _validate_group_color(color)
        resolved_icon_key = _validate_group_icon_key(icon_key)
        resolved_group_id = _validate_group_id(group_id.strip() or _slugify(resolved_name))
        now = int(time.time())
        with self._lock, self._connect() as conn:
            self._ensure_group_name_available(conn, resolved_name)
            if conn.execute("SELECT 1 FROM session_groups WHERE group_id = ?", (resolved_group_id,)).fetchone():
                raise ValueError(f"session group already exists: {resolved_group_id}")
            max_sort = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 90) AS max_sort FROM session_groups"
            ).fetchone()["max_sort"]
            conn.execute(
                """
                INSERT INTO session_groups (
                    group_id, name, color, icon_key, sort_order, is_system,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    resolved_group_id,
                    resolved_name,
                    resolved_color,
                    resolved_icon_key,
                    int(max_sort) + 10,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM session_groups WHERE group_id = ?", (resolved_group_id,)).fetchone()
        return _session_group_from_row(row)

    def update_session_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
        icon_key: str | None = None,
        is_sensitive: bool | None = None,
    ) -> SessionGroupRecord:
        resolved_group_id = group_id.strip()
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = _validate_group_name(name)
        if color is not None:
            updates["color"] = _validate_group_color(color)
        if icon_key is not None:
            updates["icon_key"] = _validate_group_icon_key(icon_key)
        if is_sensitive is not None:
            if not isinstance(is_sensitive, bool):
                raise ValueError("is_sensitive must be a boolean")
            updates["is_sensitive"] = int(is_sensitive)
        if not updates:
            raise ValueError("No editable session group fields provided")

        now = int(time.time())
        with self._lock, self._connect() as conn:
            row = self._get_group_row_for_update(conn, resolved_group_id)
            if row["is_system"]:
                raise ValueError("System session groups cannot be edited")
            if updates.get("name") and updates["name"].casefold() != row["name"].casefold():
                self._ensure_group_name_available(conn, updates["name"], excluding_group_id=resolved_group_id)
            assignments = [f"{column} = ?" for column in updates]
            values = list(updates.values())
            assignments.append("updated_at = ?")
            values.append(now)
            values.append(resolved_group_id)
            conn.execute(
                f"UPDATE session_groups SET {', '.join(assignments)} WHERE group_id = ?",
                values,
            )
            updated = conn.execute("SELECT * FROM session_groups WHERE group_id = ?", (resolved_group_id,)).fetchone()
        return _session_group_from_row(updated)

    def delete_session_group(
        self,
        group_id: str,
        destination_group_id: str = UNCATEGORIZED_GROUP_ID,
    ) -> SessionGroupRecord:
        resolved_group_id = group_id.strip()
        resolved_destination = destination_group_id.strip() or UNCATEGORIZED_GROUP_ID
        if resolved_group_id == resolved_destination:
            raise ValueError("destination_group_id must be different from group_id")
        now = int(time.time())
        with self._lock, self._connect() as conn:
            row = self._get_group_row_for_update(conn, resolved_group_id)
            if row["is_system"]:
                raise ValueError("System session groups cannot be deleted")
            destination = conn.execute(
                """
                SELECT * FROM session_groups
                WHERE group_id = ? AND deleted_at IS NULL
                """,
                (resolved_destination,),
            ).fetchone()
            if destination is None:
                raise ValueError(f"Unknown destination_group_id: {resolved_destination}")
            conn.execute(
                "UPDATE sessions SET group_id = ?, updated_at = ? WHERE group_id = ?",
                (resolved_destination, now, resolved_group_id),
            )
            conn.execute(
                "UPDATE session_files SET group_id = ? WHERE scope_type = 'group' AND group_id = ?",
                (resolved_destination, resolved_group_id),
            )
            # The group's labels go with it; the files and sessions move on unlabeled.
            conn.execute("DELETE FROM file_labels WHERE group_id = ?", (resolved_group_id,))
            conn.execute(
                """
                UPDATE session_groups
                SET deleted_at = ?, updated_at = ?
                WHERE group_id = ?
                """,
                (now, now, resolved_group_id),
            )
            deleted = conn.execute("SELECT * FROM session_groups WHERE group_id = ?", (resolved_group_id,)).fetchone()
        return _session_group_from_row(deleted)

    def create_session(
        self,
        session_id: str,
        title: str,
        title_is_auto: bool = False,
        group_id: str = UNCATEGORIZED_GROUP_ID,
    ) -> SessionRecord:
        now = int(time.time())
        resolved_group_id = group_id.strip() or UNCATEGORIZED_GROUP_ID
        with self._lock, self._connect() as conn:
            self._require_active_group(conn, resolved_group_id)
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, title, group_id, context_pack_id, context_pack_version,
                    title_is_auto, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    session_id,
                    title,
                    resolved_group_id,
                    RETIRED_CONTEXT_PACK_ID,
                    int(title_is_auto),
                    now,
                    now,
                ),
            )
        return SessionRecord(
            session_id=session_id,
            title=title,
            group_id=resolved_group_id,
            title_is_auto=title_is_auto,
            created_at=now,
            updated_at=now,
        )

    def save_session_file(
        self,
        session_id: str,
        filename: str,
        content: str,
        *,
        mime_type: str = "text/markdown",
        created_by: str = "model",
    ) -> SessionFileRecord:
        resolved_session_id = session_id.strip()
        resolved_filename = _validate_file_name(filename)
        resolved_content, size_bytes, digest = _validate_file_content(content)
        resolved_mime_type = _validate_mime_type(mime_type)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            if conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (resolved_session_id,)).fetchone() is None:
                raise ValueError(f"Unknown session_id: {resolved_session_id}")
            cursor = conn.execute(
                """
                INSERT INTO session_files (
                    scope_type, session_id, group_id, filename, mime_type, content,
                    sha256, size_bytes, created_by, created_at
                ) VALUES ('session', ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_session_id,
                    resolved_filename,
                    resolved_mime_type,
                    resolved_content,
                    digest,
                    size_bytes,
                    created_by.strip() or "model",
                    now,
                ),
            )
            row = conn.execute(
                f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return _session_file_from_row(row)

    def save_group_file(
        self,
        group_id: str,
        filename: str,
        content: str,
        *,
        mime_type: str = "text/markdown",
        created_by: str = "model",
    ) -> SessionFileRecord:
        resolved_group_id = group_id.strip() or UNCATEGORIZED_GROUP_ID
        resolved_filename = _validate_file_name(filename)
        resolved_content, size_bytes, digest = _validate_file_content(content)
        resolved_mime_type = _validate_mime_type(mime_type)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            self._require_active_group(conn, resolved_group_id)
            cursor = conn.execute(
                """
                INSERT INTO session_files (
                    scope_type, session_id, group_id, filename, mime_type, content,
                    sha256, size_bytes, created_by, created_at
                ) VALUES ('group', NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_group_id,
                    resolved_filename,
                    resolved_mime_type,
                    resolved_content,
                    digest,
                    size_bytes,
                    created_by.strip() or "model",
                    now,
                ),
            )
            row = conn.execute(
                f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return _session_file_from_row(row)

    def save_group_file_for_session(
        self,
        session_id: str,
        filename: str,
        content: str,
        *,
        mime_type: str = "text/markdown",
        created_by: str = "model",
    ) -> SessionFileRecord:
        resolved_session_id = session_id.strip()
        resolved_filename = _validate_file_name(filename)
        resolved_content, size_bytes, digest = _validate_file_content(content)
        resolved_mime_type = _validate_mime_type(mime_type)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute(
                "SELECT group_id FROM sessions WHERE session_id = ?",
                (resolved_session_id,),
            ).fetchone()
            if session is None:
                raise ValueError(f"Unknown session_id: {resolved_session_id}")
            group_id = session["group_id"]
            self._require_active_group(conn, group_id)
            cursor = conn.execute(
                """
                INSERT INTO session_files (
                    scope_type, session_id, group_id, filename, mime_type, content,
                    sha256, size_bytes, created_by, created_at
                ) VALUES ('group', NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    resolved_filename,
                    resolved_mime_type,
                    resolved_content,
                    digest,
                    size_bytes,
                    created_by.strip() or "model",
                    now,
                ),
            )
            row = conn.execute(
                f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return _session_file_from_row(row)

    def save_image(
        self, filename: str, image: ValidatedImage, *, session_id: str | None = None,
        group_id: str | None = None, for_session_group: bool = False,
        created_by: str = "model",
    ) -> SessionFileRecord:
        filename = _validate_file_name(filename)
        if not isinstance(image, ValidatedImage):
            raise ValueError("Image must be validated before storage")
        if (session_id is None) == (group_id is None) or (for_session_group and session_id is None):
            raise ValueError("Choose exactly one image scope")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if session_id is not None:
                session_id = session_id.strip()
                session = conn.execute("SELECT group_id FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
                if session is None:
                    raise ValueError(f"Unknown session_id: {session_id}")
                if for_session_group:
                    group_id, session_id = session["group_id"], None
            if group_id is not None:
                group_id = group_id.strip() or UNCATEGORIZED_GROUP_ID
                self._require_active_group(conn, group_id)
            current = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM session_files WHERE content_kind = 'image'").fetchone()[0]
            if current + len(image.data) > self.image_storage_max_bytes:
                raise ImageStorageQuotaError(f"Image storage quota exceeded ({self.image_storage_max_bytes} bytes)")
            cursor = conn.execute(
                """INSERT INTO session_files
                (scope_type, session_id, group_id, filename, mime_type, content,
                 binary_content, sha256, size_bytes, created_by, created_at, content_kind)
                VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, 'image')""",
                ("group" if group_id is not None else "session", session_id, group_id,
                 filename, image.mime_type, image.data, hashlib.sha256(image.data).hexdigest(),
                 len(image.data), created_by.strip() or "model", int(time.time())),
            )
            row = conn.execute(f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?", (cursor.lastrowid,)).fetchone()
        return _session_file_from_row(row)

    def get_image_for_session(self, session_id: str, file_id: int) -> tuple[SessionFileRecord, bytes] | None:
        session_id = session_id.strip()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN")
            session = conn.execute("SELECT group_id FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if session is None:
                raise ValueError(f"Unknown session_id: {session_id}")
            row = conn.execute(
                f"SELECT {SESSION_FILE_RECORD_COLUMNS}, binary_content FROM session_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if row is None:
                return None
            _require_file_visible_to_session(conn, row, session_id=session_id, group_id=session["group_id"])
            if row["content_kind"] != "image" or row["binary_content"] is None:
                raise ValueError("File is not an image")
            return _session_file_from_row(row), row["binary_content"]

    def save_session_pdf(
        self,
        session_id: str,
        filename: str,
        binary_content: bytes,
        *,
        extracted_text: str,
        page_count: int,
        extraction_status: str,
        extracted_text_bytes: int,
        created_by: str = "model",
    ) -> SessionFileRecord:
        resolved_session_id = session_id.strip()
        values = _validate_pdf_file(
            filename,
            binary_content,
            extracted_text=extracted_text,
            page_count=page_count,
            extraction_status=extraction_status,
            extracted_text_bytes=extracted_text_bytes,
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?",
                (resolved_session_id,),
            ).fetchone() is None:
                raise ValueError(f"Unknown session_id: {resolved_session_id}")
            return self._insert_pdf_file(
                conn,
                scope_type="session",
                session_id=resolved_session_id,
                group_id=None,
                values=values,
                created_by=created_by,
            )

    def save_group_pdf(
        self,
        group_id: str,
        filename: str,
        binary_content: bytes,
        *,
        extracted_text: str,
        page_count: int,
        extraction_status: str,
        extracted_text_bytes: int,
        created_by: str = "model",
    ) -> SessionFileRecord:
        resolved_group_id = group_id.strip() or UNCATEGORIZED_GROUP_ID
        values = _validate_pdf_file(
            filename,
            binary_content,
            extracted_text=extracted_text,
            page_count=page_count,
            extraction_status=extraction_status,
            extracted_text_bytes=extracted_text_bytes,
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_active_group(conn, resolved_group_id)
            return self._insert_pdf_file(
                conn,
                scope_type="group",
                session_id=None,
                group_id=resolved_group_id,
                values=values,
                created_by=created_by,
            )

    def save_group_pdf_for_session(
        self,
        session_id: str,
        filename: str,
        binary_content: bytes,
        *,
        extracted_text: str,
        page_count: int,
        extraction_status: str,
        extracted_text_bytes: int,
        created_by: str = "model",
    ) -> SessionFileRecord:
        resolved_session_id = session_id.strip()
        values = _validate_pdf_file(
            filename,
            binary_content,
            extracted_text=extracted_text,
            page_count=page_count,
            extraction_status=extraction_status,
            extracted_text_bytes=extracted_text_bytes,
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute(
                "SELECT group_id FROM sessions WHERE session_id = ?",
                (resolved_session_id,),
            ).fetchone()
            if session is None:
                raise ValueError(f"Unknown session_id: {resolved_session_id}")
            group_id = session["group_id"]
            self._require_active_group(conn, group_id)
            return self._insert_pdf_file(
                conn,
                scope_type="group",
                session_id=None,
                group_id=group_id,
                values=values,
                created_by=created_by,
            )

    def _insert_pdf_file(
        self,
        conn: sqlite3.Connection,
        *,
        scope_type: str,
        session_id: str | None,
        group_id: str | None,
        values: dict[str, Any],
        created_by: str,
    ) -> SessionFileRecord:
        current_bytes = conn.execute(
            """
            SELECT COALESCE(SUM(size_bytes + extracted_text_bytes), 0)
            FROM session_files
            WHERE content_kind = 'pdf'
            """
        ).fetchone()[0]
        incoming_bytes = values["size_bytes"] + values["extracted_text_bytes"]
        if current_bytes + incoming_bytes > self.pdf_storage_max_bytes:
            raise PdfStorageQuotaError(
                f"PDF storage quota exceeded ({self.pdf_storage_max_bytes} bytes)"
            )
        cursor = conn.execute(
            """
            INSERT INTO session_files (
                scope_type, session_id, group_id, filename, mime_type, content,
                binary_content, sha256, size_bytes, created_by, created_at,
                content_kind, page_count, extraction_status, extracted_text_bytes
            ) VALUES (?, ?, ?, ?, 'application/pdf', ?, ?, ?, ?, ?, ?,
                      'pdf', ?, ?, ?)
            """,
            (
                scope_type,
                session_id,
                group_id,
                values["filename"],
                values["extracted_text"],
                values["binary_content"],
                values["sha256"],
                values["size_bytes"],
                created_by.strip() or "model",
                int(time.time()),
                values["page_count"],
                values["extraction_status"],
                values["extracted_text_bytes"],
            ),
        )
        row = conn.execute(
            f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return _session_file_from_row(row)

    def list_session_files(
        self,
        *,
        session_id: str | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if session_id:
            clauses.append("(scope_type = 'session' AND session_id = ?)")
            values.append(session_id.strip())
        if group_id:
            clauses.append("(scope_type = 'group' AND group_id = ?)")
            values.append(group_id.strip())
        where = f"WHERE {' OR '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""SELECT {SESSION_FILE_MANIFEST_COLUMNS}
                FROM session_files
                {where}
                ORDER BY created_at DESC, file_id DESC
                """,
                values,
            ).fetchall()
        return [_session_file_manifest_from_row(row) for row in rows]

    def list_session_files_for_session(self, session_id: str) -> list[dict[str, Any]]:
        resolved_session_id = session_id.strip()
        with self._lock, self._connect() as conn:
            session = conn.execute(
                "SELECT group_id FROM sessions WHERE session_id = ?",
                (resolved_session_id,),
            ).fetchone()
            if session is None:
                raise ValueError(f"Unknown session_id: {resolved_session_id}")
            rows = conn.execute(
                f"""SELECT {SESSION_FILE_MANIFEST_COLUMNS}
                FROM session_files
                WHERE (scope_type = 'session' AND session_id = ?)
                   OR (scope_type = 'group' AND group_id = ?)
                ORDER BY created_at DESC, file_id DESC
                """,
                (resolved_session_id, session["group_id"]),
            ).fetchall()
        return [_session_file_manifest_from_row(row) for row in rows]

    def get_session_file(self, file_id: int) -> SessionFileRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
        return _session_file_from_row(row) if row else None

    def get_session_file_for_session(
        self,
        session_id: str,
        file_id: int,
    ) -> SessionFileRecord | None:
        resolved_session_id = session_id.strip()
        with self._lock, self._connect() as conn:
            session = conn.execute(
                "SELECT group_id FROM sessions WHERE session_id = ?",
                (resolved_session_id,),
            ).fetchone()
            if session is None:
                raise ValueError(f"Unknown session_id: {resolved_session_id}")
            row = conn.execute(
                f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if row is None:
                return None
            _require_file_visible_to_session(
                conn,
                row,
                session_id=resolved_session_id,
                group_id=session["group_id"],
            )
        return _session_file_from_row(row)

    def get_session_file_binary(self, file_id: int) -> tuple[str, str, str, bytes | None] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT filename, content_kind, mime_type, binary_content
                FROM session_files
                WHERE file_id = ?
                """,
                (file_id,),
            ).fetchone()
        if row is None:
            return None
        return row["filename"], row["content_kind"], row["mime_type"], row["binary_content"]

    def update_session_file(
        self,
        file_id: int,
        content: str,
        *,
        expected_sha256: str,
        visible_session_id: str | None = None,
        visible_group_id: str | None = None,
    ) -> SessionFileRecord:
        resolved_content, size_bytes, digest = _validate_file_content(content)
        resolved_expected_sha256 = expected_sha256.strip().lower()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown file_id: {file_id}")
            _require_file_visible_to_session(
                conn,
                row,
                session_id=visible_session_id,
                group_id=visible_group_id,
            )
            if row["sha256"] != resolved_expected_sha256:
                raise SessionFileConflictError(
                    f"File {file_id} changed since it was opened"
                )
            if row["content_kind"] != "text":
                raise ValueError("PDF and image files cannot be edited")
            conn.execute(
                """
                UPDATE session_files
                SET content = ?, sha256 = ?, size_bytes = ?
                WHERE file_id = ?
                """,
                (resolved_content, digest, size_bytes, file_id),
            )
            updated = conn.execute(
                f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
        return _session_file_from_row(updated)

    def move_session_file(
        self,
        file_id: int,
        *,
        scope_type: str,
        session_id: str | None = None,
        group_id: str | None = None,
        visible_session_id: str | None = None,
        visible_group_id: str | None = None,
    ) -> SessionFileRecord:
        resolved_scope_type = scope_type.strip().lower()
        resolved_session_id = session_id.strip() if session_id else ""
        resolved_group_id = group_id.strip() if group_id else ""
        if resolved_scope_type not in {"session", "group"}:
            raise ValueError("scope_type must be session or group")
        if resolved_scope_type == "session":
            if not resolved_session_id:
                raise ValueError("session_id is required for session scope")
            if resolved_group_id:
                raise ValueError("group_id is not allowed for session scope")
        else:
            if not resolved_group_id:
                raise ValueError("group_id is required for group scope")
            if resolved_session_id:
                raise ValueError("session_id is not allowed for group scope")

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown file_id: {file_id}")
            _require_file_visible_to_session(
                conn,
                row,
                session_id=visible_session_id,
                group_id=visible_group_id,
            )

            if resolved_scope_type == "session":
                if conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?",
                    (resolved_session_id,),
                ).fetchone() is None:
                    raise ValueError(f"Unknown session_id: {resolved_session_id}")
                target_session_id = resolved_session_id
                target_group_id = None
            else:
                self._require_active_group(conn, resolved_group_id)
                target_session_id = None
                target_group_id = resolved_group_id

            if (
                row["scope_type"] == resolved_scope_type
                and row["session_id"] == target_session_id
                and row["group_id"] == target_group_id
            ):
                return _session_file_from_row(row)

            conn.execute(
                """
                UPDATE session_files
                SET scope_type = ?, session_id = ?, group_id = ?
                WHERE file_id = ?
                """,
                (resolved_scope_type, target_session_id, target_group_id, file_id),
            )
            # Labels are a personal sorting aid; a moved file starts clean.
            conn.execute("DELETE FROM session_file_labels WHERE file_id = ?", (file_id,))
            updated = conn.execute(
                f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
        return _session_file_from_row(updated)

    def delete_session_file(
        self,
        file_id: int,
        *,
        visible_session_id: str | None = None,
        visible_group_id: str | None = None,
    ) -> SessionFileRecord:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {SESSION_FILE_RECORD_COLUMNS} FROM session_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown file_id: {file_id}")
            _require_file_visible_to_session(
                conn,
                row,
                session_id=visible_session_id,
                group_id=visible_group_id,
            )
            conn.execute("DELETE FROM session_files WHERE file_id = ?", (file_id,))
        return _session_file_from_row(row)

    def list_admin_session_files(self, *, session_id: str, group_id: str) -> list[dict[str, Any]]:
        """File manifests for the admin browser: a short text preview and label ids.

        Models never receive this shape; MCP tools keep using list_session_files.
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""SELECT {SESSION_FILE_MANIFEST_COLUMNS},
                    CASE WHEN content_kind = 'text' THEN substr(content, 1, ?) END AS preview,
                    (SELECT client_name FROM clients WHERE clients.client_id = session_files.created_by)
                        AS created_by_name
                FROM session_files
                WHERE (scope_type = 'session' AND session_id = ?)
                   OR (scope_type = 'group' AND group_id = ?)
                ORDER BY created_at DESC, file_id DESC
                """,
                (FILE_PREVIEW_CHARS, session_id, group_id),
            ).fetchall()
            label_ids = self._label_ids_for_files(conn, [row["file_id"] for row in rows], group_id)
        manifests = []
        for row in rows:
            manifest = _session_file_manifest_from_row(row)
            manifest["preview"] = row["preview"]
            manifest["created_by_name"] = row["created_by_name"]
            manifest["label_ids"] = label_ids.get(row["file_id"], [])
            manifests.append(manifest)
        return manifests

    def list_file_labels(self, group_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT label_id, group_id, name, color, created_at, updated_at
                FROM file_labels
                WHERE group_id = ?
                ORDER BY name COLLATE NOCASE, label_id
                """,
                (group_id.strip(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_file_label(self, group_id: str, name: str, color: str) -> dict[str, Any]:
        resolved_name = _validate_file_label_name(name)
        resolved_color = _validate_file_label_color(color)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_active_group(conn, group_id)
            count = conn.execute(
                "SELECT COUNT(*) FROM file_labels WHERE group_id = ?", (group_id,)
            ).fetchone()[0]
            if count >= MAX_FILE_LABELS_PER_GROUP:
                raise ValueError(f"A group can have at most {MAX_FILE_LABELS_PER_GROUP} labels")
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO file_labels(group_id, name, color, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (group_id, resolved_name, resolved_color, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"A label named {resolved_name!r} already exists") from exc
            row = conn.execute(
                "SELECT label_id, group_id, name, color, created_at, updated_at FROM file_labels WHERE label_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return dict(row)

    def update_file_label(
        self,
        label_id: int,
        *,
        group_id: str,
        name: str | None = None,
        color: str | None = None,
    ) -> dict[str, Any]:
        if name is None and color is None:
            raise ValueError("Provide a new name or color")
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._require_group_label(conn, label_id, group_id)
            resolved_name = _validate_file_label_name(name) if name is not None else row["name"]
            resolved_color = _validate_file_label_color(color) if color is not None else row["color"]
            try:
                conn.execute(
                    "UPDATE file_labels SET name = ?, color = ?, updated_at = ? WHERE label_id = ?",
                    (resolved_name, resolved_color, now, label_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"A label named {resolved_name!r} already exists") from exc
            updated = conn.execute(
                "SELECT label_id, group_id, name, color, created_at, updated_at FROM file_labels WHERE label_id = ?",
                (label_id,),
            ).fetchone()
        return dict(updated)

    def delete_file_label(self, label_id: int, *, group_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._require_group_label(conn, label_id, group_id)
            # Assignments go with the label (ON DELETE CASCADE); files are untouched.
            conn.execute("DELETE FROM file_labels WHERE label_id = ?", (label_id,))
        return dict(row)

    def set_file_label_assignment(
        self,
        label_id: int,
        file_ids: list[int],
        *,
        assigned: bool,
        session_id: str,
    ) -> dict[int, list[int]]:
        """Add or remove one label on files visible to a session; returns each file's labels."""
        resolved_ids = sorted({int(file_id) for file_id in file_ids})
        if not resolved_ids:
            raise ValueError("Choose at least one file")
        if len(resolved_ids) > 500:
            raise ValueError("Label at most 500 files at once")
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute(
                "SELECT group_id FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise ValueError(f"Unknown session_id: {session_id}")
            group_id = session["group_id"]
            self._require_group_label(conn, label_id, group_id)
            for file_id in resolved_ids:
                row = conn.execute(
                    f"SELECT {SESSION_FILE_MANIFEST_COLUMNS} FROM session_files WHERE file_id = ?",
                    (file_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"Unknown file_id: {file_id}")
                _require_file_visible_to_session(conn, row, session_id=session_id, group_id=group_id)
                if assigned:
                    conn.execute(
                        "INSERT OR IGNORE INTO session_file_labels(file_id, label_id, created_at) VALUES (?, ?, ?)",
                        (file_id, label_id, now),
                    )
                else:
                    conn.execute(
                        "DELETE FROM session_file_labels WHERE file_id = ? AND label_id = ?",
                        (file_id, label_id),
                    )
            label_ids = self._label_ids_for_files(conn, resolved_ids, group_id)
        return {file_id: label_ids.get(file_id, []) for file_id in resolved_ids}

    @staticmethod
    def _require_group_label(conn: sqlite3.Connection, label_id: int, group_id: str) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT label_id, group_id, name, color, created_at, updated_at
            FROM file_labels
            WHERE label_id = ? AND group_id = ?
            """,
            (label_id, group_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown label_id: {label_id}")
        return row

    @staticmethod
    def _label_ids_for_files(
        conn: sqlite3.Connection,
        file_ids: list[int],
        group_id: str,
    ) -> dict[int, list[int]]:
        if not file_ids:
            return {}
        placeholders = ", ".join("?" for _ in file_ids)
        rows = conn.execute(
            f"""
            SELECT assignment.file_id, assignment.label_id
            FROM session_file_labels AS assignment
            JOIN file_labels AS label ON label.label_id = assignment.label_id
            WHERE assignment.file_id IN ({placeholders}) AND label.group_id = ?
            ORDER BY label.name COLLATE NOCASE, label.label_id
            """,
            (*file_ids, group_id),
        ).fetchall()
        result: dict[int, list[int]] = {}
        for row in rows:
            result.setdefault(row["file_id"], []).append(row["label_id"])
        return result

    # ---------- contexts ----------

    def create_context(self, name: str = "", color: str = "#7a7df0") -> dict[str, Any]:
        resolved_color = _validate_context_color(color)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count, last_order = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(sort_order), -1) FROM contexts"
            ).fetchone()
            if count >= MAX_CONTEXTS:
                raise ValueError(f"Bridge can hold at most {MAX_CONTEXTS} contexts")
            resolved_name = _validate_context_name(name or f"Context {count + 1}")
            context_id = _new_context_id()
            while conn.execute("SELECT 1 FROM contexts WHERE context_id = ?", (context_id,)).fetchone():
                context_id = _new_context_id()
            conn.execute(
                """
                INSERT INTO contexts(context_id, name, color, is_locked, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?, ?)
                """,
                (context_id, resolved_name, resolved_color, last_order + 1, now, now),
            )
            return self._context_payload(conn, context_id)

    def list_contexts(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            ids = [
                row[0]
                for row in conn.execute("SELECT context_id FROM contexts ORDER BY sort_order, created_at")
            ]
            return [self._context_payload(conn, context_id) for context_id in ids]

    def get_context(self, context_id: str, *, include_content: bool = False) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            if not conn.execute("SELECT 1 FROM contexts WHERE context_id = ?", (context_id,)).fetchone():
                return None
            return self._context_payload(conn, context_id, include_content=include_content)

    def get_context_block(self, block_id: int, *, include_content: bool = False) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            self._require_context_block(conn, block_id)
            return self._context_block_payload(conn, block_id, include_content=include_content)

    def get_context_block_context(self, block_id: int) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT context_id FROM context_blocks WHERE block_id = ?", (block_id,)).fetchone()
        return row[0] if row else None

    def list_context_ids(self) -> list[str]:
        with self._lock, self._connect() as conn:
            return [row[0] for row in conn.execute("SELECT context_id FROM contexts")]

    def list_session_ids(self) -> list[str]:
        with self._lock, self._connect() as conn:
            return [row[0] for row in conn.execute("SELECT session_id FROM sessions")]

    def list_context_library_sessions(self) -> list[dict[str, Any]]:
        """What a session card needs beyond list_sessions: who spoke and roughly how much."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id,
                       GROUP_CONCAT(DISTINCT model_name) AS models,
                       COALESCE(SUM(LENGTH(user_message) + LENGTH(assistant_response)), 0) AS chars
                FROM exchanges
                WHERE deleted_at IS NULL
                GROUP BY session_id
                """
            ).fetchall()
        stats = {
            row["session_id"]: {"models": sorted((row["models"] or "").split(",")) if row["models"] else [], "chars": row["chars"]}
            for row in rows
        }
        sessions = self.list_sessions()
        for session in sessions:
            extra = stats.get(session["session_id"], {"models": [], "chars": 0})
            session["models"] = extra["models"]
            session["token_estimate"] = _estimate_tokens(extra["chars"])
        return sessions

    def list_context_library_files(self, *, preview_chars: int = 600) -> list[dict[str, Any]]:
        """Every file in Bridge with its effective group and the start of its text."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join("f." + column.strip() for column in SESSION_FILE_MANIFEST_COLUMNS.split(","))},
                       COALESCE(f.group_id, s.group_id) AS effective_group_id,
                       CASE WHEN f.content_kind = 'image' THEN '' ELSE SUBSTR(f.content, 1, ?) END AS preview,
                       CASE WHEN f.content_kind = 'image' THEN 0 ELSE LENGTH(f.content) END AS chars
                FROM session_files f
                LEFT JOIN sessions s ON s.session_id = f.session_id
                ORDER BY f.created_at DESC, f.file_id DESC
                """,
                (preview_chars,),
            ).fetchall()
        files = []
        for row in rows:
            payload = _session_file_manifest_from_row(row)
            payload["effective_group_id"] = row["effective_group_id"]
            payload["preview"] = row["preview"]
            payload["token_estimate"] = _estimate_tokens(row["chars"])
            files.append(payload)
        return files

    def update_context(
        self,
        context_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
        is_locked: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._require_context(conn, context_id)
            locked = bool(row["is_locked"]) if is_locked is None else bool(is_locked)
            if locked and (name is not None or color is not None):
                raise ContextLockedError("Unlock the context to rename it or change its color")
            conn.execute(
                "UPDATE contexts SET name = ?, color = ?, is_locked = ?, updated_at = ? WHERE context_id = ?",
                (
                    _validate_context_name(name) if name is not None else row["name"],
                    _validate_context_color(color) if color is not None else row["color"],
                    int(locked),
                    int(time.time()),
                    context_id,
                ),
            )
            return self._context_payload(conn, context_id)

    def reorder_contexts(self, context_ids: list[str]) -> list[dict[str, Any]]:
        """Set the column order. Locks do not apply: the order is not part of a context."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = {row[0] for row in conn.execute("SELECT context_id FROM contexts")}
            if set(context_ids) != existing or len(context_ids) != len(existing):
                raise ValueError("Send every context ID exactly once")
            for order, context_id in enumerate(context_ids):
                conn.execute("UPDATE contexts SET sort_order = ? WHERE context_id = ?", (order, context_id))
        return self.list_contexts()

    def delete_context(self, context_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._require_context(conn, context_id)
            if row["is_locked"]:
                raise ContextLockedError("Unlock the context before deleting it")
            payload = self._context_payload(conn, context_id)
            conn.execute("DELETE FROM contexts WHERE context_id = ?", (context_id,))
        return payload

    def add_context_block(
        self,
        context_id: str,
        snapshot: ContextBlockSnapshot,
        *,
        position: int | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_unlocked_context(conn, context_id)
            block_id = self._insert_context_block(conn, context_id, snapshot, position, snapshot_at=now)
            self._touch_context(conn, context_id, now)
            return self._context_block_payload(conn, block_id)

    def refresh_context_block(self, block_id: int, snapshot: ContextBlockSnapshot) -> dict[str, Any]:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            block = self._require_context_block(conn, block_id)
            self._require_unlocked_context(conn, block["context_id"])
            if snapshot.source_key != block["source_key"]:
                raise ValueError("A refresh must come from the same source")
            conn.execute(
                """
                UPDATE context_blocks
                SET title = ?, content = ?, source_hash = ?, found_ids = ?, token_count = ?, snapshot_at = ?,
                    source_meta = ?
                WHERE block_id = ?
                """,
                (
                    snapshot.title,
                    snapshot.content,
                    snapshot.source_hash,
                    json.dumps(snapshot.found_ids, sort_keys=True),
                    snapshot.token_count,
                    now,
                    json.dumps(snapshot.meta, sort_keys=True),
                    block_id,
                ),
            )
            self._touch_context(conn, block["context_id"], now)
            return self._context_block_payload(conn, block_id)

    def move_context_block(
        self,
        block_id: int,
        *,
        context_id: str,
        position: int | None = None,
        copy: bool = False,
    ) -> dict[str, Any]:
        """Reorder a block, move it to another context, or copy it there."""
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            block = self._require_context_block(conn, block_id)
            source_context_id = block["context_id"]
            self._require_unlocked_context(conn, context_id)
            if source_context_id == context_id:
                if copy:
                    raise ContextBlockConflictError("This source is already in the context")
                self._place_context_block(conn, block_id, context_id, position)
                self._touch_context(conn, context_id, now)
                return self._context_block_payload(conn, block_id)
            if conn.execute(
                "SELECT 1 FROM context_blocks WHERE context_id = ? AND source_key = ?",
                (context_id, block["source_key"]),
            ).fetchone():
                raise ContextBlockConflictError("This source is already in the target context")
            if copy:
                snapshot = _context_snapshot_from_row(
                    conn.execute("SELECT * FROM context_blocks WHERE block_id = ?", (block_id,)).fetchone()
                )
                new_id = self._insert_context_block(
                    conn, context_id, snapshot, position, snapshot_at=block["snapshot_at"]
                )
                self._touch_context(conn, context_id, now)
                return self._context_block_payload(conn, new_id)
            self._require_unlocked_context(conn, source_context_id)
            conn.execute(
                "UPDATE context_blocks SET context_id = ?, position = ? WHERE block_id = ?",
                (context_id, _LAST_POSITION, block_id),
            )
            self._compact_context_positions(conn, source_context_id)
            self._place_context_block(conn, block_id, context_id, position)
            self._touch_context(conn, source_context_id, now)
            self._touch_context(conn, context_id, now)
            return self._context_block_payload(conn, block_id)

    def delete_context_block(self, block_id: int) -> dict[str, Any]:
        """Remove a block and return it whole, content included, so the admin can undo."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            block = self._require_context_block(conn, block_id)
            self._require_unlocked_context(conn, block["context_id"])
            payload = self._context_block_payload(conn, block_id, include_content=True)
            conn.execute("DELETE FROM context_blocks WHERE block_id = ?", (block_id,))
            self._compact_context_positions(conn, block["context_id"])
            self._touch_context(conn, block["context_id"], int(time.time()))
        return payload

    def _insert_context_block(
        self,
        conn: sqlite3.Connection,
        context_id: str,
        snapshot: ContextBlockSnapshot,
        position: int | None,
        *,
        snapshot_at: int,
    ) -> int:
        if snapshot.source_type not in CONTEXT_SOURCE_TYPES:
            raise ValueError("A context block comes from a session, a file or a topic")
        try:
            cursor = conn.execute(
                """
                INSERT INTO context_blocks (
                    context_id, position, source_type, source_key, source_session_id, source_file_id,
                    title, content, source_hash, found_ids, token_count, snapshot_at, created_at, source_meta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context_id,
                    _LAST_POSITION,
                    snapshot.source_type,
                    snapshot.source_key,
                    snapshot.source_session_id,
                    snapshot.source_file_id,
                    snapshot.title,
                    snapshot.content,
                    snapshot.source_hash,
                    json.dumps(snapshot.found_ids, sort_keys=True),
                    snapshot.token_count,
                    snapshot_at,
                    int(time.time()),
                    json.dumps(snapshot.meta, sort_keys=True),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ContextBlockConflictError("This source is already in the context") from exc
        block_id = int(cursor.lastrowid)
        self._place_context_block(conn, block_id, context_id, position)
        return block_id

    @staticmethod
    def _place_context_block(
        conn: sqlite3.Connection, block_id: int, context_id: str, position: int | None
    ) -> None:
        """Put one block at ``position`` (end when None) and renumber the rest 0..n-1."""
        others = [
            row[0]
            for row in conn.execute(
                "SELECT block_id FROM context_blocks WHERE context_id = ? AND block_id != ? ORDER BY position, block_id",
                (context_id, block_id),
            )
        ]
        index = len(others) if position is None else max(0, min(int(position), len(others)))
        others.insert(index, block_id)
        for order, item in enumerate(others):
            conn.execute("UPDATE context_blocks SET position = ? WHERE block_id = ?", (order, item))

    @staticmethod
    def _compact_context_positions(conn: sqlite3.Connection, context_id: str) -> None:
        rows = conn.execute(
            "SELECT block_id FROM context_blocks WHERE context_id = ? ORDER BY position, block_id",
            (context_id,),
        ).fetchall()
        for order, row in enumerate(rows):
            conn.execute("UPDATE context_blocks SET position = ? WHERE block_id = ?", (order, row[0]))

    @staticmethod
    def _touch_context(conn: sqlite3.Connection, context_id: str, now: int) -> None:
        conn.execute("UPDATE contexts SET updated_at = ? WHERE context_id = ?", (now, context_id))

    @staticmethod
    def _require_context(conn: sqlite3.Connection, context_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM contexts WHERE context_id = ?", (context_id,)).fetchone()
        if row is None:
            raise ContextNotFoundError(f"Unknown context_id: {context_id}")
        return row

    def _require_unlocked_context(self, conn: sqlite3.Connection, context_id: str) -> sqlite3.Row:
        row = self._require_context(conn, context_id)
        if row["is_locked"]:
            raise ContextLockedError("This context is locked")
        return row

    @staticmethod
    def _require_context_block(conn: sqlite3.Connection, block_id: int) -> sqlite3.Row:
        row = conn.execute(
            f"SELECT {CONTEXT_BLOCK_COLUMNS} FROM context_blocks WHERE block_id = ?", (block_id,)
        ).fetchone()
        if row is None:
            raise ContextNotFoundError(f"Unknown context block: {block_id}")
        return row

    @staticmethod
    def _context_block_payload(
        conn: sqlite3.Connection, block_id: int, *, include_content: bool = False
    ) -> dict[str, Any]:
        columns = CONTEXT_BLOCK_COLUMNS + (", content" if include_content else "")
        row = conn.execute(f"SELECT {columns} FROM context_blocks WHERE block_id = ?", (block_id,)).fetchone()
        return _context_block_from_row(row)

    @staticmethod
    def _context_payload(
        conn: sqlite3.Connection, context_id: str, *, include_content: bool = False
    ) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM contexts WHERE context_id = ?", (context_id,)).fetchone()
        columns = CONTEXT_BLOCK_COLUMNS + (", content" if include_content else "")
        blocks = [
            _context_block_from_row(block)
            for block in conn.execute(
                f"SELECT {columns} FROM context_blocks WHERE context_id = ? ORDER BY position, block_id",
                (context_id,),
            )
        ]
        return {
            "context_id": row["context_id"],
            "name": row["name"],
            "color": row["color"],
            "is_locked": bool(row["is_locked"]),
            "sort_order": row["sort_order"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "token_count": sum(block["token_count"] for block in blocks),
            "blocks": blocks,
        }

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return None
        return _session_from_row(row)

    def set_session_title(self, session_id: str, title: str, source: str = "manual") -> SessionRecord:
        resolved_title = _validate_session_title(title)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown session_id: {session_id}")
            conn.execute(
                "UPDATE sessions SET title = ?, title_is_auto = 0, title_source = ?, updated_at = ? WHERE session_id = ?",
                (resolved_title, source, now, session_id),
            )
            updated = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return _session_from_row(updated)

    def set_auto_session_title(
        self, session_id: str, title: str, *, provider: str = "codex", estimated_tokens: int = 0
    ) -> bool:
        """Name a session only while its title is still the one cut from the first message."""
        resolved_title = _validate_session_title(title)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT title FROM sessions WHERE session_id = ? AND title_source = 'derived'", (session_id,)
            ).fetchone()
            if row is None:
                return False
            cursor = conn.execute(
                "UPDATE sessions SET title = ?, title_source = 'auto', updated_at = ? "
                "WHERE session_id = ? AND title_source = 'derived'",
                (resolved_title, now, session_id),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO session_title_events "
                "(session_id, old_title, new_title, provider, estimated_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, row["title"], resolved_title, provider, estimated_tokens, now),
            )
        return True

    def set_session_group(self, session_id: str, group_id: str) -> SessionRecord:
        resolved_group_id = group_id.strip() or UNCATEGORIZED_GROUP_ID
        now = int(time.time())
        with self._lock, self._connect() as conn:
            self._require_active_group(conn, resolved_group_id)
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown session_id: {session_id}")
            conn.execute(
                "UPDATE sessions SET group_id = ?, updated_at = ? WHERE session_id = ?",
                (resolved_group_id, now, session_id),
            )
            if row["group_id"] != resolved_group_id:
                # Labels belong to the old group, so the session's own files lose them.
                conn.execute(
                    """
                    DELETE FROM session_file_labels
                    WHERE file_id IN (
                        SELECT file_id FROM session_files
                        WHERE scope_type = 'session' AND session_id = ?
                    )
                    """,
                    (session_id,),
                )
            updated = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return _session_from_row(updated)

    def set_session_pinned(self, session_id: str, pinned: bool) -> SessionRecord:
        # Pinning is list housekeeping, not activity, so updated_at stays put.
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown session_id: {session_id}")
            if pinned and row["pinned_at"] is None:
                conn.execute(
                    "UPDATE sessions SET pinned_at = ? WHERE session_id = ?",
                    (int(time.time()), session_id),
                )
            elif not pinned:
                conn.execute("UPDATE sessions SET pinned_at = NULL WHERE session_id = ?", (session_id,))
            updated = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return _session_from_row(updated)

    # ---------- closing and summaries (app.session_summaries) ----------

    _ACTIVITY_SQL = """
        SELECT
            s.session_id,
            s.group_id,
            s.pinned_at,
            s.reopened_until,
            COALESCE(
                MAX(CASE WHEN e.deleted_at IS NULL THEN COALESCE(e.assistant_created_at, e.created_at) END),
                s.created_at
            ) AS last_turn_at,
            MAX(CASE WHEN e.deleted_at IS NULL THEN e.exchange_id END) AS last_exchange_id,
            SUM(CASE WHEN e.exchange_id IS NOT NULL AND e.deleted_at IS NULL THEN 1 ELSE 0 END) AS exchange_count
        FROM sessions s
        LEFT JOIN exchanges e ON e.session_id = s.session_id
    """

    @staticmethod
    def _activity_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "group_id": row["group_id"] or UNCATEGORIZED_GROUP_ID,
            "pinned": row["pinned_at"] is not None,
            "reopened_until": row["reopened_until"],
            "last_turn_at": row["last_turn_at"],
            "last_exchange_id": row["last_exchange_id"],
            "exchange_count": row["exchange_count"] or 0,
        }

    def list_session_activity(self) -> list[dict[str, Any]]:
        """What the closing rule needs for every session: last turn, pin, group and reopen time."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(self._ACTIVITY_SQL + " GROUP BY s.session_id").fetchall()
        return [self._activity_from_row(row) for row in rows]

    def get_session_activity(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                self._ACTIVITY_SQL + " WHERE s.session_id = ? GROUP BY s.session_id", (session_id,)
            ).fetchone()
        return self._activity_from_row(row) if row else None

    def set_session_reopened_until(self, session_id: str, until: int | None) -> None:
        # Reopening is housekeeping, not activity, so updated_at stays put.
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET reopened_until = ? WHERE session_id = ?", (until, session_id)
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown session_id: {session_id}")

    @staticmethod
    def _summary_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "status": row["status"],
            "content": row["content"],
            "upto_exchange_id": row["upto_exchange_id"],
            "exchange_count": row["exchange_count"],
            "model": row["model"],
            "prompt_hash": row["prompt_hash"],
            "error": row["error"],
            "estimated_tokens": row["estimated_tokens"],
            "written_at": row["written_at"],
            "updated_at": row["updated_at"],
        }

    def list_session_summaries(self) -> dict[str, dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM session_summaries").fetchall()
        return {row["session_id"]: self._summary_from_row(row) for row in rows}

    def get_session_summary(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM session_summaries WHERE session_id = ?", (session_id,)).fetchone()
        return self._summary_from_row(row) if row else None

    def _upsert_summary(self, session_id: str, **fields: Any) -> None:
        fields["updated_at"] = int(time.time())
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{column} = excluded.{column}" for column in fields)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO session_summaries (session_id, {columns}) VALUES (?, {marks}) "
                f"ON CONFLICT(session_id) DO UPDATE SET {updates}",
                (session_id, *fields.values()),
            )

    def queue_session_summary(self, session_id: str) -> None:
        """Waiting for Codex. A summary written earlier stays readable until the new one lands."""
        self._upsert_summary(session_id, status="queued", error=None)

    def start_session_summary(self, session_id: str, model: str) -> None:
        self._upsert_summary(session_id, status="running", error=None, model=model)

    def finish_session_summary(
        self,
        session_id: str,
        content: str,
        *,
        upto_exchange_id: int,
        exchange_count: int,
        model: str,
        prompt_hash: str,
        estimated_tokens: int,
    ) -> None:
        self._upsert_summary(
            session_id,
            status="done",
            content=content,
            upto_exchange_id=upto_exchange_id,
            exchange_count=exchange_count,
            model=model,
            prompt_hash=prompt_hash,
            estimated_tokens=estimated_tokens,
            error=None,
            written_at=int(time.time()),
        )

    def fail_session_summary(self, session_id: str, error: str) -> None:
        self._upsert_summary(session_id, status="failed", error=error[:500])

    def requeue_running_summaries(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE session_summaries SET status = 'queued', updated_at = ? WHERE status = 'running'",
                (int(time.time()),),
            )

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.session_id,
                    s.title,
                    s.group_id,
                    s.title_is_auto,
                    s.title_source,
                    s.created_at,
                    s.updated_at,
                    s.pinned_at,
                    g.name AS group_name,
                    g.color AS group_color,
                    g.icon_key AS group_icon_key,
                    g.sort_order AS group_sort_order,
                    g.is_system AS group_is_system,
                    g.is_sensitive AS group_is_sensitive,
                    COALESCE(
                        MAX(CASE
                            WHEN e.deleted_at IS NULL THEN COALESCE(e.assistant_created_at, e.created_at)
                            ELSE NULL
                        END),
                        s.created_at
                    ) AS last_turn_at,
                    SUM(CASE WHEN e.exchange_id IS NOT NULL AND e.deleted_at IS NULL THEN 1 ELSE 0 END) AS exchange_count,
                    SUM(CASE WHEN e.deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted_exchange_count
                FROM sessions s
                LEFT JOIN session_groups g ON g.group_id = s.group_id
                LEFT JOIN exchanges e ON e.session_id = s.session_id
                GROUP BY s.session_id
                ORDER BY last_turn_at DESC, s.created_at DESC
                """
            ).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "title": row["title"],
                "group_id": row["group_id"],
                "group": _group_payload_from_join(row),
                "title_is_auto": bool(row["title_is_auto"]),
                "title_source": row["title_source"],
                "pinned": row["pinned_at"] is not None,
                "pinned_at": row["pinned_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "last_turn_at": row["last_turn_at"],
                "last_turn_at_iso": format_timestamp_iso(row["last_turn_at"]),
                "exchange_count": row["exchange_count"] or 0,
                "deleted_exchange_count": row["deleted_exchange_count"] or 0,
            }
            for row in rows
        ]

    def save_exchange(
        self,
        session_id: str,
        model_name: str,
        user_message: str,
        assistant_response: str,
        assistant_created_at: int | None = None,
    ) -> ExchangeRecord:
        now = int(time.time())
        response_created_at = assistant_created_at or now
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown session_id: {session_id}")
            cursor = conn.execute(
                """
                INSERT INTO exchanges (
                    session_id, model_name, user_message, assistant_response,
                    assistant_created_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, model_name, user_message, assistant_response, response_created_at, now),
            )
            if row["title_is_auto"] and self._exchange_count(conn, session_id, include_deleted=False) == 1:
                conn.execute(
                    "UPDATE sessions SET title = ?, title_is_auto = 0, title_source = 'derived', updated_at = ? "
                    "WHERE session_id = ?",
                    (_derive_title(user_message), now, session_id),
                )
            else:
                conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id))
            exchange_id = int(cursor.lastrowid)
        return ExchangeRecord(
            exchange_id=exchange_id,
            session_id=session_id,
            model_name=model_name,
            user_message=user_message,
            assistant_response=assistant_response,
            assistant_created_at=response_created_at,
            created_at=now,
        )

    def get_exchange(self, exchange_id: int, include_deleted: bool = True) -> ExchangeRecord | None:
        with self._lock, self._connect() as conn:
            where = "exchange_id = ?" if include_deleted else "exchange_id = ? AND deleted_at IS NULL"
            row = conn.execute(f"SELECT * FROM exchanges WHERE {where}", (exchange_id,)).fetchone()
        return _exchange_from_row(row) if row else None

    def list_exchanges(self, session_id: str, include_deleted: bool = False) -> list[ExchangeRecord]:
        with self._lock, self._connect() as conn:
            deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
            rows = conn.execute(
                f"""
                SELECT * FROM exchanges
                WHERE session_id = ?
                {deleted_clause}
                ORDER BY created_at ASC, exchange_id ASC
                """,
                (session_id,),
            ).fetchall()
        return [_exchange_from_row(row) for row in rows]

    def get_latest_exchange(self, session_id: str) -> ExchangeRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM exchanges
                WHERE session_id = ? AND deleted_at IS NULL
                ORDER BY created_at DESC, exchange_id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return _exchange_from_row(row) if row is not None else None

    def update_exchange(
        self,
        exchange_id: int,
        *,
        model_name: str | None = None,
        user_message: str | None = None,
        assistant_response: str | None = None,
        actor: str = "admin",
    ) -> ExchangeRecord:
        updates: dict[str, str] = {}
        if model_name is not None:
            value = model_name.strip()
            if not value:
                raise ValueError("model_name must not be empty")
            updates["model_name"] = value
        if user_message is not None:
            value = user_message.strip()
            if not value:
                raise ValueError("user_message must not be empty")
            updates["user_message"] = value
        if assistant_response is not None:
            value = assistant_response.strip()
            if not value:
                raise ValueError("assistant_response must not be empty")
            updates["assistant_response"] = value

        now = int(time.time())
        with self._lock, self._connect() as conn:
            before = conn.execute("SELECT * FROM exchanges WHERE exchange_id = ?", (exchange_id,)).fetchone()
            if before is None:
                raise ValueError(f"Unknown exchange_id: {exchange_id}")
            if updates:
                assignments = [f"{column} = ?" for column in updates]
                values: list[Any] = list(updates.values())
                assignments.append("edited_at = ?")
                values.append(now)
                values.append(exchange_id)
                conn.execute(
                    f"UPDATE exchanges SET {', '.join(assignments)} WHERE exchange_id = ?",
                    values,
                )
                conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, before["session_id"]))
            after = conn.execute("SELECT * FROM exchanges WHERE exchange_id = ?", (exchange_id,)).fetchone()
            self._record_exchange_event(conn, "edit", actor, before, after)
        return _exchange_from_row(after)

    def delete_exchange(self, exchange_id: int, *, reason: str = "", actor: str = "admin") -> ExchangeRecord:
        now = int(time.time())
        resolved_reason = reason.strip() or "manual admin correction"
        with self._lock, self._connect() as conn:
            before = conn.execute("SELECT * FROM exchanges WHERE exchange_id = ?", (exchange_id,)).fetchone()
            if before is None:
                raise ValueError(f"Unknown exchange_id: {exchange_id}")
            if before["deleted_at"] is None:
                conn.execute(
                    """
                    UPDATE exchanges
                    SET deleted_at = ?, deleted_reason = ?, edited_at = ?
                    WHERE exchange_id = ?
                    """,
                    (now, resolved_reason, now, exchange_id),
                )
                conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, before["session_id"]))
            after = conn.execute("SELECT * FROM exchanges WHERE exchange_id = ?", (exchange_id,)).fetchone()
            self._record_exchange_event(conn, "delete", actor, before, after)
        return _exchange_from_row(after)

    def restore_exchange(self, exchange_id: int, *, actor: str = "admin") -> ExchangeRecord:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            before = conn.execute("SELECT * FROM exchanges WHERE exchange_id = ?", (exchange_id,)).fetchone()
            if before is None:
                raise ValueError(f"Unknown exchange_id: {exchange_id}")
            if before["deleted_at"] is not None:
                conn.execute(
                    """
                    UPDATE exchanges
                    SET deleted_at = NULL, deleted_reason = NULL, edited_at = ?
                    WHERE exchange_id = ?
                    """,
                    (now, exchange_id),
                )
                conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, before["session_id"]))
            after = conn.execute("SELECT * FROM exchanges WHERE exchange_id = ?", (exchange_id,)).fetchone()
            self._record_exchange_event(conn, "restore", actor, before, after)
        return _exchange_from_row(after)

    def mask_exchange_response(self, exchange_id: int, *, actor: str = "admin") -> ExchangeRecord:
        return self._set_exchange_response_masked(exchange_id, masked=True, actor=actor)

    def unmask_exchange_response(self, exchange_id: int, *, actor: str = "admin") -> ExchangeRecord:
        return self._set_exchange_response_masked(exchange_id, masked=False, actor=actor)

    def _set_exchange_response_masked(
        self,
        exchange_id: int,
        *,
        masked: bool,
        actor: str,
    ) -> ExchangeRecord:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            before = conn.execute("SELECT * FROM exchanges WHERE exchange_id = ?", (exchange_id,)).fetchone()
            if before is None:
                raise ValueError(f"Unknown exchange_id: {exchange_id}")
            should_update = (before["assistant_masked_at"] is None) == masked
            if should_update:
                conn.execute(
                    """
                    UPDATE exchanges
                    SET assistant_masked_at = ?, edited_at = ?
                    WHERE exchange_id = ?
                    """,
                    (now if masked else None, now, exchange_id),
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    (now, before["session_id"]),
                )
            after = conn.execute("SELECT * FROM exchanges WHERE exchange_id = ?", (exchange_id,)).fetchone()
            self._record_exchange_event(
                conn,
                "mask_response" if masked else "unmask_response",
                actor,
                before,
                after,
            )
        return _exchange_from_row(after)

    def list_exchange_events(self, exchange_id: int) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM exchange_admin_events
                WHERE exchange_id = ?
                ORDER BY created_at ASC, event_id ASC
                """,
                (exchange_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "exchange_id": row["exchange_id"],
                "session_id": row["session_id"],
                "action": row["action"],
                "actor": row["actor"],
                "before": json.loads(row["before_json"]) if row["before_json"] else None,
                "after": json.loads(row["after_json"]) if row["after_json"] else None,
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _seed_system_session_groups(conn: sqlite3.Connection) -> None:
        now = int(time.time())
        for group in SYSTEM_SESSION_GROUPS:
            conn.execute(
                """
                INSERT INTO session_groups (
                    group_id, name, color, icon_key, sort_order, is_system,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    name = excluded.name,
                    color = excluded.color,
                    icon_key = excluded.icon_key,
                    sort_order = excluded.sort_order,
                    is_system = 1,
                    updated_at = excluded.updated_at,
                    deleted_at = NULL
                """,
                (
                    group["group_id"],
                    group["name"],
                    group["color"],
                    group["icon_key"],
                    group["sort_order"],
                    now,
                    now,
                ),
            )

    @staticmethod
    def _demote_legacy_system_session_groups(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE session_groups
            SET is_system = 0, updated_at = ?
            WHERE group_id <> ? AND is_system = 1
            """,
            (int(time.time()), UNCATEGORIZED_GROUP_ID),
        )

    @staticmethod
    def _require_active_group(conn: sqlite3.Connection, group_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM session_groups WHERE group_id = ? AND deleted_at IS NULL",
            (group_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown session group: {group_id}")
        return row

    @staticmethod
    def _get_group_row_for_update(conn: sqlite3.Connection, group_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM session_groups WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown session group: {group_id}")
        if row["deleted_at"] is not None:
            raise ValueError(f"Session group is deleted: {group_id}")
        return row

    @staticmethod
    def _ensure_group_name_available(
        conn: sqlite3.Connection,
        name: str,
        excluding_group_id: str | None = None,
    ) -> None:
        rows = conn.execute(
            """
            SELECT group_id, name FROM session_groups
            WHERE deleted_at IS NULL
            """
        ).fetchall()
        folded = name.casefold()
        for row in rows:
            if excluding_group_id and row["group_id"] == excluding_group_id:
                continue
            if row["name"].casefold() == folded:
                raise ValueError(f"session group name already exists: {name}")

    @staticmethod
    def _exchange_count(conn: sqlite3.Connection, session_id: str, include_deleted: bool = False) -> int:
        deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
        row = conn.execute(
            f"SELECT COUNT(*) AS count FROM exchanges WHERE session_id = ? {deleted_clause}",
            (session_id,),
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def _record_exchange_event(
        conn: sqlite3.Connection,
        action: str,
        actor: str,
        before: sqlite3.Row | None,
        after: sqlite3.Row | None,
    ) -> None:
        source = after or before
        if source is None:
            return
        conn.execute(
            """
            INSERT INTO exchange_admin_events (
                exchange_id, session_id, action, actor, before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source["exchange_id"],
                source["session_id"],
                action,
                actor,
                json.dumps(_exchange_row_dict(before), ensure_ascii=False) if before else None,
                json.dumps(_exchange_row_dict(after), ensure_ascii=False) if after else None,
                int(time.time()),
            ),
        )


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        session_id=row["session_id"],
        title=row["title"],
        group_id=row["group_id"] or UNCATEGORIZED_GROUP_ID,
        title_is_auto=bool(row["title_is_auto"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        pinned_at=row["pinned_at"] if "pinned_at" in row.keys() else None,
        title_source=row["title_source"] if "title_source" in row.keys() else None,
    )


def _session_group_from_row(row: sqlite3.Row) -> SessionGroupRecord:
    return SessionGroupRecord(
        group_id=row["group_id"],
        name=row["name"],
        color=row["color"],
        icon_key=row["icon_key"],
        sort_order=row["sort_order"],
        is_system=bool(row["is_system"]),
        is_sensitive=bool(row["is_sensitive"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


def _session_group_payload(group: SessionGroupRecord) -> dict[str, Any]:
    return {
        "group_id": group.group_id,
        "name": group.name,
        "color": group.color,
        "icon_key": group.icon_key,
        "sort_order": group.sort_order,
        "is_system": group.is_system,
        "is_sensitive": group.is_sensitive,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
        "deleted_at": group.deleted_at,
    }


def _group_payload_from_join(row: sqlite3.Row) -> dict[str, Any]:
    group_id = row["group_id"] or UNCATEGORIZED_GROUP_ID
    return {
        "group_id": group_id,
        "name": row["group_name"] or group_id,
        "color": row["group_color"] or "#8b93a7",
        "icon_key": row["group_icon_key"] or "folder",
        "sort_order": row["group_sort_order"] if row["group_sort_order"] is not None else 0,
        "is_system": bool(row["group_is_system"]) if row["group_is_system"] is not None else False,
        "is_sensitive": bool(row["group_is_sensitive"]) if row["group_is_sensitive"] is not None else False,
    }


def _exchange_from_row(row: sqlite3.Row) -> ExchangeRecord:
    return ExchangeRecord(
        exchange_id=row["exchange_id"],
        session_id=row["session_id"],
        model_name=row["model_name"],
        user_message=row["user_message"],
        assistant_response=row["assistant_response"],
        assistant_created_at=row["assistant_created_at"] or row["created_at"],
        created_at=row["created_at"],
        assistant_masked_at=row["assistant_masked_at"],
        deleted_at=row["deleted_at"],
        deleted_reason=row["deleted_reason"],
        edited_at=row["edited_at"],
    )


def _session_file_from_row(row: sqlite3.Row) -> SessionFileRecord:
    return SessionFileRecord(
        file_id=row["file_id"],
        scope_type=row["scope_type"],
        session_id=row["session_id"],
        group_id=row["group_id"],
        filename=row["filename"],
        mime_type=row["mime_type"],
        content=row["content"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        content_kind=row["content_kind"],
        page_count=row["page_count"],
        extraction_status=row["extraction_status"],
        extracted_text_bytes=row["extracted_text_bytes"],
    )


def _session_file_manifest_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "file_id": row["file_id"],
        "scope_type": row["scope_type"],
        "session_id": row["session_id"],
        "group_id": row["group_id"],
        "filename": row["filename"],
        "mime_type": row["mime_type"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "content_kind": row["content_kind"],
        "page_count": row["page_count"],
        "extraction_status": row["extraction_status"],
        "extracted_text_bytes": row["extracted_text_bytes"],
        "text_available": row["content_kind"] == "text" or (row["content_kind"] == "pdf" and row["extraction_status"] == "ready"),
    }


def _require_file_visible_to_session(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    session_id: str | None,
    group_id: str | None,
) -> None:
    if session_id is None and group_id is None:
        return
    resolved_session_id = (session_id or "").strip()
    resolved_group_id = (group_id or "").strip()
    selected = conn.execute(
        "SELECT group_id FROM sessions WHERE session_id = ?",
        (resolved_session_id,),
    ).fetchone()
    visible = (
        selected is not None
        and selected["group_id"] == resolved_group_id
        and (
            (row["scope_type"] == "session" and row["session_id"] == resolved_session_id)
            or (row["scope_type"] == "group" and row["group_id"] == resolved_group_id)
        )
    )
    if not visible:
        raise SessionFileConflictError(
            f"File {row['file_id']} is no longer visible to session {resolved_session_id}"
        )


def session_file_payload(file: SessionFileRecord, include_content: bool = False) -> dict[str, Any]:
    payload = {
        "file_id": file.file_id,
        "scope_type": file.scope_type,
        "session_id": file.session_id,
        "group_id": file.group_id,
        "filename": file.filename,
        "mime_type": file.mime_type,
        "sha256": file.sha256,
        "size_bytes": file.size_bytes,
        "created_by": file.created_by,
        "created_at": file.created_at,
        "content_kind": file.content_kind,
        "page_count": file.page_count,
        "extraction_status": file.extraction_status,
        "extracted_text_bytes": file.extracted_text_bytes,
        "text_available": file.content_kind == "text" or (file.content_kind == "pdf" and file.extraction_status == "ready"),
    }
    if include_content:
        payload["content"] = file.content
    return payload


def _exchange_row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "exchange_id": row["exchange_id"],
        "session_id": row["session_id"],
        "model_name": row["model_name"],
        "user_message": row["user_message"],
        "assistant_response": row["assistant_response"],
        "assistant_created_at": row["assistant_created_at"] or row["created_at"],
        "created_at": row["created_at"],
        "assistant_masked_at": row["assistant_masked_at"],
        "deleted_at": row["deleted_at"],
        "deleted_reason": row["deleted_reason"],
        "edited_at": row["edited_at"],
    }


def _derive_title(user_message: str) -> str:
    text = " ".join(line.strip() for line in user_message.splitlines() if line.strip())
    text = " ".join(text.split())
    if not text:
        return "Untitled session"
    if len(text) <= 72:
        return text.rstrip(".!?")
    return text[:72].rsplit(" ", 1)[0].rstrip(".,;:!?") + "..."


def _validate_session_title(value: str) -> str:
    title = " ".join(value.strip().split())
    if not title:
        raise ValueError("session title must not be empty")
    if len(title) > 72:
        raise ValueError("session title must be 72 characters or fewer")
    return title


def _validate_group_id(value: str) -> str:
    if not value:
        raise ValueError("group_id must not be empty")
    if not all(char.isalnum() or char in {"-", "_", "."} for char in value):
        raise ValueError("group_id may only contain letters, numbers, dots, dashes, and underscores")
    return value


def _validate_group_name(value: str) -> str:
    name = " ".join(value.strip().split())
    if not name:
        raise ValueError("session group name must not be empty")
    if len(name) > 64:
        raise ValueError("session group name must be 64 characters or fewer")
    return name


def _validate_group_color(value: str) -> str:
    color = value.strip()
    if len(color) != 7 or not color.startswith("#"):
        raise ValueError("session group color must be a #RRGGBB hex value")
    try:
        int(color[1:], 16)
    except ValueError as exc:
        raise ValueError("session group color must be a #RRGGBB hex value") from exc
    return color.lower()


def _validate_group_icon_key(value: str) -> str:
    icon_key = value.strip()
    if icon_key not in SESSION_GROUP_ICON_KEYS:
        raise ValueError(f"Unknown session group icon_key: {icon_key}")
    return icon_key


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48]
    return slug or "group"


def _validate_file_label_name(value: str) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ValueError("Label name must not be empty")
    if len(name) > MAX_FILE_LABEL_NAME_CHARS:
        raise ValueError(f"Label name must be {MAX_FILE_LABEL_NAME_CHARS} characters or fewer")
    return name


def _validate_file_label_color(value: str) -> str:
    color = str(value or "").strip().lower()
    if color not in FILE_LABEL_COLORS:
        raise ValueError(f"Label color must be one of: {', '.join(FILE_LABEL_COLORS)}")
    return color


def _validate_file_name(value: str) -> str:
    filename = value.strip().replace("\\", "/").split("/")[-1]
    if not filename:
        raise ValueError("filename must not be empty")
    if len(filename) > 160:
        raise ValueError("filename must be 160 characters or fewer")
    if filename in {".", ".."}:
        raise ValueError("filename is not allowed")
    return filename


def _validate_file_content(value: str) -> tuple[str, int, str]:
    content = value if isinstance(value, str) else str(value)
    size_bytes = len(content.encode("utf-8"))
    if size_bytes == 0:
        raise ValueError("content must not be empty")
    if size_bytes > MAX_SESSION_FILE_BYTES:
        raise ValueError(f"content must be {MAX_SESSION_FILE_BYTES} bytes or fewer")
    return content, size_bytes, hashlib.sha256(content.encode("utf-8")).hexdigest()


def _validate_pdf_file(
    filename: str,
    binary_content: bytes,
    *,
    extracted_text: str,
    page_count: int,
    extraction_status: str,
    extracted_text_bytes: int,
) -> dict[str, Any]:
    resolved_filename = _validate_file_name(filename)
    if not resolved_filename.lower().endswith(".pdf"):
        raise ValueError("PDF filename must end with .pdf")
    if not isinstance(binary_content, bytes) or not binary_content:
        raise ValueError("PDF binary content must not be empty")
    if page_count < 1:
        raise ValueError("PDF page_count must be positive")
    if extraction_status not in {"ready", "no_text"}:
        raise ValueError("PDF extraction_status must be ready or no_text")
    resolved_text = extracted_text if isinstance(extracted_text, str) else str(extracted_text)
    actual_text_bytes = len(resolved_text.encode("utf-8"))
    if extracted_text_bytes != actual_text_bytes:
        raise ValueError("PDF extracted_text_bytes does not match extracted_text")
    if extraction_status == "ready" and not resolved_text:
        raise ValueError("Ready PDF extraction must contain text")
    if extraction_status == "no_text" and resolved_text:
        raise ValueError("No-text PDF extraction must not contain text")
    return {
        "filename": resolved_filename,
        "binary_content": binary_content,
        "extracted_text": resolved_text,
        "sha256": hashlib.sha256(binary_content).hexdigest(),
        "size_bytes": len(binary_content),
        "page_count": page_count,
        "extraction_status": extraction_status,
        "extracted_text_bytes": extracted_text_bytes,
    }


def _validate_mime_type(value: str) -> str:
    mime_type = value.strip() or "text/markdown"
    if len(mime_type) > 120 or not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", mime_type):
        raise ValueError("mime_type must look like type/subtype")
    return mime_type.lower()


def _output_probe_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "harness_label": row["harness_label"],
        "content_profile": row["content_profile"],
        "target_chars": row["target_chars"],
        "payload_char_count": row["payload_char_count"],
        "server_result_json_chars": row["server_result_json_chars"],
        "block_count": row["block_count"],
        "block_size": row["block_size"],
        "tool_output_mode": row["tool_output_mode"],
        "blocks": json.loads(row["blocks_json"]),
        "status": row["status"],
        "result_class": row["result_class"],
        "observed_canaries": json.loads(row["observed_canaries_json"]),
        "matched_checkpoints": json.loads(row["matched_checkpoints_json"]),
        "last_complete_block_index": row["last_complete_block_index"],
        "last_complete_canary": row["last_complete_canary"],
        "noticed_truncation": bool(row["noticed_truncation"]),
        "notes": row["notes"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "reported_at": row["reported_at"],
    }


# Blocks are parked here before being placed, so a placement never collides.
_LAST_POSITION = 1_000_000_000


def _estimate_tokens(chars: int) -> int:
    """Library cards show a quick estimate; a block counts its real tokens once dropped."""
    return (int(chars or 0) + 3) // 4


def _new_context_id() -> str:
    return CONTEXT_ID_PREFIX + "".join(secrets.choice(CONTEXT_ID_ALPHABET) for _ in range(CONTEXT_ID_LENGTH))


def _validate_context_name(value: str) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ValueError("Context name must not be empty")
    if len(name) > MAX_CONTEXT_NAME_CHARS:
        raise ValueError(f"Context name must be {MAX_CONTEXT_NAME_CHARS} characters or fewer")
    return name


def _validate_context_color(value: str) -> str:
    color = str(value or "").strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", color):
        raise ValueError("Context color must be a #rrggbb hex color")
    return color


def _context_block_from_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = {
        "block_id": row["block_id"],
        "context_id": row["context_id"],
        "position": row["position"],
        "source_type": row["source_type"],
        "source_key": row["source_key"],
        "source_session_id": row["source_session_id"],
        "source_file_id": row["source_file_id"],
        "title": row["title"],
        "source_hash": row["source_hash"],
        "found_ids": json.loads(row["found_ids"] or "{}"),
        "token_count": row["token_count"],
        "snapshot_at": row["snapshot_at"],
        "created_at": row["created_at"],
        "meta": json.loads(row["source_meta"] or "{}") if "source_meta" in row.keys() else {},
    }
    if "content" in row.keys():
        payload["content"] = row["content"]
    return payload


def _context_snapshot_from_row(row: sqlite3.Row) -> ContextBlockSnapshot:
    return ContextBlockSnapshot(
        source_type=row["source_type"],
        source_key=row["source_key"],
        source_session_id=row["source_session_id"],
        source_file_id=row["source_file_id"],
        title=row["title"],
        content=row["content"],
        source_hash=row["source_hash"],
        found_ids=json.loads(row["found_ids"] or "{}"),
        token_count=row["token_count"],
        meta=json.loads(row["source_meta"] or "{}") if "source_meta" in row.keys() else {},
    )
