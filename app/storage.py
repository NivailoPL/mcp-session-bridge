from __future__ import annotations

import json
import re
import sqlite3
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from app.time_format import format_timestamp_iso

UNCATEGORIZED_GROUP_ID = "uncategorized"

SYSTEM_SESSION_GROUPS = (
    {
        "group_id": UNCATEGORIZED_GROUP_ID,
        "name": "Uncategorized",
        "color": "#8b93a7",
        "icon_key": "folder",
        "sort_order": 0,
    },
    {
        "group_id": "brainstorming",
        "name": "Brainstorming",
        "color": "#3b82f6",
        "icon_key": "brain",
        "sort_order": 10,
    },
    {
        "group_id": "health",
        "name": "Health",
        "color": "#ef4444",
        "icon_key": "medical_plus",
        "sort_order": 20,
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


class SessionFileConflictError(ValueError):
    """Raised when a guarded file edit targets content that has changed."""


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


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
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

                CREATE TABLE IF NOT EXISTS session_groups (
                    group_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    color TEXT NOT NULL,
                    icon_key TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 100,
                    is_system INTEGER NOT NULL DEFAULT 0,
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
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
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
                """
            )
            self._ensure_column(conn, "refresh_tokens", "resource", "TEXT")
            self._ensure_column(conn, "sessions", "context_pack_version", "TEXT")
            self._ensure_column(conn, "sessions", "title_is_auto", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sessions", "group_id", f"TEXT NOT NULL DEFAULT '{UNCATEGORIZED_GROUP_ID}'")
            self._ensure_column(conn, "exchanges", "assistant_created_at", "INTEGER")
            self._ensure_column(conn, "exchanges", "deleted_at", "INTEGER")
            self._ensure_column(conn, "exchanges", "deleted_reason", "TEXT")
            self._ensure_column(conn, "exchanges", "edited_at", "INTEGER")
            self._seed_system_session_groups(conn)
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
    ) -> SessionGroupRecord:
        resolved_group_id = group_id.strip()
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = _validate_group_name(name)
        if color is not None:
            updates["color"] = _validate_group_color(color)
        if icon_key is not None:
            updates["icon_key"] = _validate_group_icon_key(icon_key)
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
            row = conn.execute("SELECT * FROM session_files WHERE file_id = ?", (cursor.lastrowid,)).fetchone()
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
            row = conn.execute("SELECT * FROM session_files WHERE file_id = ?", (cursor.lastrowid,)).fetchone()
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
                f"""
                SELECT * FROM session_files
                {where}
                ORDER BY created_at DESC, file_id DESC
                """,
                values,
            ).fetchall()
        return [session_file_payload(_session_file_from_row(row), include_content=False) for row in rows]

    def get_session_file(self, file_id: int) -> SessionFileRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM session_files WHERE file_id = ?", (file_id,)).fetchone()
        return _session_file_from_row(row) if row else None

    def update_session_file(
        self,
        file_id: int,
        content: str,
        *,
        expected_sha256: str,
    ) -> SessionFileRecord:
        resolved_content, size_bytes, digest = _validate_file_content(content)
        resolved_expected_sha256 = expected_sha256.strip().lower()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE session_files
                SET content = ?, sha256 = ?, size_bytes = ?
                WHERE file_id = ? AND sha256 = ?
                """,
                (resolved_content, digest, size_bytes, file_id, resolved_expected_sha256),
            )
            if cursor.rowcount == 0:
                exists = conn.execute(
                    "SELECT 1 FROM session_files WHERE file_id = ?",
                    (file_id,),
                ).fetchone()
                if exists is None:
                    raise ValueError(f"Unknown file_id: {file_id}")
                raise SessionFileConflictError(
                    f"File {file_id} changed since it was opened"
                )
            updated = conn.execute(
                "SELECT * FROM session_files WHERE file_id = ?",
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
                "SELECT * FROM session_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown file_id: {file_id}")

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
                "SELECT * FROM session_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
        return _session_file_from_row(updated)

    def delete_session_file(self, file_id: int) -> SessionFileRecord:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM session_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown file_id: {file_id}")
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


def _validate_mime_type(value: str) -> str:
    mime_type = value.strip() or "text/markdown"
    if len(mime_type) > 120 or not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", mime_type):
        raise ValueError("mime_type must look like type/subtype")
    return mime_type.lower()
