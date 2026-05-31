from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any


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
class SessionRecord:
    session_id: str
    title: str
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

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
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

                CREATE INDEX IF NOT EXISTS idx_exchanges_session_created
                    ON exchanges(session_id, created_at, exchange_id);

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
            self._ensure_column(conn, "exchanges", "assistant_created_at", "INTEGER")
            self._ensure_column(conn, "exchanges", "deleted_at", "INTEGER")
            self._ensure_column(conn, "exchanges", "deleted_reason", "TEXT")
            self._ensure_column(conn, "exchanges", "edited_at", "INTEGER")
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

    def create_session(
        self,
        session_id: str,
        title: str,
        context_pack_id: str,
        context_pack_version: str | None = None,
        title_is_auto: bool = False,
    ) -> SessionRecord:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, title, context_pack_id, context_pack_version,
                    title_is_auto, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, title, context_pack_id, context_pack_version, int(title_is_auto), now, now),
            )
        return SessionRecord(
            session_id=session_id,
            title=title,
            context_pack_id=context_pack_id,
            context_pack_version=context_pack_version,
            title_is_auto=title_is_auto,
            created_at=now,
            updated_at=now,
        )

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return None
        return _session_from_row(row)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.session_id,
                    s.title,
                    s.context_pack_id,
                    s.context_pack_version,
                    s.title_is_auto,
                    s.created_at,
                    s.updated_at,
                    SUM(CASE WHEN e.exchange_id IS NOT NULL AND e.deleted_at IS NULL THEN 1 ELSE 0 END) AS exchange_count,
                    SUM(CASE WHEN e.deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted_exchange_count
                FROM sessions s
                LEFT JOIN exchanges e ON e.session_id = s.session_id
                GROUP BY s.session_id
                ORDER BY s.updated_at DESC, s.created_at DESC
                """
            ).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "title": row["title"],
                "context_pack_id": row["context_pack_id"],
                "context_pack_version": row["context_pack_version"],
                "title_is_auto": bool(row["title_is_auto"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
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
        context_pack_id=row["context_pack_id"],
        context_pack_version=row["context_pack_version"],
        title_is_auto=bool(row["title_is_auto"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


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
        return "Sesja bez tytulu"
    if len(text) <= 72:
        return text.rstrip(".!?")
    return text[:72].rsplit(" ", 1)[0].rstrip(".,;:!?") + "..."
