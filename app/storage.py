from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from app.graph_config import (
    GRAPH_SCHEMA_VERSION,
    default_graph_profile,
    validate_graph_draft,
)
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
GRAPH_ENABLED_SETTING = "graph.enabled"
GRAPH_ACTIVE_PROFILE_SETTING = "graph.active_profile_id"

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
    context_pack_id: str
    context_pack_version: str | None
    title_is_auto: bool
    created_at: int
    updated_at: int


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


class Store:
    def __init__(
        self,
        db_path: Path,
        *,
        pdf_storage_max_bytes: int = 1_000_000_000,
        allow_startup_migrations: bool = True,
    ):
        self.db_path = db_path
        self.pdf_storage_max_bytes = pdf_storage_max_bytes
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
                    secret_expires_at INTEGER
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
                    context_pack_id TEXT NOT NULL,
                    context_pack_version TEXT,
                    title_is_auto INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
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

                CREATE TABLE IF NOT EXISTS graph_extractor_profiles (
                    profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version INTEGER NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    max_concepts INTEGER NOT NULL,
                    inactivity_hours INTEGER NOT NULL,
                    include_sensitive INTEGER NOT NULL DEFAULT 0,
                    schema_version TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS graph_extractor_drafts (
                    draft_id INTEGER PRIMARY KEY CHECK (draft_id = 1),
                    base_profile_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    max_concepts INTEGER NOT NULL,
                    inactivity_hours INTEGER NOT NULL,
                    include_sensitive INTEGER NOT NULL DEFAULT 0,
                    updated_by TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(base_profile_id) REFERENCES graph_extractor_profiles(profile_id)
                );

                CREATE TABLE IF NOT EXISTS graph_jobs (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    source_exchange_id INTEGER NOT NULL,
                    profile_id INTEGER NOT NULL,
                    schema_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at INTEGER NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at INTEGER,
                    started_at INTEGER,
                    completed_at INTEGER,
                    error_code TEXT,
                    error_message TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY(source_exchange_id) REFERENCES exchanges(exchange_id),
                    FOREIGN KEY(profile_id) REFERENCES graph_extractor_profiles(profile_id),
                    UNIQUE(session_id, source_exchange_id, profile_id, schema_version)
                );

                CREATE INDEX IF NOT EXISTS idx_graph_jobs_claim
                    ON graph_jobs(status, available_at, lease_expires_at, job_id);

                CREATE TABLE IF NOT EXISTS graph_extractions (
                    extraction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    source_exchange_id INTEGER NOT NULL,
                    profile_id INTEGER NOT NULL,
                    schema_version TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES graph_jobs(job_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY(source_exchange_id) REFERENCES exchanges(exchange_id),
                    FOREIGN KEY(profile_id) REFERENCES graph_extractor_profiles(profile_id)
                );

                CREATE TABLE IF NOT EXISTS graph_extraction_concepts (
                    extraction_concept_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    extraction_id INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    canonical_name TEXT NOT NULL,
                    concept_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    FOREIGN KEY(extraction_id) REFERENCES graph_extractions(extraction_id) ON DELETE CASCADE,
                    UNIQUE(extraction_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS graph_extraction_evidence (
                    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    extraction_concept_id INTEGER NOT NULL,
                    exchange_id INTEGER NOT NULL,
                    quote TEXT NOT NULL,
                    FOREIGN KEY(extraction_concept_id) REFERENCES graph_extraction_concepts(extraction_concept_id) ON DELETE CASCADE,
                    FOREIGN KEY(exchange_id) REFERENCES exchanges(exchange_id)
                );

                CREATE TABLE IF NOT EXISTS graph_session_current (
                    session_id TEXT PRIMARY KEY,
                    extraction_id INTEGER NOT NULL UNIQUE,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY(extraction_id) REFERENCES graph_extractions(extraction_id)
                );

                CREATE TABLE IF NOT EXISTS graph_lab_runs (
                    lab_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    source_exchange_id INTEGER NOT NULL,
                    settings_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    completed_at INTEGER,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY(source_exchange_id) REFERENCES exchanges(exchange_id)
                );

                CREATE INDEX IF NOT EXISTS idx_graph_lab_runs_created
                    ON graph_lab_runs(created_at DESC, lab_run_id DESC);
                """
            )
            self._ensure_column(conn, "refresh_tokens", "resource", "TEXT")
            self._ensure_column(conn, "sessions", "context_pack_version", "TEXT")
            self._ensure_column(conn, "sessions", "title_is_auto", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sessions", "group_id", f"TEXT NOT NULL DEFAULT '{UNCATEGORIZED_GROUP_ID}'")
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
            self._seed_graph_config(conn)
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
    def _seed_graph_config(conn: sqlite3.Connection) -> None:
        now = int(time.time())
        profile = default_graph_profile()
        conn.execute(
            """
            INSERT OR IGNORE INTO graph_extractor_profiles (
                profile_id, version, provider, model, effort, prompt,
                max_concepts, inactivity_hours, include_sensitive,
                schema_version, created_by, created_at
            ) VALUES (1, 1, ?, ?, ?, ?, ?, ?, ?, ?, 'system', ?)
            """,
            (
                profile["provider"],
                profile["model"],
                profile["effort"],
                profile["prompt"],
                profile["max_concepts"],
                profile["inactivity_hours"],
                int(profile["include_sensitive"]),
                GRAPH_SCHEMA_VERSION,
                now,
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings(key, value, updated_at) VALUES (?, '1', ?)",
            (GRAPH_ACTIVE_PROFILE_SETTING, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings(key, value, updated_at) VALUES (?, 'false', ?)",
            (GRAPH_ENABLED_SETTING, now),
        )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
        )

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

    def get_graph_config(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            active_row = conn.execute(
                """
                SELECT p.*
                FROM graph_extractor_profiles p
                JOIN app_settings s
                  ON s.key = ? AND CAST(s.value AS INTEGER) = p.profile_id
                """,
                (GRAPH_ACTIVE_PROFILE_SETTING,),
            ).fetchone()
            draft_row = conn.execute(
                "SELECT * FROM graph_extractor_drafts WHERE draft_id = 1"
            ).fetchone()
            enabled_row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (GRAPH_ENABLED_SETTING,),
            ).fetchone()
        if active_row is None:
            raise RuntimeError("Graph active extractor profile is missing.")
        draft = _graph_draft_row(draft_row) if draft_row else None
        return {
            "enabled": bool(enabled_row and enabled_row["value"] == "true"),
            "locked": draft is None,
            "active_profile": _graph_profile_row(active_row),
            "draft": draft,
        }

    def unlock_graph_profile(self, actor: str) -> dict[str, Any]:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM graph_extractor_drafts WHERE draft_id = 1"
            ).fetchone()
            if existing:
                return _graph_draft_row(existing)
            active = conn.execute(
                """
                SELECT p.* FROM graph_extractor_profiles p
                JOIN app_settings s
                  ON s.key = ? AND CAST(s.value AS INTEGER) = p.profile_id
                """,
                (GRAPH_ACTIVE_PROFILE_SETTING,),
            ).fetchone()
            if active is None:
                raise RuntimeError("Graph active extractor profile is missing.")
            conn.execute(
                """
                INSERT INTO graph_extractor_drafts (
                    draft_id, base_profile_id, provider, model, effort, prompt,
                    max_concepts, inactivity_hours, include_sensitive, updated_by, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    active["profile_id"],
                    active["provider"],
                    active["model"],
                    active["effort"],
                    active["prompt"],
                    active["max_concepts"],
                    active["inactivity_hours"],
                    active["include_sensitive"],
                    actor,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM graph_extractor_drafts WHERE draft_id = 1"
            ).fetchone()
        assert row is not None
        return _graph_draft_row(row)

    def update_graph_draft(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        values = validate_graph_draft(payload)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM graph_extractor_drafts WHERE draft_id = 1"
            ).fetchone() is None:
                raise ValueError("Unlock the extractor profile before editing it.")
            conn.execute(
                """
                UPDATE graph_extractor_drafts
                SET provider = ?, model = ?, effort = ?, prompt = ?,
                    max_concepts = ?, inactivity_hours = ?, include_sensitive = ?,
                    updated_by = ?, updated_at = ?
                WHERE draft_id = 1
                """,
                (
                    values["provider"],
                    values["model"],
                    values["effort"],
                    values["prompt"],
                    values["max_concepts"],
                    values["inactivity_hours"],
                    int(values["include_sensitive"]),
                    actor,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM graph_extractor_drafts WHERE draft_id = 1"
            ).fetchone()
        assert row is not None
        return _graph_draft_row(row)

    def discard_graph_draft(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM graph_extractor_drafts WHERE draft_id = 1")

    def activate_graph_draft(self, actor: str) -> dict[str, Any]:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            draft = conn.execute(
                "SELECT * FROM graph_extractor_drafts WHERE draft_id = 1"
            ).fetchone()
            if draft is None:
                raise ValueError("Unlock and edit the extractor profile before activating it.")
            version = int(
                conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM graph_extractor_profiles").fetchone()[0]
            )
            cursor = conn.execute(
                """
                INSERT INTO graph_extractor_profiles (
                    version, provider, model, effort, prompt, max_concepts,
                    inactivity_hours, include_sensitive, schema_version, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version,
                    draft["provider"],
                    draft["model"],
                    draft["effort"],
                    draft["prompt"],
                    draft["max_concepts"],
                    draft["inactivity_hours"],
                    draft["include_sensitive"],
                    GRAPH_SCHEMA_VERSION,
                    actor,
                    now,
                ),
            )
            profile_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (GRAPH_ACTIVE_PROFILE_SETTING, str(profile_id), now),
            )
            conn.execute("DELETE FROM graph_extractor_drafts WHERE draft_id = 1")
            row = conn.execute(
                "SELECT * FROM graph_extractor_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        assert row is not None
        return _graph_profile_row(row)

    def set_graph_enabled(self, enabled: bool) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean.")
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (GRAPH_ENABLED_SETTING, "true" if enabled else "false", now),
            )
            if not enabled:
                conn.execute(
                    """
                    UPDATE graph_jobs
                    SET status = 'interrupted', lease_owner = NULL, lease_expires_at = NULL,
                        completed_at = ?, updated_at = ?, error_code = 'graph_disabled',
                        error_message = 'Graph was turned off.'
                    WHERE status = 'running'
                    """,
                    (now, now),
                )
        return self.get_graph_config()

    def enqueue_eligible_graph_jobs(self, *, now: int | None = None) -> list[dict[str, Any]]:
        current_time = int(time.time()) if now is None else int(now)
        created_ids: list[int] = []
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            enabled = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (GRAPH_ENABLED_SETTING,),
            ).fetchone()
            if enabled is None or enabled["value"] != "true":
                return []
            profile = conn.execute(
                """
                SELECT p.* FROM graph_extractor_profiles p
                JOIN app_settings s
                  ON s.key = ? AND CAST(s.value AS INTEGER) = p.profile_id
                """,
                (GRAPH_ACTIVE_PROFILE_SETTING,),
            ).fetchone()
            if profile is None:
                return []
            cutoff = current_time - int(profile["inactivity_hours"]) * 3600
            candidates = conn.execute(
                """
                SELECT
                    s.session_id,
                    MAX(CASE WHEN e.deleted_at IS NULL THEN e.exchange_id END) AS source_exchange_id,
                    MAX(CASE WHEN e.deleted_at IS NULL THEN COALESCE(e.assistant_created_at, e.created_at) END) AS last_activity
                FROM sessions s
                JOIN session_groups g ON g.group_id = s.group_id AND g.deleted_at IS NULL
                LEFT JOIN exchanges e ON e.session_id = s.session_id
                WHERE (? = 1 OR g.is_sensitive = 0)
                GROUP BY s.session_id
                HAVING source_exchange_id IS NOT NULL AND last_activity <= ?
                ORDER BY last_activity ASC, s.session_id ASC
                """,
                (int(profile["include_sensitive"]), cutoff),
            ).fetchall()
            for candidate in candidates:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO graph_jobs (
                        session_id, source_exchange_id, profile_id, schema_version,
                        status, available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
                    """,
                    (
                        candidate["session_id"],
                        candidate["source_exchange_id"],
                        profile["profile_id"],
                        profile["schema_version"],
                        current_time,
                        current_time,
                        current_time,
                    ),
                )
                if cursor.rowcount:
                    created_ids.append(int(cursor.lastrowid))
            if not created_ids:
                return []
            placeholders = ",".join("?" for _ in created_ids)
            rows = conn.execute(
                f"SELECT * FROM graph_jobs WHERE job_id IN ({placeholders}) ORDER BY job_id",
                created_ids,
            ).fetchall()
        return [_graph_job_row(row) for row in rows]

    def claim_graph_job(
        self,
        lease_owner: str,
        *,
        now: int | None = None,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        current_time = int(time.time()) if now is None else int(now)
        if not lease_owner.strip():
            raise ValueError("lease_owner must not be empty.")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive.")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            enabled = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (GRAPH_ENABLED_SETTING,),
            ).fetchone()
            if enabled is None or enabled["value"] != "true":
                return None
            conn.execute(
                """
                UPDATE graph_jobs
                SET status = 'terminal_failed', completed_at = ?, updated_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL,
                    error_code = 'lease_exhausted',
                    error_message = 'Graph worker lease expired after the final attempt.'
                WHERE status = 'running'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                  AND attempts >= max_attempts
                """,
                (current_time, current_time, current_time),
            )
            active = conn.execute(
                """
                SELECT 1 FROM graph_jobs
                WHERE status = 'running' AND lease_expires_at > ?
                LIMIT 1
                """,
                (current_time,),
            ).fetchone()
            if active is not None:
                return None
            row = conn.execute(
                """
                SELECT * FROM graph_jobs
                WHERE (
                    status = 'queued'
                    OR (status = 'retryable_failed' AND available_at <= ? AND attempts < max_attempts)
                    OR (status = 'running' AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= ? AND attempts < max_attempts)
                )
                ORDER BY available_at ASC, job_id ASC
                LIMIT 1
                """,
                (current_time, current_time),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE graph_jobs
                SET status = 'running', attempts = attempts + 1, lease_owner = ?,
                    lease_expires_at = ?, started_at = COALESCE(started_at, ?),
                    completed_at = NULL, error_code = NULL, error_message = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    lease_owner.strip(),
                    current_time + lease_seconds,
                    current_time,
                    current_time,
                    row["job_id"],
                ),
            )
            claimed = conn.execute(
                "SELECT * FROM graph_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
        assert claimed is not None
        return _graph_job_row(claimed)

    def renew_graph_job_lease(
        self,
        job_id: int,
        lease_owner: str,
        *,
        now: int | None = None,
        lease_seconds: int = 300,
    ) -> bool:
        current_time = int(time.time()) if now is None else int(now)
        normalized_owner = lease_owner.strip()
        if not normalized_owner:
            raise ValueError("lease_owner must not be empty.")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive.")
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE graph_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ?
                  AND status = 'running'
                  AND lease_owner = ?
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at > ?
                """,
                (
                    current_time + lease_seconds,
                    current_time,
                    job_id,
                    normalized_owner,
                    current_time,
                ),
            )
        return cursor.rowcount == 1

    def list_graph_jobs(self, *, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._lock, self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM graph_jobs ORDER BY created_at DESC, job_id DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM graph_jobs WHERE status = ? ORDER BY created_at DESC, job_id DESC LIMIT ?",
                    (status, safe_limit),
                ).fetchall()
        return [_graph_job_row(row) for row in rows]

    def get_graph_job_context(self, job_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    j.*,
                    p.version AS profile_version,
                    p.provider,
                    p.model,
                    p.effort,
                    p.prompt,
                    p.max_concepts,
                    p.inactivity_hours,
                    p.include_sensitive
                FROM graph_jobs j
                JOIN graph_extractor_profiles p ON p.profile_id = j.profile_id
                WHERE j.job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        payload = _graph_job_row(row)
        payload["profile"] = {
            "profile_id": row["profile_id"],
            "version": row["profile_version"],
            "provider": row["provider"],
            "model": row["model"],
            "effort": row["effort"],
            "prompt": row["prompt"],
            "max_concepts": row["max_concepts"],
            "inactivity_hours": row["inactivity_hours"],
            "include_sensitive": bool(row["include_sensitive"]),
            "schema_version": row["schema_version"],
        }
        return payload

    def publish_graph_extraction(
        self,
        job_id: int,
        result: dict[str, Any],
        *,
        lease_owner: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        current_time = int(time.time()) if now is None else int(now)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM graph_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if (
                job is None
                or job["status"] != "running"
                or job["lease_owner"] != lease_owner
                or job["lease_expires_at"] is None
                or int(job["lease_expires_at"]) <= current_time
            ):
                raise ValueError("Graph job is no longer publishable.")
            cursor = conn.execute(
                """
                INSERT INTO graph_extractions (
                    job_id, session_id, source_exchange_id, profile_id,
                    schema_version, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job["session_id"],
                    job["source_exchange_id"],
                    job["profile_id"],
                    job["schema_version"],
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    current_time,
                ),
            )
            extraction_id = int(cursor.lastrowid)
            for ordinal, concept in enumerate(result["concepts"]):
                concept_cursor = conn.execute(
                    """
                    INSERT INTO graph_extraction_concepts (
                        extraction_id, ordinal, canonical_name, concept_type, summary
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        extraction_id,
                        ordinal,
                        concept["canonical_name"],
                        concept["type"],
                        concept["summary"],
                    ),
                )
                extraction_concept_id = int(concept_cursor.lastrowid)
                conn.executemany(
                    """
                    INSERT INTO graph_extraction_evidence (
                        extraction_concept_id, exchange_id, quote
                    ) VALUES (?, ?, ?)
                    """,
                    [
                        (extraction_concept_id, item["exchange_id"], item["quote"])
                        for item in concept["evidence"]
                    ],
                )
            conn.execute(
                """
                INSERT INTO graph_session_current(session_id, extraction_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    extraction_id = excluded.extraction_id,
                    updated_at = excluded.updated_at
                """,
                (job["session_id"], extraction_id, current_time),
            )
            conn.execute(
                """
                UPDATE graph_jobs
                SET status = 'completed', completed_at = ?, updated_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL,
                    error_code = NULL, error_message = NULL
                WHERE job_id = ?
                """,
                (current_time, current_time, job_id),
            )
        analysis = self.get_graph_analysis(str(job["session_id"]))
        if analysis is None:
            raise RuntimeError("Graph extraction was not published.")
        return analysis

    def fail_graph_job(
        self,
        job_id: int,
        *,
        lease_owner: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        current_time = int(time.time()) if now is None else int(now)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM graph_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != "running"
                or row["lease_owner"] != lease_owner
                or row["lease_expires_at"] is None
                or int(row["lease_expires_at"]) <= current_time
            ):
                return _graph_job_row(row) if row else None
            will_retry = retryable and int(row["attempts"]) < int(row["max_attempts"])
            status = "retryable_failed" if will_retry else "terminal_failed"
            available_at = current_time + min(60 * (2 ** max(int(row["attempts"]) - 1, 0)), 3600)
            conn.execute(
                """
                UPDATE graph_jobs
                SET status = ?, available_at = ?, completed_at = ?, updated_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL,
                    error_code = ?, error_message = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    available_at,
                    current_time,
                    current_time,
                    error_code[:80],
                    error_message[:500],
                    job_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM graph_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _graph_job_row(updated) if updated else None

    def get_graph_analysis(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            extraction = conn.execute(
                """
                SELECT e.*, p.version AS profile_version, p.model, p.effort
                FROM graph_session_current c
                JOIN graph_extractions e ON e.extraction_id = c.extraction_id
                JOIN graph_extractor_profiles p ON p.profile_id = e.profile_id
                WHERE c.session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if extraction is None:
                return None
            concepts = conn.execute(
                """
                SELECT * FROM graph_extraction_concepts
                WHERE extraction_id = ? ORDER BY ordinal ASC
                """,
                (extraction["extraction_id"],),
            ).fetchall()
            concept_payloads: list[dict[str, Any]] = []
            for concept in concepts:
                evidence = conn.execute(
                    """
                    SELECT exchange_id, quote FROM graph_extraction_evidence
                    WHERE extraction_concept_id = ? ORDER BY evidence_id ASC
                    """,
                    (concept["extraction_concept_id"],),
                ).fetchall()
                concept_payloads.append(
                    {
                        "extraction_concept_id": concept["extraction_concept_id"],
                        "canonical_name": concept["canonical_name"],
                        "type": concept["concept_type"],
                        "summary": concept["summary"],
                        "evidence": [dict(item) for item in evidence],
                    }
                )
        return {
            "extraction_id": extraction["extraction_id"],
            "job_id": extraction["job_id"],
            "session_id": extraction["session_id"],
            "source_exchange_id": extraction["source_exchange_id"],
            "profile_id": extraction["profile_id"],
            "profile_version": extraction["profile_version"],
            "model": extraction["model"],
            "effort": extraction["effort"],
            "schema_version": extraction["schema_version"],
            "created_at": extraction["created_at"],
            "concepts": concept_payloads,
        }

    def list_graph_analysis_sessions(self, *, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._lock, self._connect() as conn:
            active = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (GRAPH_ACTIVE_PROFILE_SETTING,),
            ).fetchone()
            active_profile_id = int(active["value"]) if active else 0
            active_profile = conn.execute(
                "SELECT include_sensitive FROM graph_extractor_profiles WHERE profile_id = ?",
                (active_profile_id,),
            ).fetchone()
            include_sensitive = bool(active_profile and active_profile["include_sensitive"])
            rows = conn.execute(
                """
                SELECT
                    s.session_id, s.title, s.group_id, g.name AS group_name,
                    g.is_sensitive,
                    MAX(CASE WHEN x.deleted_at IS NULL THEN x.exchange_id END) AS latest_exchange_id,
                    MAX(CASE WHEN x.deleted_at IS NULL THEN COALESCE(x.assistant_created_at, x.created_at) END) AS last_activity,
                    e.extraction_id, e.source_exchange_id, e.profile_id, e.created_at AS extracted_at,
                    p.version AS profile_version,
                    (SELECT j.status FROM graph_jobs j WHERE j.session_id = s.session_id ORDER BY j.created_at DESC, j.job_id DESC LIMIT 1) AS latest_job_status,
                    (SELECT j.error_code FROM graph_jobs j WHERE j.session_id = s.session_id ORDER BY j.created_at DESC, j.job_id DESC LIMIT 1) AS latest_job_error_code,
                    COUNT(DISTINCT c.extraction_concept_id) AS concept_count
                FROM sessions s
                JOIN session_groups g ON g.group_id = s.group_id
                LEFT JOIN exchanges x ON x.session_id = s.session_id
                LEFT JOIN graph_session_current current ON current.session_id = s.session_id
                LEFT JOIN graph_extractions e ON e.extraction_id = current.extraction_id
                LEFT JOIN graph_extractor_profiles p ON p.profile_id = e.profile_id
                LEFT JOIN graph_extraction_concepts c ON c.extraction_id = e.extraction_id
                GROUP BY s.session_id
                ORDER BY last_activity DESC, s.session_id ASC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        payloads: list[dict[str, Any]] = []
        for row in rows:
            if row["is_sensitive"] and not include_sensitive:
                freshness = "excluded"
            elif row["latest_exchange_id"] is None:
                freshness = "empty"
            elif row["extraction_id"] is None:
                freshness = "never_scanned"
            elif int(row["profile_id"]) != active_profile_id:
                freshness = "stale_profile"
            elif int(row["source_exchange_id"]) != int(row["latest_exchange_id"]):
                freshness = "stale_source"
            else:
                freshness = "current"
            payloads.append(
                {
                    "session_id": row["session_id"],
                    "title": row["title"],
                    "group_id": row["group_id"],
                    "group_name": row["group_name"],
                    "is_sensitive": bool(row["is_sensitive"]),
                    "latest_exchange_id": row["latest_exchange_id"],
                    "last_activity": row["last_activity"],
                    "extraction_id": row["extraction_id"],
                    "source_exchange_id": row["source_exchange_id"],
                    "profile_id": row["profile_id"],
                    "profile_version": row["profile_version"],
                    "extracted_at": row["extracted_at"],
                    "concept_count": row["concept_count"] or 0,
                    "freshness": freshness,
                    "latest_job_status": row["latest_job_status"],
                    "latest_job_error_code": row["latest_job_error_code"],
                }
            )
        return payloads

    def create_graph_lab_run(
        self,
        session_id: str,
        source_exchange_id: int,
        settings: dict[str, Any],
        actor: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        current_time = int(time.time()) if now is None else int(now)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO graph_lab_runs (
                    session_id, source_exchange_id, settings_json, status,
                    created_by, created_at
                ) VALUES (?, ?, ?, 'queued', ?, ?)
                """,
                (
                    session_id,
                    source_exchange_id,
                    json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
                    actor,
                    current_time,
                ),
            )
            run_id = int(cursor.lastrowid)
        run = self.get_graph_lab_run(run_id)
        assert run is not None
        return run

    def start_graph_lab_run(self, lab_run_id: int, *, now: int | None = None) -> dict[str, Any]:
        current_time = int(time.time()) if now is None else int(now)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE graph_lab_runs SET status = 'running', started_at = ? WHERE lab_run_id = ? AND status = 'queued'",
                (current_time, lab_run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Lab run is not queued.")
        run = self.get_graph_lab_run(lab_run_id)
        assert run is not None
        return run

    def complete_graph_lab_run(
        self,
        lab_run_id: int,
        result: dict[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        current_time = int(time.time()) if now is None else int(now)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE graph_lab_runs
                SET status = 'completed', result_json = ?, completed_at = ?,
                    error_code = NULL, error_message = NULL
                WHERE lab_run_id = ? AND status = 'running'
                """,
                (json.dumps(result, ensure_ascii=False, separators=(",", ":")), current_time, lab_run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Lab run is no longer completable.")
        run = self.get_graph_lab_run(lab_run_id)
        assert run is not None
        return run

    def fail_graph_lab_run(
        self,
        lab_run_id: int,
        *,
        error_code: str,
        error_message: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        current_time = int(time.time()) if now is None else int(now)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE graph_lab_runs
                SET status = 'failed', error_code = ?, error_message = ?, completed_at = ?
                WHERE lab_run_id = ? AND status IN ('queued', 'running')
                """,
                (error_code[:80], error_message[:500], current_time, lab_run_id),
            )
        run = self.get_graph_lab_run(lab_run_id)
        if run is None:
            raise ValueError("Unknown Lab run.")
        return run

    def get_graph_lab_run(self, lab_run_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM graph_lab_runs WHERE lab_run_id = ?", (lab_run_id,)
            ).fetchone()
        return _graph_lab_row(row) if row else None

    def list_graph_lab_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM graph_lab_runs ORDER BY created_at DESC, lab_run_id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [_graph_lab_row(row) for row in rows]

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
        context_pack_id: str,
        context_pack_version: str | None = None,
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    title,
                    resolved_group_id,
                    context_pack_id,
                    context_pack_version,
                    int(title_is_auto),
                    now,
                    now,
                ),
            )
        return SessionRecord(
            session_id=session_id,
            title=title,
            group_id=resolved_group_id,
            context_pack_id=context_pack_id,
            context_pack_version=context_pack_version,
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

    def get_session_file_binary(self, file_id: int) -> tuple[str, str, bytes | None] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT filename, content_kind, binary_content
                FROM session_files
                WHERE file_id = ?
                """,
                (file_id,),
            ).fetchone()
        if row is None:
            return None
        return row["filename"], row["content_kind"], row["binary_content"]

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
            if row["content_kind"] == "pdf":
                raise ValueError("PDF files cannot be edited")
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

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return None
        return _session_from_row(row)

    def set_session_title(self, session_id: str, title: str) -> SessionRecord:
        resolved_title = _validate_session_title(title)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown session_id: {session_id}")
            conn.execute(
                "UPDATE sessions SET title = ?, title_is_auto = 0, updated_at = ? WHERE session_id = ?",
                (resolved_title, now, session_id),
            )
            updated = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return _session_from_row(updated)

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
            updated = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return _session_from_row(updated)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.session_id,
                    s.title,
                    s.group_id,
                    s.context_pack_id,
                    s.context_pack_version,
                    s.title_is_auto,
                    s.created_at,
                    s.updated_at,
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
                "context_pack_id": row["context_pack_id"],
                "context_pack_version": row["context_pack_version"],
                "title_is_auto": bool(row["title_is_auto"]),
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
                    "UPDATE sessions SET title = ?, title_is_auto = 0, updated_at = ? WHERE session_id = ?",
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
        context_pack_id=row["context_pack_id"],
        context_pack_version=row["context_pack_version"],
        title_is_auto=bool(row["title_is_auto"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
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
        "text_available": row["content_kind"] != "pdf" or row["extraction_status"] == "ready",
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
        "text_available": file.content_kind != "pdf" or file.extraction_status == "ready",
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


def _graph_profile_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "profile_id": row["profile_id"],
        "version": row["version"],
        "provider": row["provider"],
        "model": row["model"],
        "effort": row["effort"],
        "prompt": row["prompt"],
        "max_concepts": row["max_concepts"],
        "inactivity_hours": row["inactivity_hours"],
        "include_sensitive": bool(row["include_sensitive"]),
        "schema_version": row["schema_version"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def _graph_draft_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "draft_id": row["draft_id"],
        "base_profile_id": row["base_profile_id"],
        "provider": row["provider"],
        "model": row["model"],
        "effort": row["effort"],
        "prompt": row["prompt"],
        "max_concepts": row["max_concepts"],
        "inactivity_hours": row["inactivity_hours"],
        "include_sensitive": bool(row["include_sensitive"]),
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
    }


def _graph_job_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "session_id": row["session_id"],
        "source_exchange_id": row["source_exchange_id"],
        "profile_id": row["profile_id"],
        "schema_version": row["schema_version"],
        "status": row["status"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "available_at": row["available_at"],
        "lease_owner": row["lease_owner"],
        "lease_expires_at": row["lease_expires_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _graph_lab_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "lab_run_id": row["lab_run_id"],
        "session_id": row["session_id"],
        "source_exchange_id": row["source_exchange_id"],
        "settings": json.loads(row["settings_json"]),
        "status": row["status"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }
