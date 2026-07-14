from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, fields
from typing import Any, Iterable, Sequence

import httpx
import sqlite_vec
import tiktoken
from sqlite_vec import serialize_float32

from app.storage import Store

SEARCH_CONFIG_SETTING = "search.config"
OPENAI_KEY_SETTING = "providers.openai.api_key"
COHERE_KEY_SETTING = "providers.cohere.api_key"
RENAME_MODEL_SETTING = "general.rename_model"

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_HIGHLIGHT_OPEN = "\u0002"
_HIGHLIGHT_CLOSE = "\u0003"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TextChunk:
    text: str
    token_count: int
    token_ids: tuple[int, ...]


@dataclass(frozen=True)
class SearchConfig:
    enabled: bool = False
    cohere_rerank_enabled: bool = False
    include_conversations: bool = True
    include_session_files: bool = True
    include_group_files: bool = True
    included_group_ids: tuple[str, ...] = ()
    chunk_size: int = 600
    chunk_overlap: int = 100
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    rebuild_message_threshold: int = 20
    rebuild_max_wait_minutes: int = 15
    bm25_candidates: int = 50
    vector_candidates: int = 50
    result_limit: int = 20
    cohere_model: str = "rerank-v4.0-fast"
    rerank_candidates: int = 50

    def validate(self) -> SearchConfig:
        if not 64 <= self.chunk_size <= 4000:
            raise ValueError("chunk_size must be between 64 and 4000 tokens")
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        if not 1 <= self.embedding_dimensions <= 4096:
            raise ValueError("embedding_dimensions must be between 1 and 4096")
        if not 1 <= self.rebuild_message_threshold <= 10_000:
            raise ValueError("rebuild_message_threshold must be between 1 and 10000")
        if not 1 <= self.rebuild_max_wait_minutes <= 10_080:
            raise ValueError("rebuild_max_wait_minutes must be between 1 and 10080")
        for name in ("bm25_candidates", "vector_candidates", "result_limit", "rerank_candidates"):
            if not 1 <= int(getattr(self, name)) <= 500:
                raise ValueError(f"{name} must be between 1 and 500")
        if not self.embedding_model.strip():
            raise ValueError("embedding_model must not be empty")
        if not self.cohere_model.strip():
            raise ValueError("cohere_model must not be empty")
        if not self.source_kinds():
            raise ValueError("Select at least one search source")
        return self

    def partition_groups(self, group_ids: Iterable[str]) -> tuple[set[str], set[str]]:
        all_groups = set(group_ids)
        approved = all_groups & set(self.included_group_ids)
        return approved, all_groups - approved

    def source_kinds(self) -> tuple[str, ...]:
        kinds: list[str] = []
        if self.include_conversations:
            kinds.append("conversation")
        if self.include_session_files:
            kinds.append("session_file")
        if self.include_group_files:
            kinds.append("group_file")
        return tuple(kinds)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["included_group_ids"] = list(self.included_group_ids)
        return payload

    def index_signature(self) -> str:
        payload = {
            "include_conversations": self.include_conversations,
            "include_session_files": self.include_session_files,
            "include_group_files": self.include_group_files,
            "included_group_ids": sorted(self.included_group_ids),
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> SearchConfig:
        if not value:
            return cls()
        known = {item.name for item in fields(cls)}
        payload = {key: item for key, item in value.items() if key in known}
        bool_fields = {
            "enabled", "cohere_rerank_enabled", "include_conversations",
            "include_session_files", "include_group_files",
        }
        int_fields = {
            "chunk_size", "chunk_overlap", "embedding_dimensions",
            "rebuild_message_threshold", "rebuild_max_wait_minutes", "bm25_candidates",
            "vector_candidates", "result_limit", "rerank_candidates",
        }
        str_fields = {"embedding_model", "cohere_model"}
        for name in bool_fields & payload.keys():
            if not isinstance(payload[name], bool):
                raise TypeError(f"{name} must be a boolean")
        for name in int_fields & payload.keys():
            if not isinstance(payload[name], int) or isinstance(payload[name], bool):
                raise TypeError(f"{name} must be an integer")
        for name in str_fields & payload.keys():
            if not isinstance(payload[name], str):
                raise TypeError(f"{name} must be a string")
        if "included_group_ids" in payload:
            if not isinstance(payload["included_group_ids"], (list, tuple)):
                raise TypeError("included_group_ids must be a list")
            payload["included_group_ids"] = tuple(
                str(group_id) for group_id in payload["included_group_ids"] if str(group_id).strip()
            )
        return cls(**payload).validate()


def fts_query(query: str) -> str:
    tokens = _TOKEN_RE.findall(query)
    return " OR ".join(f'"{token}"' for token in tokens)


def chunk_text(text: str, *, chunk_size: int = 600, overlap: int = 100) -> list[TextChunk]:
    if overlap < 0 or chunk_size < 1 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")
    encoding = tiktoken.get_encoding("cl100k_base")
    token_ids = encoding.encode(text)
    if not token_ids:
        return []
    chunks: list[TextChunk] = []
    step = chunk_size - overlap
    for start in range(0, len(token_ids), step):
        ids = token_ids[start : start + chunk_size]
        if not ids:
            break
        chunks.append(TextChunk(encoding.decode(ids).strip(), len(ids), tuple(ids)))
        if start + chunk_size >= len(token_ids):
            break
    return chunks


def _parse_highlighted(value: str) -> tuple[str, list[dict[str, int]]]:
    text: list[str] = []
    ranges: list[dict[str, int]] = []
    start: int | None = None
    for char in value:
        if char == _HIGHLIGHT_OPEN:
            start = len(text)
        elif char == _HIGHLIGHT_CLOSE:
            if start is not None and len(text) > start:
                ranges.append({"start": start, "end": len(text)})
            start = None
        else:
            text.append(char)
    return "".join(text), ranges


class SearchService:
    def __init__(self, store: Store):
        self.store = store
        self._init_schema()
        with self._connect() as conn:
            conn.execute(
                """UPDATE search_index_state
                   SET status=CASE WHEN active_generation IS NULL THEN 'empty' ELSE 'ready' END,
                       last_error='Previous index build was interrupted by a service restart.',
                       cancel_requested=0
                   WHERE singleton=1 AND status IN ('queued','building')"""
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.store.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS search_documents (
                    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_key TEXT NOT NULL UNIQUE,
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    session_id TEXT,
                    group_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    subtitle TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    source_timestamp INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS search_documents_fts USING fts5(
                    title, content, content='search_documents',
                    content_rowid='document_id', tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TRIGGER IF NOT EXISTS search_documents_ai AFTER INSERT ON search_documents BEGIN
                    INSERT INTO search_documents_fts(rowid, title, content)
                    VALUES (new.document_id, new.title, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS search_documents_ad AFTER DELETE ON search_documents BEGIN
                    INSERT INTO search_documents_fts(search_documents_fts, rowid, title, content)
                    VALUES ('delete', old.document_id, old.title, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS search_documents_au AFTER UPDATE ON search_documents BEGIN
                    INSERT INTO search_documents_fts(search_documents_fts, rowid, title, content)
                    VALUES ('delete', old.document_id, old.title, old.content);
                    INSERT INTO search_documents_fts(rowid, title, content)
                    VALUES (new.document_id, new.title, new.content);
                END;
                CREATE TABLE IF NOT EXISTS search_dirty_documents (
                    document_key TEXT PRIMARY KEY, changed_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS search_source_revision (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    revision INTEGER NOT NULL DEFAULT 0,
                    synced_revision INTEGER NOT NULL DEFAULT -1
                );
                INSERT OR IGNORE INTO search_source_revision(singleton) VALUES (1);
                CREATE TRIGGER IF NOT EXISTS search_source_exchanges_ai
                AFTER INSERT ON exchanges BEGIN
                    UPDATE search_source_revision SET revision=revision+1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS search_source_exchanges_au
                AFTER UPDATE ON exchanges BEGIN
                    UPDATE search_source_revision SET revision=revision+1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS search_source_exchanges_ad
                AFTER DELETE ON exchanges BEGIN
                    UPDATE search_source_revision SET revision=revision+1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS search_source_sessions_ai
                AFTER INSERT ON sessions BEGIN
                    UPDATE search_source_revision SET revision=revision+1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS search_source_sessions_au
                AFTER UPDATE ON sessions BEGIN
                    UPDATE search_source_revision SET revision=revision+1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS search_source_sessions_ad
                AFTER DELETE ON sessions BEGIN
                    UPDATE search_source_revision SET revision=revision+1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS search_source_files_ai
                AFTER INSERT ON session_files BEGIN
                    UPDATE search_source_revision SET revision=revision+1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS search_source_files_au
                AFTER UPDATE ON session_files BEGIN
                    UPDATE search_source_revision SET revision=revision+1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS search_source_files_ad
                AFTER DELETE ON session_files BEGIN
                    UPDATE search_source_revision SET revision=revision+1 WHERE singleton=1;
                END;
                CREATE TABLE IF NOT EXISTS search_vector_chunks (
                    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generation INTEGER NOT NULL, document_key TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL, content TEXT NOT NULL,
                    token_count INTEGER NOT NULL, embedding BLOB NOT NULL,
                    UNIQUE(generation, document_key, chunk_index)
                );
                CREATE INDEX IF NOT EXISTS idx_search_vector_generation
                    ON search_vector_chunks(generation, document_key);
                CREATE TABLE IF NOT EXISTS search_index_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    active_generation INTEGER, status TEXT NOT NULL DEFAULT 'empty',
                    document_count INTEGER NOT NULL DEFAULT 0,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    processed_count INTEGER NOT NULL DEFAULT 0,
                    queued_at INTEGER, started_at INTEGER, completed_at INTEGER,
                    last_error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    index_config_hash TEXT,
                    auto_rebuild_suppressed INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO search_index_state(singleton) VALUES (1);
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(search_index_state)")
            }
            if "index_config_hash" not in columns:
                conn.execute("ALTER TABLE search_index_state ADD COLUMN index_config_hash TEXT")
            if "auto_rebuild_suppressed" not in columns:
                conn.execute(
                    "ALTER TABLE search_index_state ADD COLUMN auto_rebuild_suppressed INTEGER NOT NULL DEFAULT 0"
                )

    def get_config(self) -> SearchConfig:
        raw = self.store.get_app_setting(SEARCH_CONFIG_SETTING)
        if not raw:
            return SearchConfig()
        try:
            return SearchConfig.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            return SearchConfig()

    def set_config(self, config: SearchConfig) -> SearchConfig:
        validated = config.validate()
        previous = self.get_config()
        self.store.set_app_setting(SEARCH_CONFIG_SETTING, json.dumps(validated.to_dict(), separators=(",", ":")))
        if (
            validated.enabled
            and (not previous.enabled or validated.index_signature() != previous.index_signature())
        ):
            with self._connect() as conn:
                conn.execute(
                    """UPDATE search_index_state SET auto_rebuild_suppressed=0,last_error=NULL,
                        status=CASE WHEN status IN ('queued','building') THEN status
                            WHEN active_generation IS NULL THEN 'empty' ELSE 'ready' END
                        WHERE singleton=1"""
                )
        return validated

    def _source_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            exchange_rows = conn.execute(
                """
                SELECT e.exchange_id, e.session_id, s.group_id, s.title, e.model_name,
                       e.user_message, e.assistant_response, e.created_at, e.assistant_created_at
                FROM exchanges e JOIN sessions s ON s.session_id = e.session_id
                WHERE e.deleted_at IS NULL
                """
            ).fetchall()
            file_rows = conn.execute(
                """
                SELECT f.file_id, f.scope_type, f.session_id,
                       CASE WHEN f.scope_type = 'session' THEN s.group_id ELSE f.group_id END AS effective_group_id,
                       f.filename, f.content, f.created_by, f.created_at, s.title AS session_title
                FROM session_files f LEFT JOIN sessions s ON s.session_id = f.session_id
                """
            ).fetchall()
        documents: list[dict[str, Any]] = []
        for row in exchange_rows:
            documents.append({
                "document_key": f"exchange:{row['exchange_id']}",
                "source_kind": "conversation", "source_id": str(row["exchange_id"]),
                "session_id": row["session_id"], "group_id": row["group_id"],
                "title": row["title"], "subtitle": row["model_name"],
                "content": f"User:\n{row['user_message']}\n\nAssistant:\n{row['assistant_response']}",
                "source_timestamp": max(row["created_at"], row["assistant_created_at"]),
            })
        for row in file_rows:
            documents.append({
                "document_key": f"file:{row['file_id']}",
                "source_kind": "session_file" if row["scope_type"] == "session" else "group_file",
                "source_id": str(row["file_id"]), "session_id": row["session_id"],
                "group_id": row["effective_group_id"], "title": row["filename"],
                "subtitle": row["session_title"] or row["created_by"], "content": row["content"],
                "source_timestamp": row["created_at"],
            })
        return documents

    def sync_documents(self) -> dict[str, int]:
        with self._connect() as conn:
            revision = conn.execute(
                "SELECT revision,synced_revision FROM search_source_revision WHERE singleton=1"
            ).fetchone()
            if revision["revision"] == revision["synced_revision"]:
                total = conn.execute("SELECT COUNT(*) FROM search_documents").fetchone()[0]
                return {"changed": 0, "deleted": 0, "total": total}
            source_revision = revision["revision"]
        now = int(time.time())
        sources = self._source_rows()
        active_keys = {item["document_key"] for item in sources}
        changed = deleted = 0
        with self._connect() as conn:
            stored_hashes = {
                row["document_key"]: row["content_hash"]
                for row in conn.execute("SELECT document_key,content_hash FROM search_documents")
            }
            for item in sources:
                digest_input = "\u001f".join(str(item.get(key) or "") for key in (
                    "source_kind", "source_id", "session_id", "group_id", "title", "subtitle", "content"
                ))
                content_hash = hashlib.sha256(digest_input.encode()).hexdigest()
                if stored_hashes.get(item["document_key"]) == content_hash:
                    continue
                conn.execute(
                    """
                    INSERT INTO search_documents(document_key,source_kind,source_id,session_id,group_id,
                        title,subtitle,content,source_timestamp,content_hash,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(document_key) DO UPDATE SET source_kind=excluded.source_kind,
                        source_id=excluded.source_id,session_id=excluded.session_id,group_id=excluded.group_id,
                        title=excluded.title,subtitle=excluded.subtitle,content=excluded.content,
                        source_timestamp=excluded.source_timestamp,content_hash=excluded.content_hash,
                        updated_at=excluded.updated_at
                    """,
                    (item["document_key"],item["source_kind"],item["source_id"],item["session_id"],
                     item["group_id"],item["title"],item["subtitle"],item["content"],
                     item["source_timestamp"],content_hash,now),
                )
                conn.execute(
                    """INSERT INTO search_dirty_documents(document_key,changed_at) VALUES (?,?)
                    ON CONFLICT(document_key) DO UPDATE SET changed_at=excluded.changed_at""",
                    (item["document_key"], now),
                )
                changed += 1
            for document_key in stored_hashes.keys() - active_keys:
                conn.execute("DELETE FROM search_documents WHERE document_key=?", (document_key,))
                conn.execute(
                    """INSERT INTO search_dirty_documents(document_key,changed_at) VALUES (?,?)
                    ON CONFLICT(document_key) DO UPDATE SET changed_at=excluded.changed_at""",
                    (document_key, now),
                )
                deleted += 1
            conn.execute(
                "UPDATE search_source_revision SET synced_revision=? WHERE singleton=1",
                (source_revision,),
            )
        return {"changed": changed, "deleted": deleted, "total": len(sources)}

    def basic_search(self, query: str, *, limit: int = 20,
                     group_ids: Sequence[str] | None = None,
                     source_kinds: Sequence[str] | None = None) -> list[dict[str, Any]]:
        self.sync_documents()
        expression = fts_query(query)
        if not expression:
            return []
        clauses = ["search_documents_fts MATCH ?"]
        values: list[Any] = [expression]
        for column, selected in (("d.group_id", group_ids), ("d.source_kind", source_kinds)):
            if selected is not None:
                if not selected:
                    return []
                clauses.append(f"{column} IN ({','.join('?' for _ in selected)})")
                values.extend(selected)
        values.append(max(1, min(int(limit), 500)))
        sql = f"""
            SELECT d.*, bm25(search_documents_fts,4.0,1.0) AS raw_rank,
                   snippet(search_documents_fts,1,?,?,' … ',48) AS marked_snippet
            FROM search_documents_fts
            JOIN search_documents d ON d.document_id=search_documents_fts.rowid
            WHERE {' AND '.join(clauses)}
            ORDER BY raw_rank ASC,d.source_timestamp DESC LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, [_HIGHLIGHT_OPEN, _HIGHLIGHT_CLOSE, *values]).fetchall()
        results = []
        for row in rows:
            snippet, highlights = _parse_highlighted(row["marked_snippet"])
            results.append({
                "document_key": row["document_key"], "source_kind": row["source_kind"],
                "source_id": row["source_id"], "session_id": row["session_id"],
                "group_id": row["group_id"], "title": row["title"], "subtitle": row["subtitle"],
                "timestamp": row["source_timestamp"], "snippet": snippet, "highlights": highlights,
                "bm25_score": -float(row["raw_rank"]), "pipeline": ["BM25"],
            })
        return results

    def partition_basic_candidates(self, query: str, config: SearchConfig | None = None
                                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        config = (config or self.get_config()).validate()
        candidates = self.basic_search(query, limit=config.bm25_candidates * 2,
                                       source_kinds=config.source_kinds())
        approved_ids = set(config.included_group_ids)
        approved = [item for item in candidates if item["group_id"] in approved_ids]
        local_only = [item for item in candidates if item["group_id"] not in approved_ids]
        for item in local_only:
            item["pipeline"] = ["BM25", "Local only"]
        return approved[:config.bm25_candidates], local_only[:config.result_limit]

    def index_status(self, *, sync: bool = True) -> dict[str, Any]:
        if sync:
            self.sync_documents()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM search_index_state WHERE singleton=1").fetchone()
            dirty = conn.execute("SELECT COUNT(*) FROM search_dirty_documents").fetchone()[0]
            documents = conn.execute("SELECT COUNT(*) FROM search_documents").fetchone()[0]
        payload = dict(row)
        payload["cancel_requested"] = bool(payload["cancel_requested"])
        payload["auto_rebuild_suppressed"] = bool(payload["auto_rebuild_suppressed"])
        payload["dirty_document_count"] = dirty
        payload["source_document_count"] = documents
        config = self.get_config()
        payload["needs_rebuild"] = (
            payload["active_generation"] is None
            or payload["index_config_hash"] != config.index_signature()
        )
        return payload

    def delete_vector_index(self) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("DELETE FROM search_vector_chunks")
            conn.execute("""UPDATE search_index_state SET active_generation=NULL,status='empty',
                document_count=0,chunk_count=0,processed_count=0,queued_at=NULL,started_at=NULL,
                completed_at=NULL,last_error=NULL,cancel_requested=0,index_config_hash=NULL,
                auto_rebuild_suppressed=1 WHERE singleton=1""")
        return self.index_status(sync=False)

    def _vector_connect(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    @staticmethod
    def _provider_post(url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = httpx.post(url, headers=headers, json=payload, timeout=60)
                if response.status_code not in {429, 500, 502, 503, 504}:
                    response.raise_for_status()
                    return response.json()
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * (2 ** attempt)
                time.sleep(min(delay, 8))
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
        if last_error:
            raise RuntimeError(f"Provider request failed: {last_error}") from last_error
        raise RuntimeError(f"Provider request failed with HTTP {response.status_code}: {response.text[:300]}")

    def embed_texts(self, api_key: str, texts: Sequence[str], config: SearchConfig) -> list[list[float]]:
        if not api_key.strip():
            raise ValueError("OpenAI API key is not configured")
        payload: dict[str, Any] = {"model": config.embedding_model, "input": list(texts)}
        if config.embedding_dimensions:
            payload["dimensions"] = config.embedding_dimensions
        data = self._provider_post(
            "https://api.openai.com/v1/embeddings",
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
            payload=payload,
        )
        ordered = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
        if len(ordered) != len(texts):
            raise RuntimeError("OpenAI returned an unexpected number of embeddings")
        embeddings = [list(map(float, item["embedding"])) for item in ordered]
        if any(len(vector) != config.embedding_dimensions for vector in embeddings):
            raise RuntimeError("OpenAI returned embeddings with an unexpected dimension")
        return embeddings

    def rerank(self, api_key: str, query: str, documents: Sequence[str],
               config: SearchConfig) -> list[tuple[int, float]]:
        if not api_key.strip():
            raise ValueError("Cohere API key is not configured")
        data = self._provider_post(
            "https://api.cohere.com/v2/rerank",
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
            payload={
                "model": config.cohere_model,
                "query": query,
                "documents": list(documents),
                "top_n": min(config.result_limit, len(documents)),
            },
        )
        results = [
            (int(item["index"]), float(item["relevance_score"]))
            for item in data.get("results", [])
        ]
        if documents and not results:
            raise RuntimeError("Cohere returned no rerank results")
        if len({index for index, _ in results}) != len(results) or any(
            index < 0 or index >= len(documents) for index, _ in results
        ):
            raise RuntimeError("Cohere returned invalid rerank result indices")
        return results

    def rebuild_index(
        self,
        openai_api_key: str,
        config: SearchConfig | None = None,
        *,
        queued_build: bool = False,
    ) -> dict[str, Any]:
        config = (config or self.get_config()).validate()
        if not config.enabled:
            raise ValueError("RAG search is disabled")
        if not config.included_group_ids:
            raise ValueError("Select at least one group before building the vector index")
        self.sync_documents()
        cancelled_before_start = False
        with self._connect() as conn:
            state = conn.execute("SELECT * FROM search_index_state WHERE singleton=1").fetchone()
            if state["status"] == "building" or (
                state["status"] == "queued" and not queued_build
            ):
                raise RuntimeError("A vector index build is already running")
            if queued_build and state["cancel_requested"]:
                conn.execute(
                    """UPDATE search_index_state SET status=CASE WHEN active_generation IS NULL
                        THEN 'empty' ELSE 'ready' END,cancel_requested=0,
                        last_error='Vector index build was stopped before it started.'
                        WHERE singleton=1"""
                )
                cancelled_before_start = True
            generation = int(state["active_generation"] or 0) + 1
            if cancelled_before_start:
                documents = []
            else:
                source_kinds = config.source_kinds()
                if not source_kinds:
                    raise ValueError("Select at least one source type")
                group_marks = ",".join("?" for _ in config.included_group_ids)
                kind_marks = ",".join("?" for _ in source_kinds)
                documents = conn.execute(
                    f"""SELECT * FROM search_documents
                        WHERE group_id IN ({group_marks}) AND source_kind IN ({kind_marks})
                        ORDER BY document_id""",
                    [*config.included_group_ids, *source_kinds],
                ).fetchall()
                conn.execute(
                    """UPDATE search_index_state SET status='building',queued_at=?,started_at=?,
                        completed_at=NULL,last_error=NULL,cancel_requested=0,processed_count=0,
                        document_count=?,auto_rebuild_suppressed=0 WHERE singleton=1""",
                    (int(time.time()), int(time.time()), len(documents)),
                )
        if cancelled_before_start:
            return self.index_status(sync=False)
        try:
            prepared: list[tuple[str, int, TextChunk]] = []
            for document in documents:
                for index, chunk in enumerate(chunk_text(
                    document["content"], chunk_size=config.chunk_size, overlap=config.chunk_overlap
                )):
                    prepared.append((document["document_key"], index, chunk))
            with self._connect() as conn:
                conn.execute("DELETE FROM search_vector_chunks WHERE generation=?", (generation,))
            processed_documents: set[str] = set()
            for start in range(0, len(prepared), 64):
                with self._connect() as conn:
                    cancelled = conn.execute(
                        "SELECT cancel_requested FROM search_index_state WHERE singleton=1"
                    ).fetchone()[0]
                if cancelled:
                    raise InterruptedError("Vector index build was stopped")
                batch = prepared[start:start + 64]
                embeddings = self.embed_texts(openai_api_key, [item[2].text for item in batch], config)
                with self._connect() as conn:
                    conn.executemany(
                        """INSERT INTO search_vector_chunks(
                            generation,document_key,chunk_index,content,token_count,embedding
                        ) VALUES (?,?,?,?,?,?)""",
                        [
                            (generation, key, index, chunk.text, chunk.token_count, serialize_float32(vector))
                            for (key, index, chunk), vector in zip(batch, embeddings, strict=True)
                        ],
                    )
                    processed_documents.update(item[0] for item in batch)
                    conn.execute(
                        "UPDATE search_index_state SET processed_count=? WHERE singleton=1",
                        (len(processed_documents),),
                    )
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                previous = conn.execute(
                    "SELECT active_generation FROM search_index_state WHERE singleton=1"
                ).fetchone()[0]
                conn.execute(
                    """UPDATE search_index_state SET active_generation=?,status='ready',
                        chunk_count=?,processed_count=?,completed_at=?,last_error=NULL,
                        cancel_requested=0,index_config_hash=? WHERE singleton=1""",
                    (
                        generation, len(prepared), len(documents), int(time.time()),
                        config.index_signature(),
                    ),
                )
                conn.execute("DELETE FROM search_dirty_documents")
                if previous is not None:
                    conn.execute("DELETE FROM search_vector_chunks WHERE generation=?", (previous,))
            return self.index_status(sync=False)
        except InterruptedError as exc:
            with self._connect() as conn:
                conn.execute("DELETE FROM search_vector_chunks WHERE generation=?", (generation,))
                conn.execute(
                    """UPDATE search_index_state SET status=CASE WHEN active_generation IS NULL
                        THEN 'empty' ELSE 'ready' END,last_error=?,cancel_requested=0 WHERE singleton=1""",
                    (str(exc),),
                )
            return self.index_status(sync=False)
        except Exception as exc:
            with self._connect() as conn:
                conn.execute("DELETE FROM search_vector_chunks WHERE generation=?", (generation,))
                conn.execute(
                    """UPDATE search_index_state SET status='failed',last_error=?,
                        cancel_requested=0 WHERE singleton=1""", (str(exc)[:1000],)
                )
            raise

    def start_rebuild(self, openai_api_key: str, config: SearchConfig | None = None) -> dict[str, Any]:
        config = (config or self.get_config()).validate()
        if not openai_api_key.strip():
            raise ValueError("OpenAI API key is not configured")
        if not config.enabled:
            raise ValueError("RAG search is disabled")
        if not config.included_group_ids:
            raise ValueError("Select at least one group before building the vector index")
        with self._connect() as conn:
            status = conn.execute(
                "SELECT status FROM search_index_state WHERE singleton=1"
            ).fetchone()[0]
            if status in {"queued", "building"}:
                return self.index_status(sync=False)
            conn.execute(
                """UPDATE search_index_state SET status='queued',queued_at=?,last_error=NULL,
                    cancel_requested=0,auto_rebuild_suppressed=0 WHERE singleton=1""",
                (int(time.time()),),
            )

        def run() -> None:
            try:
                self.rebuild_index(openai_api_key, config, queued_build=True)
            except Exception:
                logger.exception("Vector index rebuild failed")

        threading.Thread(target=run, name="rag-index-build", daemon=True).start()
        return self.index_status(sync=False)

    def cancel_rebuild(self) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """UPDATE search_index_state SET cancel_requested=1,auto_rebuild_suppressed=1
                   WHERE singleton=1 AND status IN ('queued','building')"""
            )
        return self.index_status(sync=False)

    def maybe_start_rebuild(self, openai_api_key: str) -> dict[str, Any]:
        config = self.get_config()
        status = self.index_status(sync=False)
        if not config.enabled or not openai_api_key:
            return status
        if status["status"] in {"queued", "building", "failed"}:
            return status
        if status["auto_rebuild_suppressed"]:
            return status
        self.sync_documents()
        status = self.index_status(sync=False)
        dirty = status["dirty_document_count"]
        with self._connect() as conn:
            oldest = conn.execute("SELECT MIN(changed_at) FROM search_dirty_documents").fetchone()[0]
        overdue = bool(oldest and time.time() - oldest >= config.rebuild_max_wait_minutes * 60)
        if (
            status["needs_rebuild"]
            or dirty >= config.rebuild_message_threshold
            or overdue
        ):
            return self.start_rebuild(openai_api_key, config)
        return status

    def _vector_candidates(self, query: str, openai_api_key: str,
                           config: SearchConfig) -> list[dict[str, Any]]:
        with self._connect() as conn:
            state = conn.execute(
                """SELECT active_generation,index_config_hash
                   FROM search_index_state WHERE singleton=1"""
            ).fetchone()
        if not state or state["active_generation"] is None:
            raise ValueError("The vector index has not been built yet")
        if state["index_config_hash"] != config.index_signature():
            raise ValueError("The vector index is stale; rebuild it before Hybrid search")
        generation = state["active_generation"]
        query_vector = self.embed_texts(openai_api_key, [query], config)[0]
        marks = ",".join("?" for _ in config.included_group_ids)
        kinds = config.source_kinds()
        kind_marks = ",".join("?" for _ in kinds)
        with self._vector_connect() as conn:
            rows = conn.execute(
                f"""SELECT c.document_key,c.content,
                           vec_distance_cosine(c.embedding,?) AS distance
                    FROM search_vector_chunks c JOIN search_documents d USING(document_key)
                    WHERE c.generation=? AND d.group_id IN ({marks})
                      AND d.source_kind IN ({kind_marks})
                    ORDER BY distance LIMIT ?""",
                [serialize_float32(query_vector), generation,
                 *config.included_group_ids, *kinds, config.vector_candidates],
            ).fetchall()
        best: dict[str, dict[str, Any]] = {}
        for row in rows:
            score = 1.0 - float(row["distance"])
            if row["document_key"] not in best or score > best[row["document_key"]]["vector_score"]:
                best[row["document_key"]] = {
                    "document_key": row["document_key"], "vector_score": score,
                    "vector_snippet": row["content"][:900],
                }
        return list(best.values())

    def hybrid_search(self, query: str, *, openai_api_key: str,
                      cohere_api_key: str = "", config: SearchConfig | None = None
                      ) -> dict[str, Any]:
        config = (config or self.get_config()).validate()
        if not config.enabled:
            raise ValueError("RAG search is disabled")
        approved, local_only = self.partition_basic_candidates(query, config)
        vectors = self._vector_candidates(query, openai_api_key, config)
        fused: dict[str, dict[str, Any]] = {}
        for rank, item in enumerate(approved):
            entry = fused.setdefault(item["document_key"], dict(item))
            entry["hybrid_score"] = entry.get("hybrid_score", 0.0) + 1.0 / (60 + rank + 1)
        vector_map = {item["document_key"]: item for item in vectors}
        if vector_map:
            with self._connect() as conn:
                marks = ",".join("?" for _ in vector_map)
                documents = {
                    row["document_key"]: dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM search_documents WHERE document_key IN ({marks})", list(vector_map)
                    )
                }
            for rank, vector in enumerate(vectors):
                key = vector["document_key"]
                if key not in fused:
                    document = documents[key]
                    fused[key] = {
                        "document_key": key, "source_kind": document["source_kind"],
                        "source_id": document["source_id"], "session_id": document["session_id"],
                        "group_id": document["group_id"], "title": document["title"],
                        "subtitle": document["subtitle"], "timestamp": document["source_timestamp"],
                        "snippet": vector["vector_snippet"], "highlights": [], "pipeline": [],
                    }
                fused[key]["vector_score"] = vector["vector_score"]
                fused[key]["hybrid_score"] = fused[key].get("hybrid_score", 0.0) + 1.0 / (60 + rank + 1)
        ranked = sorted(fused.values(), key=lambda item: item.get("hybrid_score", 0), reverse=True)
        for item in ranked:
            item["pipeline"] = ["Hybrid"]
        warning = None
        if config.cohere_rerank_enabled and ranked:
            if not cohere_api_key:
                warning = "Cohere rerank is enabled but its API key is not configured."
            else:
                try:
                    selection = ranked[:config.rerank_candidates]
                    order = self.rerank(cohere_api_key, query, [item["snippet"] for item in selection], config)
                    ranked = [selection[index] for index, _ in order]
                    for item, (_, score) in zip(ranked, order, strict=True):
                        item["rerank_score"] = score
                        item["pipeline"] = ["Hybrid", "Reranked"]
                except Exception as exc:
                    warning = f"Cohere rerank failed; showing Hybrid results: {exc}"
        return {
            "results": ranked[:config.result_limit],
            "local_only_results": local_only[:config.result_limit],
            "warning": warning,
        }
