from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.storage import Store
from bridge_cli.caddy import has_site, replace_site_address
from bridge_cli.install import ManagedInstaller, SetupAnswers
from bridge_cli.layout import Layout
from bridge_cli.presentation import TerminalTheme
from bridge_cli.setup_wizard import (
    SetupInspector,
    SetupStep,
    SetupWizard,
    render_setup_dashboard,
)


class Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRunner:
    def __init__(self, *, active: bool = False, fail_on: tuple[str, ...] | None = None):
        self.active = active
        self.fail_on = fail_on
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, check: bool = True):
        call = tuple(args)
        self.calls.append(call)
        failed = self.fail_on is not None and call[: len(self.fail_on)] == self.fail_on
        if failed:
            result = Result(1, stderr="forced failure")
        elif call[:2] == ("systemctl", "is-active"):
            result = Result(0, "active\n") if self.active else Result(3, "inactive\n")
        elif call and call[0] == "curl":
            result = Result(0, '{"ok": true}\n')
        else:
            result = Result(0, "ok\n")
        if check and result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result


def source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (source / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (source / "app").mkdir()
    (source / "app/__init__.py").write_text("", encoding="utf-8")
    return source


def test_setup_dashboard_detects_legacy_installation_without_writing(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    legacy_db = source / "data/bridge.sqlite3"
    Store(legacy_db).create_session("existing", "Existing session", "manual-context")
    (source / ".env").write_text(
        "BRIDGE_PUBLIC_BASE_URL=https://bridge.example.test\n"
        "BRIDGE_OWNER_USERNAME=owner\n"
        "BRIDGE_OWNER_PASSWORD_HASH=existing-hash\n"
        "BRIDGE_SECRET_KEY=existing-secret\n",
        encoding="utf-8",
    )
    layout = Layout.for_root(tmp_path / "target")

    steps = SetupInspector(
        layout,
        source,
        RecordingRunner(active=True),
        legacy_db=legacy_db,
        legacy_env=source / ".env",
    ).collect()

    by_id = {step.id: step for step in steps}
    assert by_id["installation"].state == "DETECTED"
    assert by_id["database"].state == "DETECTED"
    assert "1 session" in by_id["database"].detail
    assert by_id["administrator"].state == "DETECTED"
    assert by_id["public_address"].state == "DETECTED"
    assert not layout.state_root.exists()


def test_dashboard_renders_indygo_heading_but_plain_text_can_be_forced(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")
    steps = SetupInspector(layout, source, RecordingRunner()).collect()

    colored = render_setup_dashboard(steps, TerminalTheme(enabled=True))
    plain = render_setup_dashboard(steps, TerminalTheme(enabled=False))

    assert "\x1b[38;2;99;102;241m" in colored
    assert "MCP Session Bridge" in plain
    assert "INDYGO" not in plain
    assert "\x1b[" not in plain


def test_setup_bootstrap_installs_global_cli_and_is_idempotent(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")
    installer = ManagedInstaller(layout, source, RecordingRunner())

    assert installer.ensure_global_cli() == "installed"
    first = layout.command_path.read_text(encoding="utf-8")
    assert "# MCP Session Bridge bootstrap launcher" in first
    assert f"--project {source}" in first
    assert layout.command_path.stat().st_mode & 0o777 == 0o755
    assert installer.ensure_global_cli() == "current"
    assert layout.command_path.read_text(encoding="utf-8") == first


def test_setup_bootstrap_does_not_overwrite_foreign_command(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")
    layout.command_path.parent.mkdir(parents=True)
    layout.command_path.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not owned"):
        ManagedInstaller(layout, source, RecordingRunner()).ensure_global_cli()

    assert layout.command_path.read_text(encoding="utf-8") == "#!/bin/sh\necho foreign\n"


def test_adoption_decision_is_resumable_and_advances_recommended_step(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    legacy_db = source / "data/bridge.sqlite3"
    Store(legacy_db).create_session("existing", "Existing session", "manual-context")
    layout = Layout.for_root(tmp_path / "target")
    answers = iter(["y"])
    wizard = SetupWizard(
        layout,
        source,
        RecordingRunner(active=True),
        legacy_db=legacy_db,
        legacy_env=None,
        theme=TerminalTheme(enabled=False),
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _line: None,
    )

    initial = wizard._steps()
    assert wizard._recommended(initial) == 2

    wizard._configure_adoption()
    resumed = wizard._steps()

    assert next(step for step in resumed if step.id == "installation").state == "READY"
    assert wizard._recommended(resumed) == 3

    wizard.installer.stage_release()
    assert wizard._recommended(wizard._steps()) == 4
    wizard.installer.stage_database(legacy_db)
    assert wizard._recommended(wizard._steps()) == 5


def test_no_color_environment_disables_terminal_styling(monkeypatch) -> None:
    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("NO_COLOR", "1")

    assert TerminalTheme.detect(Tty()).enabled is False


def test_readiness_action_reports_attention_when_host_tool_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    source = source_tree(tmp_path)
    outputs: list[str] = []
    answers = iter(["1", "q"])
    monkeypatch.setattr(
        "bridge_cli.setup_wizard.shutil.which",
        lambda name: None if name == "uv" else f"/usr/bin/{name}",
    )
    wizard = SetupWizard(
        Layout.for_root(tmp_path / "target"),
        source,
        RecordingRunner(),
        legacy_db=None,
        legacy_env=None,
        theme=TerminalTheme(enabled=False),
        input_fn=lambda _prompt: next(answers),
        output_fn=outputs.append,
    )

    assert wizard.run() == 0
    assert any(line.startswith("ATTENTION Missing host prerequisites: uv") for line in outputs)


def test_enter_selects_recommended_activation_step(tmp_path: Path, monkeypatch) -> None:
    source = source_tree(tmp_path)
    answers = iter(["", "q"])
    activated: list[bool] = []
    wizard = SetupWizard(
        Layout.for_root(tmp_path / "target"),
        source,
        RecordingRunner(),
        legacy_db=None,
        legacy_env=None,
        theme=TerminalTheme(enabled=False),
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _line: None,
    )
    steps = [
        SetupStep(1, "server", "Server readiness", "READY", ""),
        SetupStep(2, "installation", "Existing installation", "READY", ""),
        SetupStep(3, "release", "Bridge files", "READY", ""),
        SetupStep(4, "database", "Database", "READY", ""),
        SetupStep(5, "administrator", "Administrator", "READY", ""),
        SetupStep(6, "public_address", "Public address", "READY", ""),
        SetupStep(7, "service", "Background service", "READY", ""),
        SetupStep(8, "activate", "Activate installation", "READY", ""),
        SetupStep(9, "verify", "Verify everything", "WAITING", ""),
    ]
    monkeypatch.setattr(wizard, "_steps", lambda: steps)
    monkeypatch.setattr(wizard, "_activate", lambda: activated.append(True))

    assert wizard.run() == 0
    assert activated == [True]


def test_corrupt_database_is_reported_as_attention(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    legacy_db = source / "data/bridge.sqlite3"
    legacy_db.parent.mkdir(parents=True)
    legacy_db.write_bytes(b"not a sqlite database")

    steps = SetupInspector(
        Layout.for_root(tmp_path / "target"),
        source,
        RecordingRunner(),
        legacy_db=legacy_db,
        legacy_env=None,
    ).collect()

    database = next(step for step in steps if step.id == "database")
    assert database.state == "ATTENTION"
    assert "cannot be inspected" in database.detail


def test_detected_admin_and_domain_must_each_be_confirmed_before_service_step(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    legacy_db = source / "data/bridge.sqlite3"
    Store(legacy_db).create_session("existing", "Existing", "manual-context")
    legacy_env = source / ".env"
    legacy_env.write_text(
        "BRIDGE_PUBLIC_BASE_URL=https://bridge.example.test\n"
        "BRIDGE_OWNER_USERNAME=owner\n"
        "BRIDGE_OWNER_PASSWORD_HASH=existing-hash\n"
        "BRIDGE_SECRET_KEY=existing-secret\n",
        encoding="utf-8",
    )
    layout = Layout.for_root(tmp_path / "target")
    answers = iter(["y", ""])
    wizard = SetupWizard(
        layout,
        source,
        RecordingRunner(active=True),
        legacy_db=legacy_db,
        legacy_env=legacy_env,
        theme=TerminalTheme(enabled=False),
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _line: None,
    )
    wizard._write_state({"adopt_legacy": True})
    wizard.installer.stage_release()
    wizard.installer.stage_database(legacy_db)

    assert wizard._recommended(wizard._steps()) == 5
    wizard._configure_administrator()
    assert wizard._recommended(wizard._steps()) == 6
    wizard._configure_public_address()
    assert wizard._recommended(wizard._steps()) == 7


def test_prepare_does_not_replace_live_systemd_or_caddy(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")
    previous_release = layout.releases_root / "previous"
    previous_release.mkdir(parents=True)
    layout.current_link.parent.mkdir(parents=True, exist_ok=True)
    layout.current_link.symlink_to(previous_release)
    layout.service_unit.parent.mkdir(parents=True)
    layout.service_unit.write_text("legacy unit\n", encoding="utf-8")
    layout.caddyfile.parent.mkdir(parents=True)
    layout.caddyfile.write_text(
        "bridge.example.test {\n    reverse_proxy 127.0.0.1:8787\n}\n",
        encoding="utf-8",
    )

    runner = RecordingRunner(active=True)
    result = ManagedInstaller(layout, source, runner).install(
        SetupAnswers("bridge.example.test", "owner", "long-password"),
        activate=False,
    )

    assert result["state"] == "prepared"
    assert layout.service_unit.read_text(encoding="utf-8") == "legacy unit\n"
    assert "reverse_proxy" in layout.caddyfile.read_text(encoding="utf-8")
    assert layout.pending_service_unit.exists()
    assert layout.pending_caddy_fragment.exists()
    assert layout.current_link.resolve() == previous_release.resolve()
    assert ("chown", "-R", "root:root", str(layout.state_root)) in runner.calls

    steps = SetupInspector(layout, source, runner).collect()
    service = next(step for step in steps if step.id == "service")
    assert service.state == "READY"
    assert "legacy service remains active" in service.detail
    assert SetupWizard._recommended(steps) == 8


def test_activation_adopts_existing_caddy_site_and_restarts_service(tmp_path: Path, monkeypatch) -> None:
    source = source_tree(tmp_path)
    legacy_db = source / "data/bridge.sqlite3"
    Store(legacy_db).create_session("existing", "Existing session", "manual-context")
    layout = Layout.for_root(tmp_path / "target")
    layout.caddyfile.parent.mkdir(parents=True)
    layout.caddyfile.write_text(
        "bridge.example.test {\n    reverse_proxy 127.0.0.1:8787\n}\n",
        encoding="utf-8",
    )
    runner = RecordingRunner(active=True)
    monkeypatch.setattr("bridge_cli.install._dns_resolves", lambda _domain: True)

    installer = ManagedInstaller(layout, source, runner)
    prepared = installer.install(
        SetupAnswers("bridge.example.test", "owner", "long-password"),
        legacy_db=legacy_db,
        activate=False,
    )
    assert prepared["state"] == "prepared"
    layout.data_root.chmod(0o700)

    result = installer.activate(legacy_db)

    assert result["state"] == "complete"
    assert layout.data_root.stat().st_mode & 0o777 == 0o750
    assert layout.current_link.resolve() == layout.release_dir(result["version"]).resolve()
    assert runner.calls.index(("systemctl", "stop", "mcp-session-bridge.service")) < runner.calls.index(
        ("systemctl", "restart", "mcp-session-bridge.service")
    )
    assert layout.caddyfile.read_text(encoding="utf-8").count("bridge.example.test {") == 1
    assert not layout.caddy_fragment.exists()
    assert Store(layout.db_path, allow_startup_migrations=False).get_session("existing") is not None


def test_stage_release_refreshes_a_prepared_release_but_not_the_live_release(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")
    installer = ManagedInstaller(layout, source, RecordingRunner())

    release = installer.stage_release()
    (source / "app" / "fresh.py").write_text("fresh = True\n", encoding="utf-8")
    installer.stage_release()

    assert (release / "app" / "fresh.py").exists()

    layout.current_link.parent.mkdir(parents=True, exist_ok=True)
    layout.current_link.symlink_to(release)
    (source / "app" / "live_only.py").write_text("live = True\n", encoding="utf-8")
    installer.stage_release()

    assert not (release / "app" / "live_only.py").exists()


def test_activation_failure_restores_legacy_unit_and_restarts_it(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")
    previous_release = layout.releases_root / "previous"
    previous_release.mkdir(parents=True)
    layout.current_link.parent.mkdir(parents=True, exist_ok=True)
    layout.current_link.symlink_to(previous_release)
    layout.service_unit.parent.mkdir(parents=True)
    layout.service_unit.write_text("legacy unit\n", encoding="utf-8")
    layout.caddyfile.parent.mkdir(parents=True)
    layout.caddyfile.write_text("other.example.test {\n    respond 200\n}\n", encoding="utf-8")
    runner = RecordingRunner(active=True, fail_on=("curl",))

    with pytest.raises(RuntimeError, match="restored"):
        ManagedInstaller(layout, source, runner).install(
            SetupAnswers("bridge.example.test", "owner", "long-password"),
            activate=True,
        )

    assert layout.service_unit.read_text(encoding="utf-8") == "legacy unit\n"
    assert layout.current_link.resolve() == previous_release.resolve()
    assert runner.calls.count(("systemctl", "restart", "mcp-session-bridge.service")) == 2
    assert (
        "systemctl",
        "disable",
        "mcp-session-bridge.service",
        "mcp-session-bridge-restart.path",
        "mcp-session-bridge-status.timer",
    ) in runner.calls
    receipt = json.loads(layout.operation_file.read_text(encoding="utf-8"))
    assert receipt["rollback"] == "restored"


def test_caddy_adoption_recognizes_and_rewrites_multi_address_header() -> None:
    original = (
        "other.example.test, https://bridge.example.test:443 {\n"
        "    reverse_proxy 127.0.0.1:8787\n"
        "}\n"
    )
    assert has_site(original, "bridge.example.test")
    rendered = replace_site_address(original, "bridge.example.test", "new.example.test")
    assert "other.example.test, new.example.test:443 {" in rendered
    assert "bridge.example.test" not in rendered


def test_activation_reports_when_automatic_restore_itself_fails(tmp_path: Path, monkeypatch) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")
    runner = RecordingRunner(active=True, fail_on=("caddy", "validate"))
    installer = ManagedInstaller(layout, source, runner)
    installer.install(
        SetupAnswers("bridge.example.test", "owner", "long-password"),
        activate=False,
    )
    monkeypatch.setattr(
        installer,
        "_restore_activation",
        lambda _backup: (_ for _ in ()).throw(RuntimeError("restore unavailable")),
    )

    with pytest.raises(RuntimeError, match="automatic restore also failed"):
        installer.activate()

    receipt = json.loads(layout.operation_file.read_text(encoding="utf-8"))
    assert receipt["rollback"] == "failed"


def test_prepare_can_keep_existing_owner_password_hash(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    legacy_env = source / ".env"
    legacy_env.write_text(
        "BRIDGE_OWNER_USERNAME=owner\n"
        "BRIDGE_OWNER_PASSWORD_HASH=keep-this-hash\n"
        "BRIDGE_SECRET_KEY=keep-this-secret\n",
        encoding="utf-8",
    )
    layout = Layout.for_root(tmp_path / "target")

    ManagedInstaller(layout, source, RecordingRunner()).install(
        SetupAnswers("bridge.example.test", None, None),
        legacy_env=legacy_env,
        activate=False,
    )

    staged = layout.pending_env_file.read_text(encoding="utf-8")
    assert "BRIDGE_OWNER_USERNAME=owner" in staged
    assert "BRIDGE_OWNER_PASSWORD_HASH=keep-this-hash" in staged
    assert "BRIDGE_SECRET_KEY=keep-this-secret" in staged
    assert f"BRIDGE_DB_PATH={layout.db_path}" in staged


def test_prepare_keeps_live_managed_environment_unchanged(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")
    layout.env_file.parent.mkdir(parents=True)
    live = (
        "BRIDGE_PUBLIC_BASE_URL=https://old.example.test\n"
        "BRIDGE_OWNER_USERNAME=old-owner\n"
        "BRIDGE_OWNER_PASSWORD_HASH=old-hash\n"
        "BRIDGE_SECRET_KEY=old-secret\n"
    )
    layout.env_file.write_text(live, encoding="utf-8")

    ManagedInstaller(layout, source, RecordingRunner()).install(
        SetupAnswers("new.example.test", "new-owner", "new-long-password"),
        activate=False,
    )

    assert layout.env_file.read_text(encoding="utf-8") == live
    assert "https://new.example.test" in layout.pending_env_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "domain",
    [
        "bridge.example.test, attacker.example.test",
        "bridge.example.test {",
        "bridge.example.test:443",
        "bridge..example.test",
        "-bridge.example.test",
        "bridge.example.test\nattacker.example.test",
    ],
)
def test_domain_validation_rejects_caddy_control_input(tmp_path: Path, domain: str) -> None:
    with pytest.raises(ValueError, match="hostname"):
        ManagedInstaller(
            Layout.for_root(tmp_path / "target"),
            source_tree(tmp_path),
            RecordingRunner(),
        ).configure_domain(domain)


def test_setup_requires_adoption_decision_before_dependent_steps(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    legacy_db = source / "data/bridge.sqlite3"
    Store(legacy_db).create_session("existing", "Existing", "manual-context")
    wizard = SetupWizard(
        Layout.for_root(tmp_path / "target"),
        source,
        RecordingRunner(),
        legacy_db=legacy_db,
        legacy_env=None,
        theme=TerminalTheme(enabled=False),
        output_fn=lambda _line: None,
    )

    with pytest.raises(RuntimeError, match="Choose step 2"):
        wizard._prepare_database()


def test_setup_rejects_adoption_change_after_dependent_staging(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    legacy_db = source / "data/bridge.sqlite3"
    Store(legacy_db).create_session("existing", "Existing", "manual-context")
    answers = iter(["n"])
    wizard = SetupWizard(
        Layout.for_root(tmp_path / "target"),
        source,
        RecordingRunner(),
        legacy_db=legacy_db,
        legacy_env=None,
        theme=TerminalTheme(enabled=False),
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _line: None,
    )
    wizard._write_state({"adopt_legacy": True})
    wizard._prepare_database()

    with pytest.raises(RuntimeError, match="cannot change"):
        wizard._configure_adoption()


def test_caddy_site_detection_ignores_nested_directives() -> None:
    caddyfile = (
        "other.example.test {\n"
        "    @bridge host bridge.example.test\n"
        "    handle @bridge {\n"
        "        reverse_proxy 127.0.0.1:9999\n"
        "    }\n"
        "}\n"
    )

    assert has_site(caddyfile, "bridge.example.test") is False
    assert replace_site_address(
        caddyfile, "bridge.example.test", "new.example.test"
    ) == caddyfile


def test_reactivation_of_managed_installation_never_readopts_legacy_database(
    tmp_path: Path, monkeypatch
) -> None:
    source = source_tree(tmp_path)
    legacy_db = source / "data/bridge.sqlite3"
    Store(legacy_db).create_session("legacy", "Legacy", "manual-context")
    layout = Layout.for_root(tmp_path / "target")
    runner = RecordingRunner(active=True)
    monkeypatch.setattr("bridge_cli.install._dns_resolves", lambda _domain: True)
    installer = ManagedInstaller(layout, source, runner)

    installer.install(
        SetupAnswers("bridge.example.test", "owner", "long-password"),
        legacy_db=legacy_db,
        activate=True,
    )
    Store(layout.db_path, allow_startup_migrations=False).create_session(
        "managed-only", "Created after adoption", "manual-context"
    )

    installer.activate(legacy_db)

    managed = Store(layout.db_path, allow_startup_migrations=False)
    assert managed.get_session("managed-only") is not None


def test_failed_managed_reactivation_keeps_last_write_and_removes_wal_sidecars(
    tmp_path: Path, monkeypatch
) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")
    monkeypatch.setattr("bridge_cli.install._dns_resolves", lambda _domain: True)
    ManagedInstaller(layout, source, RecordingRunner(active=True)).install(
        SetupAnswers("bridge.example.test", "owner", "long-password"),
        activate=True,
    )

    class CutoverFailureRunner(RecordingRunner):
        wrote = False

        def run(self, *args: str, check: bool = True):
            if args == ("systemctl", "stop", "mcp-session-bridge.service") and not self.wrote:
                Store(layout.db_path, allow_startup_migrations=False).create_session(
                    "last-write", "Accepted before stop", "manual-context"
                )
                self.wrote = True
            if args and args[0] == "curl":
                Path(f"{layout.db_path}-wal").write_bytes(b"stale")
                Path(f"{layout.db_path}-shm").write_bytes(b"stale")
            return super().run(*args, check=check)

    runner = CutoverFailureRunner(active=True, fail_on=("curl",))

    with pytest.raises(RuntimeError, match="restored"):
        ManagedInstaller(layout, source, runner).activate()

    assert not Path(f"{layout.db_path}-wal").exists()
    assert not Path(f"{layout.db_path}-shm").exists()
    restored = Store(layout.db_path, allow_startup_migrations=False)
    assert restored.get_session("last-write") is not None


def test_activation_checks_public_route_after_caddy_reload(tmp_path: Path, monkeypatch) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")
    runner = RecordingRunner(active=False)
    monkeypatch.setattr("bridge_cli.install._dns_resolves", lambda _domain: True)

    ManagedInstaller(layout, source, runner).install(
        SetupAnswers("bridge.example.test", "owner", "long-password"),
        activate=True,
    )

    reload_index = runner.calls.index(("systemctl", "reload", "caddy"))
    public_health = next(
        index
        for index, call in enumerate(runner.calls)
        if call and call[-1] == "https://bridge.example.test/healthz"
    )
    assert reload_index < public_health
    curl_calls = [call for call in runner.calls if call and call[0] == "curl"]
    assert len(curl_calls) == 2
    assert all("--retry-connrefused" in call for call in curl_calls)


def test_activation_rejects_http_200_without_bridge_health_contract(
    tmp_path: Path, monkeypatch
) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")
    monkeypatch.setattr("bridge_cli.install._dns_resolves", lambda _domain: True)

    class WrongPublicHealthRunner(RecordingRunner):
        def run(self, *args: str, check: bool = True):
            result = super().run(*args, check=check)
            if args and args[-1] == "https://bridge.example.test/healthz":
                return Result(0, "not the Bridge\n")
            return result

    with pytest.raises(RuntimeError, match="restored"):
        ManagedInstaller(
            layout, source, WrongPublicHealthRunner(active=False)
        ).install(
            SetupAnswers("bridge.example.test", "owner", "long-password"),
            activate=True,
        )

    receipt = json.loads(layout.operation_file.read_text(encoding="utf-8"))
    assert receipt["rollback"] == "restored"


def test_rollback_reports_failure_when_service_cannot_stop(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    layout = Layout.for_root(tmp_path / "target")

    class RollbackStopFailureRunner(RecordingRunner):
        stop_count = 0

        def run(self, *args: str, check: bool = True):
            if args == ("systemctl", "stop", "mcp-session-bridge.service"):
                self.stop_count += 1
                if self.stop_count == 2:
                    self.calls.append(tuple(args))
                    return Result(1, stderr="rollback stop failed")
            return super().run(*args, check=check)

    runner = RollbackStopFailureRunner(active=True, fail_on=("curl",))

    with pytest.raises(RuntimeError, match="automatic restore also failed"):
        ManagedInstaller(layout, source, runner).install(
            SetupAnswers("bridge.example.test", "owner", "long-password"),
            activate=True,
        )

    receipt = json.loads(layout.operation_file.read_text(encoding="utf-8"))
    assert receipt["rollback"] == "failed"
