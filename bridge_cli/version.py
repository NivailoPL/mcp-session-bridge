from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


try:
    BRIDGE_VERSION = version("mcp-session-bridge")
except PackageNotFoundError:
    BRIDGE_VERSION = "2026.8.2"


def _development_label(project_file: Path) -> str | None:
    try:
        with project_file.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    project_version = config.get("project", {}).get("version")
    development = (
        config.get("tool", {}).get("mcp-session-bridge", {}).get("development", {})
    )
    target_version = development.get("target-version")
    label = development.get("label")
    if (
        isinstance(project_version, str)
        and isinstance(target_version, str)
        and target_version != project_version
        and isinstance(label, str)
        and label
    ):
        return label
    return None


BRIDGE_VERSION_LABEL = _development_label(
    Path(__file__).resolve().parents[1] / "pyproject.toml"
)
