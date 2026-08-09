from __future__ import annotations

import json
from argparse import Namespace
import socket
from pathlib import Path

import pytest

from app.storage import Store
from bridge_cli.install import ManagedInstaller, SetupAnswers
from bridge_cli.layout import Layout
from bridge_cli.__main__ import _configure
from bridge_cli.operations import StatusCollector


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, check: bool = True):
        self.calls.append(tuple(args))

        class Result:
            returncode = 0
            stdout = '{"ok": true}\n' if args and args[0] == "curl" else "active\n"
            stderr = ""

        return Result()


class FailFirstRestartRunner(RecordingRunner):
    def __init__(self) -> None:
        super().__init__()
        self.failed_restart = False

    def run(self, *args: str, check: bool = True):
        self.calls.append(tuple(args))

        class Result:
            returncode = 0
            stdout = "active\n"
            stderr = ""

        if args == ("systemctl", "restart", "mcp-session-bridge.service") and not self.failed_restart:
            self.failed_restart = True
            Result.returncode = 1
            Result.stdout = ""
            Result.stderr = "forced restart failure"
        return Result()


def test_layout_is_fully_relocatable_for_safe_setup_tests(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path)

    assert layout.opt_root == tmp_path / "opt/mcp-session-bridge"
    assert layout.env_file == tmp_path / "etc/mcp-session-bridge/bridge.env"
    assert layout.db_path == tmp_path / "var/lib/mcp-session-bridge/bridge.sqlite3"
    assert layout.command_path == tmp_path / "usr/local/bin/mcp-bridge"


@pytest.mark.parametrize(
    ("mode", "state", "message"),
    [
        ("managed", "pass", "active"),
        ("prepared", "action_required", "prepared but not active"),
        ("unmanaged", "action_required", "not managed"),
    ],
)
def test_status_reports_installation_lifecycle(
    tmp_path: Path, mode: str, state: str, message: str
) -> None:
    check = StatusCollector(
        Layout.for_root(tmp_path), RecordingRunner(), probe_http=False
    )._installation_check(mode)

    assert check.state == state
    assert message in check.message


def test_setup_dry_run_makes_no_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    layout = Layout.for_root(tmp_path / "target")
    installer = ManagedInstaller(layout, source, RecordingRunner())
    events: list[tuple[int, int, str, str]] = []

    result = installer.install(
        SetupAnswers(domain="bridge.example.test", username="owner", password="long-password"),
        progress=lambda *event: events.append(event),
        dry_run=True,
        activate=False,
    )

    assert events[0][3] == "RUNNING"
    assert events[1][3] == "PASS"
    assert [event[3] for event in events[2:]] == ["PLANNED"] * 5
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
    env_text = layout.pending_env_file.read_text(encoding="utf-8")
    assert result["state"] == "prepared"
    assert manifest["mode"] == "prepared"
    assert manifest["public_base_url"] == "https://bridge.example.test"
    setup_state = json.loads(layout.setup_file.read_text(encoding="utf-8"))
    assert not layout.current_link.exists()
    assert Path(setup_state["release_dir"]).resolve() == layout.release_dir(result["version"]).resolve()
    assert layout.db_path.exists()
    assert "BRIDGE_ALLOW_STARTUP_MIGRATIONS=false" in env_text
    assert "BRIDGE_OWNER_USERNAME=admin" in env_text
    assert "long-password" not in env_text
    assert not layout.service_unit.exists()
    assert not layout.caddy_fragment.exists()
    assert layout.pending_service_unit.exists()
    assert layout.pending_caddy_fragment.exists()
    status_unit = layout.pending_status_service_unit.read_text(encoding="utf-8")
    assert f"ExecStart={layout.command_path} status --refresh --write-snapshot" in status_unit
    assert "{self.layout.command_path}" not in status_unit


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
    assert not any(layout.backup_root.iterdir())


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

    assert report.installation["mode"] == "prepared"
    assert report.version["current"] == "0.4.0"
    assert {check.id for check in report.checks} >= {"installation", "config", "database", "service", "caddy"}
    installation = next(check for check in report.checks if check.id == "installation")
    assert installation.state == "action_required"
    assert "Activate" in (installation.remediation or "")
    assert next(check for check in report.checks if check.id == "database").state == "pass"


