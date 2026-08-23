import tomllib
from pathlib import Path

from bridge_cli.version import BRIDGE_VERSION_LABEL, _development_label


def test_repository_development_version_metadata_matches_status_label() -> None:
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project.open("rb") as handle:
        config = tomllib.load(handle)

    development = config["tool"]["mcp-session-bridge"]["development"]
    assert development["target-version"] == "2026.8.2"
    assert development["label"] == "2026.8.2-beta"
    assert BRIDGE_VERSION_LABEL == development["label"]


def test_development_label_is_used_until_target_becomes_package_version(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        """
[project]
version = "0.5.0"

[tool.mcp-session-bridge.development]
target-version = "2026.8.2"
label = "2026.8.2-beta"
""".strip(),
        encoding="utf-8",
    )

    assert _development_label(project) == "2026.8.2-beta"

    project.write_text(
        project.read_text(encoding="utf-8").replace(
            'version = "0.5.0"', 'version = "2026.8.2"'
        ),
        encoding="utf-8",
    )

    assert _development_label(project) is None
