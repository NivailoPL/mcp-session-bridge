"""Storage for the Topics lab: map versions, runs, per-chapter results, journal.

These tables are additive (older Bridges ignore them, so the schema stays at
version 2) and nothing here is shown to models. Every run keeps its full input
and every raw provider answer, so each step can be read back later in the
admin, in a JSON export and with ``mcp-bridge topics``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from app.strands import TOKEN_FEATURES_MASTER_KEY
from app.topics import (
    JOURNAL_ACTORS,
    POOL_KEY,
    TOPIC_SETTINGS_KEY,
    TopicError,
    default_topic_settings,
    validate_note,
    validate_topic_map,
    validate_topic_settings,
)

if TYPE_CHECKING:
    from threading import Lock

TOPIC_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS topic_map_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER,
    origin TEXT NOT NULL CHECK (origin IN ('import', 'manual', 'proposal')),
    note TEXT NOT NULL DEFAULT '',
    topics_json TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    version_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'paused', 'completed', 'failed')),
    scope_json TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT,
    metrics_json TEXT,
    total_count INTEGER NOT NULL DEFAULT 0,
    done_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    FOREIGN KEY(version_id) REFERENCES topic_map_versions(version_id)
);

CREATE INDEX IF NOT EXISTS idx_topic_runs_created ON topic_runs(created_at DESC, run_id DESC);

CREATE TABLE IF NOT EXISTS topic_run_items (
    run_id INTEGER NOT NULL,
    strand_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed', 'skipped')),
    chapter_title TEXT NOT NULL,
    request_json TEXT,
    response_json TEXT,
    topic_key TEXT,
    confidence REAL,
    probabilities_json TEXT,
    cost_usd REAL,
    error TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(run_id, strand_id),
    FOREIGN KEY(run_id) REFERENCES topic_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(strand_id) REFERENCES strands(strand_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_topic_run_items_strand ON topic_run_items(strand_id, run_id DESC);

CREATE TABLE IF NOT EXISTS topic_proposals (
    proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    base_version_id INTEGER,
    candidate_version_id INTEGER,
    test_run_id INTEGER,
    op TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('testing', 'pending', 'accepted', 'rejected', 'outdated', 'failed')),
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    test_json TEXT,
    decision_note TEXT,
    created_at INTEGER NOT NULL,
    decided_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_topic_proposals_status ON topic_proposals(status, proposal_id DESC);

CREATE TABLE IF NOT EXISTS topic_journal (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    run_id INTEGER,
    version_id INTEGER,
    message TEXT NOT NULL,
    data_json TEXT
);
"""

# Columns added after the first release of these tables. (table, column, declaration)
TOPIC_LATER_COLUMNS = (
    ("topic_map_versions", "is_candidate", "INTEGER NOT NULL DEFAULT 0"),
    ("topic_runs", "adopted", "INTEGER NOT NULL DEFAULT 0"),
    ("topic_runs", "proposal_id", "INTEGER"),
)

PROPOSAL_OPS = ("first_map", "new_topic", "split", "redescribe", "edit")
PROPOSAL_STATUSES = ("testing", "pending", "accepted", "rejected", "outdated", "failed")


