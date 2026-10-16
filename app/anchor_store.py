"""Storage for the admin-only anchor layers: highlights and strands.

These tables are additive (older Bridges ignore them, so the schema stays at
version 2) and nothing here is ever shown to models. ``AnchorStoreMixin`` is
mixed into ``Store`` and uses its connection, lock and session lookups.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from app.highlights import (
    HighlightError,
    clean_quote,
    quote_found,
    quote_hash,
    source_status,
    validate_side,
)
from app.strands import (
    STRAND_SETTINGS_KEY,
    TOKEN_FEATURES_MASTER_KEY,
    TOKEN_FEATURES_STRANDS_KEY,
    StrandError,
    default_strand_settings,
    normalize_ranges,
    validate_strand_settings,
    validate_title,
)

if TYPE_CHECKING:
    from threading import Lock

    from app.storage import ExchangeRecord, SessionRecord

# A run still "running" after this long was cut off by a restart.
STRAND_RUN_STALE_SECONDS = 15 * 60

ANCHOR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS highlights (
    highlight_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    exchange_id INTEGER NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('user', 'model')),
    quote TEXT NOT NULL,
    quote_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(exchange_id, side, quote_hash)
);

CREATE INDEX IF NOT EXISTS idx_highlights_session
    ON highlights(session_id, created_at, highlight_id);

CREATE TABLE IF NOT EXISTS strand_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source_exchange_id INTEGER NOT NULL,
    exchange_count INTEGER NOT NULL,
    settings_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    mode TEXT NOT NULL DEFAULT 'full',
    proposed_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    reverted_at INTEGER,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_strand_runs_session
    ON strand_runs(session_id, created_at DESC, run_id DESC);

CREATE TABLE IF NOT EXISTS strands (
    strand_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('manual', 'codex')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted')),
    run_id INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES strand_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_strands_session
    ON strands(session_id, status, strand_id);

CREATE TABLE IF NOT EXISTS strand_ranges (
    range_id INTEGER PRIMARY KEY AUTOINCREMENT,
    strand_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    start_exchange_id INTEGER NOT NULL,
    end_exchange_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted',
    run_id INTEGER,
    FOREIGN KEY(strand_id) REFERENCES strands(strand_id) ON DELETE CASCADE,
    UNIQUE(strand_id, ordinal)
);
"""

# Columns added after the first release of these tables; Store adds them to
# databases created before them. (table, column, declaration)
ANCHOR_LATER_COLUMNS = (
    ("strand_runs", "mode", "TEXT NOT NULL DEFAULT 'full'"),
    ("strand_ranges", "status", "TEXT NOT NULL DEFAULT 'accepted'"),
    ("strand_ranges", "run_id", "INTEGER"),
)


