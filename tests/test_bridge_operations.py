from __future__ import annotations

import json
from pathlib import Path

from app.storage import Store
from bridge_cli.install import ManagedInstaller, SetupAnswers
from bridge_cli.layout import Layout
from bridge_cli.operations import StatusCollector


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, check: bool = True):
        self.calls.append(tuple(args))

        class Result:
            returncode = 0
            stdout = "active\n"
            stderr = ""

        return Result()


def test_layout_is_fully_relocatable_for_safe_setup_tests(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path)

    assert layout.opt_root == tmp_path / "opt/mcp-session-bridge"
    assert layout.env_file == tmp_path / "etc/mcp-session-bridge/bridge.env"
    assert layout.db_path == tmp_path / "var/lib/mcp-session-bridge/bridge.sqlite3"
    assert layout.command_path == tmp_path / "usr/local/bin/bridge"


def test_setup_dry_run_makes_no_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    layout = Layout.for_root(tmp_path / "target")
    installer = ManagedInstaller(layout, source, RecordingRunner())

    result = installer.install(
        SetupAnswers(domain="bridge.example.test", username="owner", password="long-password"),
        dry_run=True,
        activate=False,
    )

    assert result["state"] == "dry_run"
    assert result["steps"]
    assert not layout.opt_root.exists()
    assert not layout.env_file.exists()


def test_setup_creates_managed_layout_without_leaking_password(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (source / "app").mkdir()
    (source / "app/__init__.py").write_text("", encoding="utf-8")
    layout = Layout.for_root(tmp_path / "target")
    installer = ManagedInstaller(layout, source, RecordingRunner())

    result = installer.install(
        SetupAnswers(domain="bridge.example.test", username="admin", password="long-password"),
        activate=False,
    )

    manifest = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    env_text = layout.env_file.read_text(encoding="utf-8")
    assert result["state"] == "complete"
    assert manifest["mode"] == "managed"
    assert manifest["public_base_url"] == "https://bridge.example.test"
    assert layout.current_link.resolve() == layout.release_dir(result["version"]).resolve()
    assert layout.db_path.exists()
    assert "BRIDGE_ALLOW_STARTUP_MIGRATIONS=false" in env_text
    assert "BRIDGE_OWNER_USERNAME=admin" in env_text
    assert "long-password" not in env_text
    assert layout.service_unit.exists()
    assert layout.caddy_fragment.exists()


def test_setup_adopts_legacy_database_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (source / "app").mkdir()
    (source / "app/__init__.py").write_text("", encoding="utf-8")
    legacy_db = source / "data/bridge.sqlite3"
    store = Store(legacy_db)
    store.create_session("legacy-session", "Keep this", "manual-context")
    marker = source / "do-not-delete.txt"
    marker.write_text("preserved", encoding="utf-8")
    layout = Layout.for_root(tmp_path / "target")

    ManagedInstaller(layout, source, RecordingRunner()).install(
        SetupAnswers(domain="bridge.example.test", username="owner", password="long-password"),
        legacy_db=legacy_db,
        activate=False,
    )

    managed = Store(layout.db_path, allow_startup_migrations=False)
    assert managed.get_session("legacy-session") is not None
    assert marker.read_text(encoding="utf-8") == "preserved"
    assert any(layout.backup_root.iterdir())


def test_status_collector_reports_managed_installation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (source / "app").mkdir()
    (source / "app/__init__.py").write_text("", encoding="utf-8")
    layout = Layout.for_root(tmp_path / "target")
    runner = RecordingRunner()
    ManagedInstaller(layout, source, runner).install(
        SetupAnswers(domain="bridge.example.test", username="owner", password="long-password"),
        activate=False,
    )

    report = StatusCollector(layout, runner, probe_http=False).collect()

    assert report.installation["mode"] == "managed"
    assert report.version["current"] == "0.4.0"
    assert {check.id for check in report.checks} >= {"config", "database", "service", "caddy"}
    assert next(check for check in report.checks if check.id == "database").state == "pass"


def test_repeated_setup_preserves_bridge_secret(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (source / "app").mkdir()
    (source / "app/__init__.py").write_text("", encoding="utf-8")
    layout = Layout.for_root(tmp_path / "target")
    installer = ManagedInstaller(layout, source, RecordingRunner())

    installer.install(
        SetupAnswers(domain="bridge.example.test", username="owner", password="first-password"),
        activate=False,
    )
    first_secret = next(
        line for line in layout.env_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("BRIDGE_SECRET_KEY=")
    )
    installer.install(
        SetupAnswers(domain="bridge.example.test", username="owner", password="second-password"),
        activate=False,
    )
    second_secret = next(
        line for line in layout.env_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("BRIDGE_SECRET_KEY=")
    )

    assert second_secret == first_secret
