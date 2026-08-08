from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Mapping


def update_env_file(path: Path, values: Mapping[str, str]) -> None:
    """Update selected dotenv keys without discarding operator-owned content."""
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    rendered: list[str] = []

    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rendered.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in pending:
            rendered.append(f"{key}={_dotenv_value(pending.pop(key))}")
        else:
            rendered.append(line)

    if rendered and rendered[-1] != "":
        rendered.append("")
    for key, value in pending.items():
        rendered.append(f"{key}={_dotenv_value(value)}")

    payload = "\n".join(rendered).rstrip() + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key.strip()] = _unquote(raw.strip())
    return values


def _dotenv_value(value: str) -> str:
    if not value or any(char.isspace() for char in value) or any(char in value for char in '#"\''):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
