from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class SavedSummary:
    session_id: str
    title: str
    model_name: str
    path: str
    file_path: str
    chars: int
    sha256: str
    created_at: int


class SessionSummaryStore:
    def __init__(self, root: Path):
        self.root = root
        self._lock = Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def save_summary(
        self,
        session_id: str,
        model_name: str,
        summary_markdown: str,
        title: str = "",
    ) -> SavedSummary:
        content = summary_markdown.strip()
        if not content:
            raise ValueError("summary_markdown must not be empty")

        resolved_model = model_name.strip() or "Unknown model"
        resolved_title = title.strip() or f"Podsumowanie sesji - {resolved_model}"
        created_at = int(time.time())

        with self._lock:
            session_dir = self._session_dir(session_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            filename = self._summary_filename(session_dir, created_at, resolved_title)
            file_path = session_dir / filename
            _write_text_atomic(file_path, content + "\n")

            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            record = {
                "session_id": session_id,
                "title": resolved_title,
                "model_name": resolved_model,
                "path": f"{session_id}/{filename}",
                "file_path": str(file_path),
                "chars": len(content),
                "sha256": digest,
                "created_at": created_at,
            }
            index = self._read_index(session_dir)
            index = [entry for entry in index if entry.get("path") != record["path"]]
            index.append(record)
            _write_json_atomic(self._index_path(session_dir), index)

        return SavedSummary(**record)

    def list_summaries(self, session_id: str) -> list[dict[str, Any]]:
        session_dir = self._session_dir(session_id)
        if not session_dir.is_dir():
            return []

        indexed = {entry.get("path"): entry for entry in self._read_index(session_dir) if isinstance(entry, dict)}
        summaries: list[dict[str, Any]] = []
        for file_path in sorted(session_dir.glob("*.md")):
            rel_path = f"{session_id}/{file_path.name}"
            content = file_path.read_text(encoding="utf-8").strip()
            record = indexed.get(rel_path, {})
            created_at = int(record.get("created_at") or file_path.stat().st_mtime)
            title = str(record.get("title") or _title_from_markdown(content) or file_path.stem)
            model_name = str(record.get("model_name") or "")
            summaries.append(
                {
                    "session_id": session_id,
                    "title": title,
                    "model_name": model_name,
                    "path": rel_path,
                    "file_path": str(file_path),
                    "chars": len(content),
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "created_at": created_at,
                }
            )

        return sorted(summaries, key=lambda item: (item["created_at"], item["path"]))

    def _session_dir(self, session_id: str) -> Path:
        if not _is_safe_session_id(session_id):
            raise ValueError("session_id may only contain letters, numbers, dots, dashes, and underscores")
        return (self.root / session_id).resolve()

    def _summary_filename(self, session_dir: Path, created_at: int, title: str) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(created_at))
        slug = _slugify(title)
        filename = f"{stamp}-podsumowanie-sesji-{slug}.md"
        suffix = 2
        while (session_dir / filename).exists():
            filename = f"{stamp}-podsumowanie-sesji-{slug}-{suffix}.md"
            suffix += 1
        return filename

    def _read_index(self, session_dir: Path) -> list[dict[str, Any]]:
        path = self._index_path(session_dir)
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [entry for entry in data if isinstance(entry, dict)]

    @staticmethod
    def _index_path(session_dir: Path) -> Path:
        return session_dir / "summary-index.json"


def _write_text_atomic(path: Path, content: str) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(content, encoding="utf-8")
    temporary_path.replace(path)


def _write_json_atomic(path: Path, payload: Any) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def _title_from_markdown(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48]
    return slug.strip("-") or "summary"


def _is_safe_session_id(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char in {"-", "_", "."} for char in value)