def test_status_collector_reports_deployed_checkout_revision(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path)
    layout.state_root.mkdir(parents=True)
    layout.installation_file.write_text(
        json.dumps(
            {
                "mode": "managed",
                "version": "0.4.0",
                "commit": "a" * 40,
                "release_id": "0.4.0-git-aaaaaaaaaaaa",
                "source_root": "/srv/bridge",
            }
        ),
        encoding="utf-8",
    )

    report = StatusCollector(layout, RecordingRunner(), probe_http=False).collect()

    assert report.version["commit"] == "a" * 40
    assert report.version["release_id"] == "0.4.0-git-aaaaaaaaaaaa"
    assert report.installation["source_root"] == "/srv/bridge"


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
        line for line in layout.pending_env_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("BRIDGE_SECRET_KEY=")
    )
    installer.install(
        SetupAnswers(domain="bridge.example.test", username="owner", password="second-password"),
        activate=False,
    )
    second_secret = next(
        line for line in layout.pending_env_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("BRIDGE_SECRET_KEY=")
    )

    assert second_secret == first_secret

def test_setup_reports_waiting_when_dns_has_not_propagated(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (source / "app").mkdir()
    (source / "app/__init__.py").write_text("", encoding="utf-8")
    layout = Layout.for_root(tmp_path / "target")
    runner = RecordingRunner()
    monkeypatch.setattr("bridge_cli.install.shutil.which", lambda _name: "/usr/bin/tool")

    def unresolved(*_args, **_kwargs):
        raise socket.gaierror("not found")

    monkeypatch.setattr("bridge_cli.install.socket.getaddrinfo", unresolved)

    result = ManagedInstaller(layout, source, runner).install(
        SetupAnswers(domain="pending.example.test", username="owner", password="long-password"),
        activate=True,
    )

    operation = json.loads(layout.operation_file.read_text(encoding="utf-8"))
    assert result["state"] == "waiting"
    assert "DNS" in result["next_step"]
    assert operation["state"] == "waiting"
    assert not any(
        call and call[-1] == "https://pending.example.test/healthz"
        for call in runner.calls
    )
    assert (
        "systemctl",
        "enable",
        "mcp-session-bridge.service",
        "mcp-session-bridge-restart.path",
        "mcp-session-bridge-status.timer",
    ) in runner.calls
    assert ("systemctl", "restart", "mcp-session-bridge.service") in runner.calls


def test_status_reports_waiting_dns_as_an_actionable_check(tmp_path: Path, monkeypatch) -> None:
    layout = Layout.for_root(tmp_path)
    layout.state_root.mkdir(parents=True)
    layout.installation_file.write_text(
        json.dumps({"mode": "managed", "public_base_url": "https://pending.example.test"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "bridge_cli.operations.socket.getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.gaierror()),
    )

    report = StatusCollector(layout, RecordingRunner(), probe_http=False).collect()

    dns = next(check for check in report.checks if check.id == "dns")
    assert dns.state == "waiting"
    assert "DNS" in (dns.remediation or "")

def test_repeated_setup_does_not_readopt_stale_legacy_database(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (source / "app").mkdir()
    (source / "app/__init__.py").write_text("", encoding="utf-8")
    legacy_db = source / "data/bridge.sqlite3"
    Store(legacy_db).create_session("legacy-session", "Legacy", "manual-context")
    layout = Layout.for_root(tmp_path / "target")
    installer = ManagedInstaller(layout, source, RecordingRunner())
    answers = SetupAnswers(
        domain="bridge.example.test",
        username="owner",
        password="long-password",
    )

    installer.install(answers, legacy_db=legacy_db, activate=False)
    Store(layout.db_path, allow_startup_migrations=False).create_session(
        "managed-session", "Created after adoption", "manual-context"
    )
    installer.install(answers, legacy_db=legacy_db, activate=False)

    managed = Store(layout.db_path, allow_startup_migrations=False)
    assert managed.get_session("legacy-session") is not None
    assert managed.get_session("managed-session") is not None

def test_configure_domain_updates_caddy_and_runtime_atomically(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (source / "app").mkdir()
    (source / "app/__init__.py").write_text("", encoding="utf-8")
    layout = Layout.for_root(tmp_path / "target")
    runner = RecordingRunner()
    ManagedInstaller(layout, source, runner).install(
        SetupAnswers(domain="old.example.test", username="owner", password="long-password"),
        activate=True,
    )
    monkeypatch.setattr("bridge_cli.__main__._require_root", lambda _command: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    result = _configure(
        Namespace(domain="new.example.test", username=None),
        layout,
        runner,
    )

    assert result == 0
    assert "https://new.example.test" in layout.env_file.read_text(encoding="utf-8")
    assert "new.example.test {" in layout.caddy_fragment.read_text(encoding="utf-8")
    assert ("caddy", "validate", "--config", str(layout.caddyfile)) in runner.calls
    assert ("systemctl", "reload", "caddy") in runner.calls
    assert ("systemctl", "restart", "mcp-session-bridge.service") in runner.calls


def test_configure_failure_restores_files_and_running_service(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (source / "app").mkdir()
    (source / "app/__init__.py").write_text("", encoding="utf-8")
    layout = Layout.for_root(tmp_path / "target")
    ManagedInstaller(layout, source, RecordingRunner()).install(
        SetupAnswers("old.example.test", "owner", "long-password"),
        activate=True,
    )
    old_env = layout.env_file.read_text(encoding="utf-8")
    old_caddy = layout.caddyfile.read_text(encoding="utf-8")
    old_fragment = layout.caddy_fragment.read_text(encoding="utf-8")
    runner = FailFirstRestartRunner()
    monkeypatch.setattr("bridge_cli.__main__._require_root", lambda _command: None)

    with pytest.raises(RuntimeError, match="forced restart failure"):
        _configure(
            Namespace(domain="new.example.test", username=None, section="domain"),
            layout,
            runner,
        )

    assert layout.env_file.read_text(encoding="utf-8") == old_env
    assert layout.caddyfile.read_text(encoding="utf-8") == old_caddy
    assert layout.caddy_fragment.read_text(encoding="utf-8") == old_fragment
    assert runner.calls.count(("systemctl", "restart", "mcp-session-bridge.service")) == 2


def test_configure_domain_updates_adopted_caddy_site_without_creating_duplicate(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (source / "app").mkdir()
    (source / "app/__init__.py").write_text("", encoding="utf-8")
    layout = Layout.for_root(tmp_path / "target")
    layout.caddyfile.parent.mkdir(parents=True)
    layout.caddyfile.write_text(
        "old.example.test {\n    bind 192.0.2.10\n    reverse_proxy 127.0.0.1:8787\n}\n",
        encoding="utf-8",
    )
    runner = RecordingRunner()
    ManagedInstaller(layout, source, runner).install(
        SetupAnswers(domain="old.example.test", username="owner", password="long-password"),
        activate=True,
    )
    monkeypatch.setattr("bridge_cli.__main__._require_root", lambda _command: None)

    result = _configure(
        Namespace(domain="new.example.test", username=None, section="domain"),
        layout,
        runner,
    )

    assert result == 0
    caddy = layout.caddyfile.read_text(encoding="utf-8")
    assert "new.example.test {" in caddy
    assert "old.example.test {" not in caddy
    assert "bind 192.0.2.10" in caddy
    assert not layout.caddy_fragment.exists()
