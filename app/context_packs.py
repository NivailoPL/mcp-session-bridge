from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PACK_NOTES = """Ten context pack nie ma dodatkowych notatek.

Główny protokół zachowania modelu powinien być zapisany w system prompcie projektu Claude/ChatGPT."""


@dataclass(frozen=True)
class ContextFile:
    path: str
    title: str
    content: str
    chars: int
    sha256: str


@dataclass(frozen=True)
class ContextPack:
    pack_id: str
    name: str
    description: str
    instructions: str
    files: list[ContextFile]
    root: Path

    @property
    def total_chars(self) -> int:
        return len(self.instructions) + sum(file.chars for file in self.files)

    @property
    def content_hash(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.pack_id.encode("utf-8"))
        digest.update(self.instructions.encode("utf-8"))
        for file in self.files:
            digest.update(file.path.encode("utf-8"))
            digest.update(file.sha256.encode("utf-8"))
        return digest.hexdigest()


class ContextPackStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def list_packs(self) -> list[dict[str, Any]]:
        packs: list[dict[str, Any]] = []
        for pack_dir in sorted(path for path in self.root.iterdir() if path.is_dir()):
            try:
                pack = self.load_pack(pack_dir.name)
            except (FileNotFoundError, ValueError):
                continue
            packs.append(
                {
                    "context_pack_id": pack.pack_id,
                    "name": pack.name,
                    "description": pack.description,
                    "file_count": len(pack.files),
                    "total_chars": pack.total_chars,
                    "sha256": pack.content_hash,
                }
            )
        return packs

    def load_pack(self, pack_id: str) -> ContextPack:
        if not _is_safe_id(pack_id):
            raise ValueError("context_pack_id may only contain letters, numbers, dots, dashes, and underscores")

        pack_dir = (self.root / pack_id).resolve()
        if not _is_relative_to(pack_dir, self.root.resolve()) or not pack_dir.is_dir():
            raise FileNotFoundError(f"Context pack not found: {pack_id}")

        manifest = _read_manifest(pack_dir)
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError(f"Context pack {pack_id} manifest must define a non-empty files list")

        instructions = DEFAULT_PACK_NOTES
        instructions_file = manifest.get("instructions_file")
        if instructions_file:
            instructions = _read_pack_file(pack_dir, str(instructions_file))

        context_files: list[ContextFile] = []
        for entry in files:
            if isinstance(entry, str):
                rel_path = entry
                title = Path(entry).stem
            elif isinstance(entry, dict):
                rel_path = str(entry.get("path", ""))
                title = str(entry.get("title") or Path(rel_path).stem)
            else:
                raise ValueError(f"Invalid file entry in context pack {pack_id}: {entry!r}")

            content = _read_pack_file(pack_dir, rel_path)
            context_files.append(
                ContextFile(
                    path=rel_path,
                    title=title,
                    content=content,
                    chars=len(content),
                    sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )

        return ContextPack(
            pack_id=pack_id,
            name=str(manifest.get("name") or pack_id),
            description=str(manifest.get("description") or ""),
            instructions=instructions,
            files=context_files,
            root=pack_dir,
        )


def _read_manifest(pack_dir: Path) -> dict[str, Any]:
    json_path = pack_dir / "manifest.json"
    yaml_path = pack_dir / "manifest.yml"
    yml_path = pack_dir / "manifest.yaml"

    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
    elif yaml_path.exists():
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    elif yml_path.exists():
        data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
    else:
        raise FileNotFoundError(f"Missing manifest in {pack_dir}")

    if not isinstance(data, dict):
        raise ValueError(f"Manifest must be an object in {pack_dir}")
    return data


def _read_pack_file(pack_dir: Path, rel_path: str) -> str:
    if not rel_path or rel_path.startswith("/"):
        raise ValueError(f"Invalid context file path: {rel_path}")
    path = (pack_dir / rel_path).resolve()
    if not _is_relative_to(path, pack_dir.resolve()):
        raise ValueError(f"Context file path escapes pack directory: {rel_path}")
    if not path.is_file():
        raise FileNotFoundError(f"Context file not found: {rel_path}")
    return path.read_text(encoding="utf-8").strip()


def _is_safe_id(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char in {"-", "_", "."} for char in value)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