class TopicStoreMixin:
    _lock: Lock

    def _connect(self) -> sqlite3.Connection: ...  # provided by Store
    def get_app_setting(self, key: str) -> str | None: ...
    def set_app_setting(self, key: str, value: str) -> dict[str, Any]: ...

    # ---------- settings ----------

    def get_topic_settings(self) -> dict[str, Any]:
        raw = self.get_app_setting(TOPIC_SETTINGS_KEY)
        if raw is None:
            return default_topic_settings()
        try:
            return validate_topic_settings(json.loads(raw))
        except (ValueError, TypeError):
            return default_topic_settings()

    def set_topic_settings(self, payload: Any) -> dict[str, Any]:
        settings = validate_topic_settings(payload, self.get_topic_settings())
        self.set_app_setting(TOPIC_SETTINGS_KEY, json.dumps(settings))
        return settings

    def topics_enabled(self) -> bool:
        return self.get_app_setting(TOKEN_FEATURES_MASTER_KEY) != "false"

    # ---------- map versions ----------

    def create_topic_version(
        self,
        topics: Any,
        *,
        origin: str,
        actor: str,
        note: Any = None,
        parent_id: int | None = None,
        candidate: bool = False,
    ) -> dict[str, Any]:
        """Store a new map version and make it active, or keep it as an untested candidate."""
        normalized = validate_topic_map(topics)
        note_text = validate_note(note)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            if parent_id is not None and conn.execute(
                "SELECT 1 FROM topic_map_versions WHERE version_id = ?", (parent_id,)
            ).fetchone() is None:
                raise TopicError("Unknown parent map version.", code="version_unknown")
            if not candidate:
                conn.execute("UPDATE topic_map_versions SET is_active = 0 WHERE is_active = 1")
            cursor = conn.execute(
                """
                INSERT INTO topic_map_versions (parent_id, origin, note, topics_json, is_active, is_candidate,
                                                created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (parent_id, origin, note_text, json.dumps(normalized, ensure_ascii=False),
                 0 if candidate else 1, 1 if candidate else 0, actor, now),
            )
            version_id = int(cursor.lastrowid)
            row = conn.execute("SELECT * FROM topic_map_versions WHERE version_id = ?", (version_id,)).fetchone()
        return _version_payload(row)

    def activate_topic_version(self, version_id: int) -> dict[str, Any]:
        """Make a version active; a tested candidate becomes a saved version on the way."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM topic_map_versions WHERE version_id = ?", (version_id,)).fetchone()
            if row is None:
                raise TopicError("Unknown map version.", code="version_unknown")
            conn.execute("UPDATE topic_map_versions SET is_active = 0 WHERE is_active = 1")
            conn.execute(
                "UPDATE topic_map_versions SET is_active = 1, is_candidate = 0 WHERE version_id = ?", (version_id,)
            )
            row = conn.execute("SELECT * FROM topic_map_versions WHERE version_id = ?", (version_id,)).fetchone()
        return _version_payload(row)

    def get_topic_version(self, version_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM topic_map_versions WHERE version_id = ?", (version_id,)).fetchone()
        return _version_payload(row) if row else None

    def active_topic_version(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM topic_map_versions WHERE is_active = 1").fetchone()
        return _version_payload(row) if row else None

    def list_topic_versions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM topic_map_versions WHERE is_candidate = 0 ORDER BY version_id DESC"
            ).fetchall()
        return [_version_payload(row, with_topics=False) for row in rows]

    # ---------- chapters and assignments ----------

    def topic_chapters(self) -> list[dict[str, Any]]:
        """Accepted chapters with what Jev needs and whether their group is sensitive."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.strand_id, s.title, s.session_id, se.title AS session_title,
                       se.updated_at AS session_updated_at, se.group_id,
                       COALESCE(g.name, 'Uncategorized') AS group_name, COALESCE(g.is_sensitive, 0) AS sensitive,
                       CASE WHEN g.group_id IS NOT NULL AND g.is_system = 0 THEN 1 ELSE 0 END AS owner_group,
                       COALESCE(g.color, '#8e93a8') AS group_color, COALESCE(g.icon_key, 'folder') AS group_icon,
                       (SELECT MIN(r.start_exchange_id) FROM strand_ranges r WHERE r.strand_id = s.strand_id) AS first_exchange_id,
                       (SELECT MIN(e.created_at) FROM strand_ranges r JOIN exchanges e ON e.exchange_id = r.start_exchange_id
                        WHERE r.strand_id = s.strand_id) AS first_at
                FROM strands s
                JOIN sessions se ON se.session_id = s.session_id
                LEFT JOIN session_groups g ON g.group_id = se.group_id AND g.deleted_at IS NULL
                WHERE s.status = 'accepted'
                ORDER BY se.created_at, s.session_id, s.strand_id
                """
            ).fetchall()
        return [
            {
                "strand_id": row["strand_id"],
                "title": row["title"],
                "session_id": row["session_id"],
                "session_title": row["session_title"],
                "group_name": row["group_name"],
                "sensitive": bool(row["sensitive"]),
                "group_id": row["group_id"],
                "owner_group": bool(row["owner_group"]),
                "group_color": row["group_color"],
                "group_icon": row["group_icon"],
                "session_updated_at": row["session_updated_at"],
                "first_exchange_id": row["first_exchange_id"],
                "first_at": row["first_at"],
            }
            for row in rows
        ]

    def topic_current_assignments(self, version_id: int) -> dict[int, dict[str, Any]]:
        """Latest finished answer per chapter on one map version."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT i.* FROM topic_run_items i
                JOIN topic_runs r ON r.run_id = i.run_id
                WHERE r.version_id = ? AND (r.kind = 'classify' OR r.adopted = 1) AND i.status = 'done'
                ORDER BY i.run_id
                """,
                (version_id,),
            ).fetchall()
        current: dict[int, dict[str, Any]] = {}
        for row in rows:
            current[row["strand_id"]] = _item_payload(row)
        return current

    def topic_overview_counts(self, version_id: int | None, include_sensitive: bool) -> dict[str, int]:
        chapters = self.topic_chapters()
        current = self.topic_current_assignments(version_id) if version_id else {}
        counts = {"chapters": len(chapters), "classified": 0, "unclassified": 0, "changed": 0, "pool": 0,
                  "sensitive_skipped": 0}
        for chapter in chapters:
            item = current.get(chapter["strand_id"])
            if chapter["sensitive"] and not include_sensitive and item is None:
                counts["sensitive_skipped"] += 1
                continue
            if item is None:
                counts["unclassified"] += 1
                continue
            counts["classified"] += 1
            if item["chapter_title"] != chapter["title"]:
                counts["changed"] += 1
            if item["topic_key"] == POOL_KEY:
                counts["pool"] += 1
        return counts

    # ---------- runs ----------

    def create_topic_run(
        self,
        *,
        kind: str,
        version_id: int | None,
        scope: dict[str, Any],
        settings: dict[str, Any],
        chapters: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        input_data: dict[str, Any],
        actor: str,
        proposal_id: int | None = None,
        exclusive: bool = True,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            if version_id is None:
                # Find topics on an empty map has no version yet; 0 marks "no map".
                conn.execute("PRAGMA foreign_keys=OFF")
                version_id = 0
            if exclusive and conn.execute("SELECT 1 FROM topic_runs WHERE status IN ('queued', 'running')").fetchone():
                raise TopicError("Another topics run is in progress.", code="run_active")
            cursor = conn.execute(
                """
                INSERT INTO topic_runs (kind, version_id, status, scope_json, settings_json, input_json,
                                        total_count, skipped_count, created_by, created_at, proposal_id)
                VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (kind, version_id, json.dumps(scope), json.dumps(settings), json.dumps(input_data, ensure_ascii=False),
                 len(chapters), len(skipped), actor, now, proposal_id),
            )
            run_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO topic_run_items (run_id, strand_id, status, chapter_title, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(run_id, c["strand_id"], "pending", c["title"], now) for c in chapters]
                + [(run_id, c["strand_id"], "skipped", c["title"], now) for c in skipped],
            )
            row = conn.execute("SELECT * FROM topic_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_payload(row)

    def set_topic_run_status(
        self,
        run_id: int,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM topic_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise TopicError("Unknown topics run.", code="run_unknown")
            started = row["started_at"] or (now if status == "running" else None)
            finished = now if status in ("paused", "completed", "failed") else None
            conn.execute(
                """
                UPDATE topic_runs SET status = ?, started_at = ?, finished_at = ?, error_code = ?, error_message = ?,
                       metrics_json = COALESCE(?, metrics_json)
                WHERE run_id = ?
                """,
                (status, started, finished, error_code, error_message,
                 json.dumps(metrics, ensure_ascii=False) if metrics is not None else None, run_id),
            )
            row = conn.execute("SELECT * FROM topic_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_payload(row)

    def record_topic_item(
        self,
        run_id: int,
        strand_id: int,
        *,
        request: dict[str, Any],
        response: Any,
        topic_key: str | None = None,
        confidence: float | None = None,
        probabilities: dict[str, float] | None = None,
        cost_usd: float | None = None,
        error: str | None = None,
    ) -> None:
        status = "failed" if error else "done"
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE topic_run_items SET status = ?, request_json = ?, response_json = ?, topic_key = ?,
                       confidence = ?, probabilities_json = ?, cost_usd = ?, error = ?, updated_at = ?
                WHERE run_id = ? AND strand_id = ?
                """,
                (status, json.dumps(request, ensure_ascii=False),
                 json.dumps(response, ensure_ascii=False) if response is not None else None,
                 topic_key, confidence, json.dumps(probabilities) if probabilities is not None else None,
                 cost_usd, error, now, run_id, strand_id),
            )
            conn.execute(
                """
                UPDATE topic_runs SET
                    done_count = (SELECT COUNT(*) FROM topic_run_items WHERE run_id = ? AND status = 'done'),
                    failed_count = (SELECT COUNT(*) FROM topic_run_items WHERE run_id = ? AND status = 'failed'),
                    cost_usd = (SELECT COALESCE(SUM(cost_usd), 0) FROM topic_run_items WHERE run_id = ?)
                WHERE run_id = ?
                """,
                (run_id, run_id, run_id, run_id),
            )

    def reset_failed_topic_items(self, run_id: int) -> int:
        """Put failed chapters back in line before a resume."""
        with self._lock, self._connect() as conn:
            changed = conn.execute(
                "UPDATE topic_run_items SET status = 'pending', error = NULL WHERE run_id = ? AND status = 'failed'",
                (run_id,),
            ).rowcount
            conn.execute(
                """
                UPDATE topic_runs SET failed_count = 0 WHERE run_id = ?
                """,
                (run_id,),
            )
        return changed

    def pending_topic_items(self, run_id: int) -> list[int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT strand_id FROM topic_run_items WHERE run_id = ? AND status = 'pending' ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [row["strand_id"] for row in rows]

    def topic_run_items(self, run_id: int) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM topic_run_items WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [_item_payload(row, raw=True) for row in rows]

    def get_topic_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM topic_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_payload(row) if row else None

    def set_topic_run_output(self, run_id: int, output: Any) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE topic_runs SET output_json = ? WHERE run_id = ?",
                (json.dumps(output, ensure_ascii=False), run_id),
            )

    def adopt_topic_run(self, run_id: int) -> None:
        """A test run whose candidate was accepted becomes the classification of that version."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE topic_runs SET adopted = 1 WHERE run_id = ?", (run_id,))

    def get_topic_run_output(self, run_id: int) -> Any:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT output_json FROM topic_runs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["output_json"]) if row and row["output_json"] else None

    def topic_run_assignments(self, run_id: int) -> dict[int, dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM topic_run_items WHERE run_id = ? AND status = 'done'", (run_id,)
            ).fetchall()
        return {row["strand_id"]: _item_payload(row) for row in rows}

    def last_topic_run(self, kind: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM topic_runs WHERE kind = ? AND status = 'completed' ORDER BY run_id DESC LIMIT 1", (kind,)
            ).fetchone()
        return _run_payload(row) if row else None

    def latest_proposing_run(self) -> dict[str, Any] | None:
        """The newest Find or Refine run in any state, so a failure is not silent."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM topic_runs WHERE kind IN ('find', 'refine') ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
        return _run_payload(row) if row else None

    # ---------- proposals ----------

    def create_topic_proposal(
        self,
        *,
        run_id: int | None,
        base_version_id: int | None,
        op: str,
        source: str,
        summary: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if op not in PROPOSAL_OPS:
            raise ValueError(f"Unknown proposal op {op}")
        now = int(time.time())
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO topic_proposals (run_id, base_version_id, op, source, status, summary, payload_json, created_at)
                VALUES (?, ?, ?, ?, 'testing', ?, ?, ?)
                """,
                (run_id, base_version_id, op, source, summary[:300], json.dumps(payload, ensure_ascii=False), now),
            )
            row = conn.execute("SELECT * FROM topic_proposals WHERE proposal_id = ?", (cursor.lastrowid,)).fetchone()
        return _proposal_payload(row)

    def update_topic_proposal(self, proposal_id: int, **fields: Any) -> dict[str, Any]:
        allowed = {"status", "candidate_version_id", "test_run_id", "test", "payload", "decision_note", "decided_at",
                   "summary", "base_version_id"}
        if not set(fields) <= allowed:
            raise ValueError(f"Unknown proposal fields: {set(fields) - allowed}")
        columns = {
            "test": ("test_json", lambda v: json.dumps(v, ensure_ascii=False) if v is not None else None),
            "payload": ("payload_json", lambda v: json.dumps(v, ensure_ascii=False)),
        }
        sets, params = [], []
        for key, value in fields.items():
            column, convert = columns.get(key, (key, lambda v: v))
            sets.append(f"{column} = ?")
            params.append(convert(value))
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE topic_proposals SET {', '.join(sets)} WHERE proposal_id = ?", (*params, proposal_id))
            row = conn.execute("SELECT * FROM topic_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise TopicError("Unknown proposal.", code="proposal_unknown")
        return _proposal_payload(row)

    def get_topic_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM topic_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        return _proposal_payload(row) if row else None

    def list_topic_proposals(self, statuses: tuple[str, ...] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if statuses:
                marks = ",".join("?" for _ in statuses)
                rows = conn.execute(
                    f"SELECT * FROM topic_proposals WHERE status IN ({marks}) ORDER BY proposal_id DESC LIMIT ?",
                    (*statuses, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM topic_proposals ORDER BY proposal_id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [_proposal_payload(row) for row in rows]

    def outdate_topic_proposals(self, active_version_id: int | None) -> int:
        """Proposals built on another map version can no longer be accepted as they are."""
        with self._lock, self._connect() as conn:
            return conn.execute(
                """
                UPDATE topic_proposals SET status = 'outdated'
                WHERE status IN ('pending', 'testing') AND COALESCE(base_version_id, -1) != COALESCE(?, -1)
                """,
                (active_version_id,),
            ).rowcount

    def rejected_topic_proposals(self, limit: int = 30) -> list[dict[str, Any]]:
        return [
            {"summary": item["summary"], "note": item["decision_note"]}
            for item in self.list_topic_proposals(("rejected",), limit=limit)
        ]

    def get_topic_run_input(self, run_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT input_json FROM topic_runs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["input_json"]) if row else None

    def list_topic_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM topic_runs ORDER BY run_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_run_payload(row) for row in rows]

    def active_topic_run(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM topic_runs
                WHERE status IN ('queued', 'running') OR (status = 'paused' AND kind = 'classify')
                ORDER BY run_id DESC LIMIT 1
                """
            ).fetchone()
        return _run_payload(row) if row else None

    def recover_topic_runs(self) -> list[int]:
        """Runs cut off by a restart become paused; their finished chapters stay."""
        now = int(time.time())
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT run_id FROM topic_runs WHERE status IN ('queued', 'running')").fetchall()
            conn.execute(
                """
                UPDATE topic_runs SET status = 'paused', finished_at = ?, error_code = 'interrupted',
                       error_message = 'Bridge restarted during the run. Resume to finish it.'
                WHERE status IN ('queued', 'running') AND kind = 'classify'
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE topic_runs SET status = 'failed', finished_at = ?, error_code = 'interrupted',
                       error_message = 'Bridge restarted during the run. Start it again.'
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )
        return [row["run_id"] for row in rows]

    def average_topic_cost(self) -> float | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT AVG(cost_usd) AS cost FROM (
                    SELECT cost_usd FROM topic_run_items WHERE status = 'done' AND cost_usd IS NOT NULL
                    ORDER BY run_id DESC LIMIT 200
                )
                """
            ).fetchone()
        return float(row["cost"]) if row and row["cost"] is not None else None

    # ---------- journal ----------

    def add_topic_journal(
        self,
        actor: str,
        action: str,
        message: str,
        *,
        run_id: int | None = None,
        version_id: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if actor not in JOURNAL_ACTORS:
            raise ValueError(f"Unknown journal actor {actor}")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO topic_journal (created_at, actor, action, run_id, version_id, message, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (int(time.time()), actor, action, run_id, version_id, message,
                 json.dumps(data, ensure_ascii=False) if data is not None else None),
            )

    def list_topic_journal(self, *, limit: int = 200, since: int | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM topic_journal WHERE (? IS NULL OR created_at >= ?)
                ORDER BY event_id DESC LIMIT ?
                """,
                (since, since, limit),
            ).fetchall()
        return [_journal_payload(row) for row in rows]


def _version_payload(row: sqlite3.Row, *, with_topics: bool = True) -> dict[str, Any]:
    topics = json.loads(row["topics_json"])
    payload = {
        "version_id": row["version_id"],
        "parent_id": row["parent_id"],
        "origin": row["origin"],
        "note": row["note"],
        "is_active": bool(row["is_active"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "topic_count": sum(1 for topic in topics if topic["key"] != POOL_KEY),
        "is_candidate": bool(row["is_candidate"]) if "is_candidate" in row.keys() else False,
    }
    if with_topics:
        payload["topics"] = topics
    return payload


def _run_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "kind": row["kind"],
        "version_id": row["version_id"],
        "status": row["status"],
        "scope": json.loads(row["scope_json"]),
        "settings": json.loads(row["settings_json"]),
        "metrics": json.loads(row["metrics_json"]) if row["metrics_json"] else None,
        "total_count": row["total_count"],
        "done_count": row["done_count"],
        "failed_count": row["failed_count"],
        "skipped_count": row["skipped_count"],
        "cost_usd": round(float(row["cost_usd"] or 0), 6),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "adopted": bool(row["adopted"]) if "adopted" in row.keys() else False,
        "proposal_id": row["proposal_id"] if "proposal_id" in row.keys() else None,
    }


def _item_payload(row: sqlite3.Row, *, raw: bool = False) -> dict[str, Any]:
    payload = {
        "run_id": row["run_id"],
        "strand_id": row["strand_id"],
        "status": row["status"],
        "chapter_title": row["chapter_title"],
        "topic_key": row["topic_key"],
        "confidence": row["confidence"],
        "probabilities": json.loads(row["probabilities_json"]) if row["probabilities_json"] else {},
        "cost_usd": row["cost_usd"],
        "error": row["error"],
    }
    if raw:
        payload["request"] = json.loads(row["request_json"]) if row["request_json"] else None
        payload["response"] = json.loads(row["response_json"]) if row["response_json"] else None
    return payload


def _proposal_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "proposal_id": row["proposal_id"],
        "run_id": row["run_id"],
        "base_version_id": row["base_version_id"],
        "candidate_version_id": row["candidate_version_id"],
        "test_run_id": row["test_run_id"],
        "op": row["op"],
        "source": row["source"],
        "status": row["status"],
        "summary": row["summary"],
        "payload": json.loads(row["payload_json"]),
        "test": json.loads(row["test_json"]) if row["test_json"] else None,
        "decision_note": row["decision_note"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


def _journal_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "created_at": row["created_at"],
        "actor": row["actor"],
        "action": row["action"],
        "run_id": row["run_id"],
        "version_id": row["version_id"],
        "message": row["message"],
        "data": json.loads(row["data_json"]) if row["data_json"] else None,
    }
