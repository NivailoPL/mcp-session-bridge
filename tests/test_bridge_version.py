from pathlib import Path

from bridge_cli.version import _development_label


def test_development_label_is_used_until_target_becomes_package_version(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        """
[project]
version = "0.5.0"

[tool.mcp-session-bridge.development]
target-version = "0.5.1"
label = "0.5.1-beta"
""".strip(),
        encoding="utf-8",
    )

    assert _development_label(project) == "0.5.1-beta"

    project.write_text(
        project.read_text(encoding="utf-8").replace('version = "0.5.0"', 'version = "0.5.1"'),
        encoding="utf-8",
    )

    assert _development_label(project) is None