class AnchorStoreMixin:
    _lock: Lock

    def _connect(self) -> sqlite3.Connection: ...  # provided by Store
    def get_session(self, session_id: str) -> SessionRecord | None: ...
    def get_exchange(self, exchange_id: int, include_deleted: bool = True) -> ExchangeRecord | None: ...
    def list_exchanges(self, session_id: str, include_deleted: bool = False) -> list[ExchangeRecord]: ...
    def get_app_setting(self, key: str) -> str | None: ...
    def set_app_setting(self, key: str, value: str) -> dict[str, Any]: ...

    # ---------- highlights ----------

    def create_highlight(self, session_id: str, exchange_id: Any, side: Any, quote: Any) -> dict[str, Any]:
        side = validate_side(side)
        cleaned = clean_quote(quote)
        if isinstance(exchange_id, bool) or not isinstance(exchange_id, int):
            raise HighlightError("exchange_id must be an integer.", code="exchange_invalid")
        exchange = self.get_exchange(exchange_id, include_deleted=False)
        if exchange is None or exchange.session_id != session_id:
            raise HighlightError("That exchange is not part of this session.", code="exchange_unknown")
        if side == "model" and exchange.assistant_masked_at is not None:
            raise HighlightError("A masked model response cannot be highlighted.", code="source_masked")
        text = exchange.user_message if side == "user" else exchange.assistant_response
        if not quote_found(cleaned, text):
            raise HighlightError(
                "The selected text was not found in this turn. Select text inside a single turn.",
                code="quote_not_found",
            )
        digest = quote_hash(cleaned)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO highlights (session_id, exchange_id, side, quote, quote_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, exchange_id, side, cleaned, digest, now),
            )
            row = conn.execute(
                "SELECT * FROM highlights WHERE exchange_id = ? AND side = ? AND quote_hash = ?",
                (exchange_id, side, digest),
            ).fetchone()
        return self._highlight_payload(row, exchange)

    def list_highlights(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM highlights WHERE session_id = ? ORDER BY created_at DESC, highlight_id DESC",
                (session_id,),
            ).fetchall()
        exchanges = {exchange.exchange_id: exchange for exchange in self.list_exchanges(session_id, include_deleted=True)}
        return [self._highlight_payload(row, exchanges.get(row["exchange_id"])) for row in rows]

    def delete_highlight(self, highlight_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM highlights WHERE highlight_id = ?", (highlight_id,))
        return cursor.rowcount > 0

    @staticmethod
    def _highlight_payload(row: sqlite3.Row, exchange: ExchangeRecord | None) -> dict[str, Any]:
        if exchange is None:
            status = source_status(row["quote"], side=row["side"], exists=False)
        else:
            text = exchange.user_message if row["side"] == "user" else exchange.assistant_response
            status = source_status(
                row["quote"],
                side=row["side"],
                exists=True,
                deleted=exchange.deleted_at is not None,
                masked=exchange.assistant_masked_at is not None,
                source_text=text,
            )
        return {
            "highlight_id": row["highlight_id"],
            "session_id": row["session_id"],
            "exchange_id": row["exchange_id"],
            "side": row["side"],
            "quote": row["quote"],
            "model_name": exchange.model_name if exchange is not None else None,
            "status": status,
            "created_at": row["created_at"],
        }

    # ---------- strands ----------

    def strand_positions(self, session_id: str) -> dict[int, int]:
        """Eligible exchange_id -> transcript position for range validation."""
        return {exchange.exchange_id: index for index, exchange in enumerate(self.list_exchanges(session_id))}

    def list_strands(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            strands = conn.execute(
                "SELECT * FROM strands WHERE session_id = ? ORDER BY strand_id",
                (session_id,),
            ).fetchall()
            ranges = conn.execute(
                """
                SELECT r.* FROM strand_ranges r
                JOIN strands s ON s.strand_id = r.strand_id
                WHERE s.session_id = ?
                ORDER BY r.strand_id, r.ordinal
                """,
                (session_id,),
            ).fetchall()
        by_strand: dict[int, list[dict[str, int]]] = {}
        for item in ranges:
            by_strand.setdefault(item["strand_id"], []).append(
                {
                    "start_exchange_id": item["start_exchange_id"],
                    "end_exchange_id": item["end_exchange_id"],
                    "status": item["status"],
                    "run_id": item["run_id"],
                }
            )
        positions = self.strand_positions(session_id)
        payload = [
            {
                "strand_id": row["strand_id"],
                "session_id": row["session_id"],
                "title": row["title"],
                "origin": row["origin"],
                "status": row["status"],
                "run_id": row["run_id"],
                "ranges": [item for item in by_strand.get(row["strand_id"], []) if item["status"] == "accepted"],
                "pending_ranges": [item for item in by_strand.get(row["strand_id"], []) if item["status"] == "pending"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in strands
        ]
        payload.sort(key=lambda item: _first_position(item, positions))
        return payload

    def get_strand(self, strand_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT session_id FROM strands WHERE strand_id = ?", (strand_id,)).fetchone()
        if row is None:
            return None
        return next((item for item in self.list_strands(row["session_id"]) if item["strand_id"] == strand_id), None)

    def create_strand(self, session_id: str, title: Any, ranges: Any) -> dict[str, Any]:
        if self.get_session(session_id) is None:
            raise StrandError("Unknown session.", code="session_unknown")
        cleaned_title = validate_title(title)
        spans = normalize_ranges(ranges, self.strand_positions(session_id))
        now = int(time.time())
        with self._lock, self._connect() as conn:
            strand_id = self._insert_strand(conn, session_id, cleaned_title, spans, "manual", "accepted", None, now)
        strand = self.get_strand(strand_id)
        assert strand is not None
        return strand

    def update_strand(self, strand_id: int, *, title: Any = None, ranges: Any = None) -> dict[str, Any]:
        current = self.get_strand(strand_id)
        if current is None:
            raise StrandError("Unknown strand.", code="strand_unknown")
        cleaned_title = validate_title(title) if title is not None else current["title"]
        spans = (
            normalize_ranges(ranges, self.strand_positions(current["session_id"])) if ranges is not None else None
        )
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE strands SET title = ?, updated_at = ? WHERE strand_id = ?",
                (cleaned_title, now, strand_id),
            )
            if spans is not None:
                conn.execute("DELETE FROM strand_ranges WHERE strand_id = ? AND status = 'accepted'", (strand_id,))
                self._insert_ranges(conn, strand_id, spans)
        updated = self.get_strand(strand_id)
        assert updated is not None
        return updated

    def delete_strand(self, strand_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM strands WHERE strand_id = ?", (strand_id,))
        return cursor.rowcount > 0

    def accept_strands(self, session_id: str, strand_id: int | None = None) -> int:
        """Accept pending chapters and pending extensions; returns how many items changed."""
        now = int(time.time())
        scope, params = _pending_scope(session_id, strand_id)
        with self._lock, self._connect() as conn:
            extensions = conn.execute(
                f"""
                UPDATE strand_ranges SET status = 'accepted'
                WHERE status = 'pending'
                  AND strand_id IN (SELECT strand_id FROM strands WHERE status = 'accepted' AND {scope})
                """,
                params,
            ).rowcount
            conn.execute(
                f"""
                UPDATE strand_ranges SET status = 'accepted'
                WHERE status = 'pending' AND strand_id IN (SELECT strand_id FROM strands WHERE {scope})
                """,
                params,
            )
            chapters = conn.execute(
                f"UPDATE strands SET status = 'accepted', updated_at = ? WHERE status = 'pending' AND {scope}",
                (now, *params),
            ).rowcount
        return chapters + extensions

    def discard_pending_strands(self, session_id: str, strand_id: int | None = None) -> int:
        """Drop pending chapters and pending extensions; accepted ranges stay."""
        scope, params = _pending_scope(session_id, strand_id)
        with self._lock, self._connect() as conn:
            extensions = conn.execute(
                f"""
                DELETE FROM strand_ranges
                WHERE status = 'pending'
                  AND strand_id IN (SELECT strand_id FROM strands WHERE status = 'accepted' AND {scope})
                """,
                params,
            ).rowcount
            chapters = conn.execute(f"DELETE FROM strands WHERE status = 'pending' AND {scope}", params).rowcount
        return chapters + extensions

    def revert_strand_run(self, run_id: int) -> int:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            run = conn.execute("SELECT run_id FROM strand_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise StrandError("Unknown Codex run.", code="run_unknown")
            removed = conn.execute("DELETE FROM strands WHERE run_id = ?", (run_id,)).rowcount
            removed += conn.execute("DELETE FROM strand_ranges WHERE run_id = ?", (run_id,)).rowcount
            conn.execute("UPDATE strand_runs SET reverted_at = ? WHERE run_id = ?", (now, run_id))
        return removed

    def has_pending_strands(self, session_id: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(_PENDING_EXISTS_SQL, (session_id, session_id)).fetchone()
        return row is not None

    # ---------- chapter state ----------

    def chapter_states(self, session_id: str | None = None) -> dict[str, dict[str, Any]]:
        """How far each session is split into chapters.

        A completed, unreverted Codex run whose proposals are all resolved marks
        the session analyzed up to its last exchange; exchanges added later make
        it "behind". Exchange coverage is reported alongside but never decides
        completeness, because Codex may rightly leave noise out of every chapter.
        """
        where, params = ("WHERE session_id = ?", (session_id,)) if session_id else ("", ())
        with self._lock, self._connect() as conn:
            exchanges = conn.execute(
                f"""
                SELECT session_id, exchange_id FROM exchanges
                {where.replace("WHERE", "WHERE deleted_at IS NULL AND") if where else "WHERE deleted_at IS NULL"}
                ORDER BY session_id, created_at, exchange_id
                """,
                params,
            ).fetchall()
            runs = conn.execute(
                f"SELECT session_id, run_id, status, source_exchange_id, reverted_at FROM strand_runs {where}",
                params,
            ).fetchall()
            pending_runs = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT run_id FROM strands WHERE status = 'pending' AND run_id IS NOT NULL
                    UNION SELECT run_id FROM strand_ranges WHERE status = 'pending' AND run_id IS NOT NULL
                    """
                ).fetchall()
            }
            chapters = conn.execute(
                f"""
                SELECT s.session_id, s.strand_id, s.origin, s.status, r.start_exchange_id, r.end_exchange_id, r.status AS range_status
                FROM strands s LEFT JOIN strand_ranges r ON r.strand_id = s.strand_id
                {where.replace("session_id", "s.session_id")}
                """,
                params,
            ).fetchall()
        order: dict[str, list[int]] = {}
        for row in exchanges:
            order.setdefault(row["session_id"], []).append(row["exchange_id"])
        sessions = set(order) | {row["session_id"] for row in runs} | {row["session_id"] for row in chapters}
        result: dict[str, dict[str, Any]] = {}
        for sid in sessions:
            ids = order.get(sid, [])
            session_runs = [row for row in runs if row["session_id"] == sid]
            session_chapters = [row for row in chapters if row["session_id"] == sid]
            running = any(row["status"] in ("queued", "running") for row in session_runs)
            pending = any(
                row["status"] == "pending" or row["range_status"] == "pending" for row in session_chapters
            )
            resolved = [
                row["source_exchange_id"]
                for row in session_runs
                if row["status"] == "completed" and row["reverted_at"] is None and row["run_id"] not in pending_runs
            ]
            analyzed_to = max(resolved) if resolved else None
            analyzed = sum(1 for exchange_id in ids if analyzed_to is not None and exchange_id <= analyzed_to)
            accepted = {row["strand_id"] for row in session_chapters if row["status"] == "accepted"}
            covered_positions: set[int] = set()
            index = {exchange_id: position for position, exchange_id in enumerate(ids)}
            for row in session_chapters:
                if row["status"] != "accepted" or row["range_status"] != "accepted":
                    continue
                start, end = index.get(row["start_exchange_id"]), index.get(row["end_exchange_id"])
                if start is not None and end is not None:
                    covered_positions.update(range(start, end + 1))
            if running:
                state = "running"
            elif pending:
                state = "review"
            elif analyzed_to is not None:
                state = "complete" if analyzed == len(ids) else "behind"
            elif accepted:
                state = "manual"
            else:
                state = "none"
            if state == "none" and not session_runs:
                continue
            result[sid] = {
                "state": state,
                "exchange_count": len(ids),
                "analyzed_count": analyzed,
                "new_count": len(ids) - analyzed if analyzed_to is not None else 0,
                "covered_count": len(covered_positions),
                "chapter_count": len(accepted),
                "analyzed_to": analyzed_to,
            }
        return result

    def chapter_state(self, session_id: str) -> dict[str, Any]:
        state = self.chapter_states(session_id).get(session_id)
        if state is not None:
            return state
        count = len(self.list_exchanges(session_id))
        return {
            "state": "none", "exchange_count": count, "analyzed_count": 0, "new_count": 0,
            "covered_count": 0, "chapter_count": 0, "analyzed_to": None,
        }

    def codex_runs_with_chapters(self, session_id: str) -> list[int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id FROM strand_runs
                WHERE session_id = ? AND status = 'completed' AND reverted_at IS NULL
                ORDER BY run_id
                """,
                (session_id,),
            ).fetchall()
        return [row["run_id"] for row in rows]

    # ---------- strand runs ----------

    def get_strand_settings(self) -> dict[str, str]:
        raw = self.get_app_setting(STRAND_SETTINGS_KEY)
        if raw is None:
            return default_strand_settings()
        try:
            return validate_strand_settings(json.loads(raw))
        except (ValueError, TypeError):
            return default_strand_settings()

    def set_strand_settings(self, payload: Any) -> dict[str, str]:
        settings = validate_strand_settings(payload)
        self.set_app_setting(STRAND_SETTINGS_KEY, json.dumps(settings, ensure_ascii=False))
        return settings

    def get_token_features(self) -> dict[str, bool]:
        master = self.get_app_setting(TOKEN_FEATURES_MASTER_KEY) != "false"
        strands = self.get_app_setting(TOKEN_FEATURES_STRANDS_KEY) != "false"
        return {"enabled": master, "strands": strands, "strands_active": master and strands}

    def set_token_features(self, payload: Any) -> dict[str, bool]:
        if not isinstance(payload, dict) or not payload or not set(payload) <= {"enabled", "strands"}:
            raise StrandError("Send enabled and/or strands.", code="settings_invalid")
        if not all(isinstance(value, bool) for value in payload.values()):
            raise StrandError("Feature switches must be true or false.", code="settings_invalid")
        if "enabled" in payload:
            self.set_app_setting(TOKEN_FEATURES_MASTER_KEY, "true" if payload["enabled"] else "false")
        if "strands" in payload:
            self.set_app_setting(TOKEN_FEATURES_STRANDS_KEY, "true" if payload["strands"] else "false")
        return self.get_token_features()

    def create_strand_run(
        self,
        session_id: str,
        *,
        source_exchange_id: int,
        exchange_count: int,
        settings: dict[str, str],
        actor: str,
        mode: str = "full",
        now: int | None = None,
    ) -> dict[str, Any]:
        current = int(now if now is not None else time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE strand_runs
                SET status = 'failed', error_code = 'interrupted',
                    error_message = 'The run was interrupted before it finished.', completed_at = ?
                WHERE session_id = ? AND status IN ('queued', 'running') AND created_at < ?
                """,
                (current, session_id, current - STRAND_RUN_STALE_SECONDS),
            )
            active = conn.execute(
                "SELECT 1 FROM strand_runs WHERE session_id = ? AND status IN ('queued', 'running') LIMIT 1",
                (session_id,),
            ).fetchone()
            if active is not None:
                raise StrandError("A Codex run for this session is already in progress.", code="run_active")
            pending = conn.execute(_PENDING_EXISTS_SQL, (session_id, session_id)).fetchone()
            if pending is not None:
                raise StrandError(
                    "Accept or discard the pending proposal before a new Codex run.", code="pending_exists"
                )
            cursor = conn.execute(
                """
                INSERT INTO strand_runs (
                    session_id, source_exchange_id, exchange_count, settings_json, status, mode, created_by, created_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    session_id, source_exchange_id, exchange_count, json.dumps(settings, ensure_ascii=False),
                    mode, actor, current,
                ),
            )
            run_id = int(cursor.lastrowid)
        run = self.get_strand_run(run_id)
        assert run is not None
        return run

    def start_strand_run(self, run_id: int, *, now: int | None = None) -> None:
        current = int(now if now is not None else time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE strand_runs SET status = 'running', started_at = ? WHERE run_id = ? AND status = 'queued'",
                (current, run_id),
            )

    def complete_strand_run(
        self,
        run_id: int,
        proposals: list[dict[str, Any]],
        *,
        extensions: list[dict[str, Any]] | None = None,
        now: int | None = None,
    ) -> None:
        current = int(now if now is not None else time.time())
        with self._lock, self._connect() as conn:
            run = conn.execute(
                "SELECT session_id FROM strand_runs WHERE run_id = ? AND status = 'running'", (run_id,)
            ).fetchone()
            if run is None:
                return
            for proposal in proposals:
                self._insert_strand(
                    conn, run["session_id"], proposal["title"], proposal["ranges"], "codex", "pending", run_id, current
                )
            for extension in extensions or []:
                owner = conn.execute(
                    "SELECT 1 FROM strands WHERE strand_id = ? AND session_id = ? AND status = 'accepted'",
                    (extension["strand_id"], run["session_id"]),
                ).fetchone()
                if owner is None:
                    continue
                start = conn.execute(
                    "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM strand_ranges WHERE strand_id = ?",
                    (extension["strand_id"],),
                ).fetchone()[0]
                self._insert_ranges(
                    conn, extension["strand_id"], extension["ranges"], status="pending", run_id=run_id, start_ordinal=start
                )
            conn.execute(
                """
                UPDATE strand_runs SET status = 'completed', proposed_count = ?, completed_at = ?
                WHERE run_id = ?
                """,
                (len(proposals) + len(extensions or []), current, run_id),
            )

    def fail_strand_run(self, run_id: int, *, error_code: str, error_message: str, now: int | None = None) -> None:
        current = int(now if now is not None else time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE strand_runs SET status = 'failed', error_code = ?, error_message = ?, completed_at = ?
                WHERE run_id = ? AND status IN ('queued', 'running')
                """,
                (error_code, error_message[:500], current, run_id),
            )

    def get_strand_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM strand_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _strand_run_payload(row) if row is not None else None

    def latest_strand_run(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM strand_runs WHERE session_id = ? ORDER BY created_at DESC, run_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return _strand_run_payload(row) if row is not None else None

    # ---------- helpers ----------

    def _insert_strand(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        title: str,
        spans: list[tuple[int, int]],
        origin: str,
        status: str,
        run_id: int | None,
        now: int,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO strands (session_id, title, origin, status, run_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, title, origin, status, run_id, now, now),
        )
        strand_id = int(cursor.lastrowid)
        self._insert_ranges(conn, strand_id, spans)
        return strand_id

    @staticmethod
    def _insert_ranges(
        conn: sqlite3.Connection,
        strand_id: int,
        spans: list[tuple[int, int]],
        *,
        status: str = "accepted",
        run_id: int | None = None,
        start_ordinal: int | None = None,
    ) -> None:
        if start_ordinal is None:
            start_ordinal = conn.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM strand_ranges WHERE strand_id = ?", (strand_id,)
            ).fetchone()[0]
        conn.executemany(
            """
            INSERT INTO strand_ranges (strand_id, ordinal, start_exchange_id, end_exchange_id, status, run_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (strand_id, start_ordinal + index, start, end, status, run_id)
                for index, (start, end) in enumerate(spans)
            ],
        )


def _first_position(strand: dict[str, Any], positions: dict[int, int]) -> tuple[int, int]:
    starts = [positions.get(item["start_exchange_id"], 10**9) for item in strand["ranges"] + strand["pending_ranges"]]
    return (min(starts) if starts else 10**9, strand["strand_id"])


def _strand_run_payload(row: sqlite3.Row) -> dict[str, Any]:
    settings = json.loads(row["settings_json"])
    return {
        "run_id": row["run_id"],
        "session_id": row["session_id"],
        "source_exchange_id": row["source_exchange_id"],
        "exchange_count": row["exchange_count"],
        "model": settings.get("model"),
        "effort": settings.get("effort"),
        "status": row["status"],
        "mode": row["mode"],
        "proposed_count": row["proposed_count"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "reverted_at": row["reverted_at"],
    }


_PENDING_EXISTS_SQL = """
SELECT 1 FROM strands WHERE session_id = ? AND status = 'pending'
UNION ALL
SELECT 1 FROM strand_ranges r JOIN strands s ON s.strand_id = r.strand_id
WHERE s.session_id = ? AND r.status = 'pending'
LIMIT 1
"""


def _pending_scope(session_id: str, strand_id: int | None) -> tuple[str, tuple[Any, ...]]:
    if strand_id is None:
        return "session_id = ?", (session_id,)
    return "session_id = ? AND strand_id = ?", (session_id, strand_id)
