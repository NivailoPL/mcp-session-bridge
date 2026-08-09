from __future__ import annotations

import json
import grp
import os
import shutil
import socket
import stat
import subprocess
import time
from pathlib import Path

import pytest

from bridge_cli.__main__ import build_parser
from bridge_cli.codex_runtime import (
    CODEX_PACKAGE,
    CODEX_VERSION,
    CodexRuntimeManager,
    load_runtime_lock,
)
from bridge_cli.layout import Layout
from bridge_cli.release import UpdateManager


class Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRunner:
    def __init__(self, *, fail_on: tuple[str, ...] | None = None):
        self.fail_on = fail_on
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, check: bool = True):
        call = tuple(args)
        self.calls.append(call)
        failed = self.fail_on is not None and call[: len(self.fail_on)] == self.fail_on
        if call[:2] == ("systemctl", "is-active"):
            result = Result(3, "inactive\n")
        elif call[:2] == ("systemctl", "is-enabled"):
            result = Result(1, "disabled\n")
        elif failed:
            result = Result(1, stderr="forced failure")
        else:
            result = Result(0, "ok\n")
        if call[:2] == ("npm", "ci") and not failed:
            prefix = Path(call[call.index("--prefix") + 1])
            package = prefix / "node_modules/@openai/codex/package.json"
            package.parent.mkdir(parents=True, exist_ok=True)
            package.write_text(json.dumps({"version": CODEX_VERSION}), encoding="utf-8")
        if check and result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result


def _source_root(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    lock_dir = source / "deploy/codex-runtime"
    lock_dir.mkdir(parents=True)
    lock_dir.joinpath("package.json").write_text(
        json.dumps(
            {
                "name": "mcp-session-bridge-codex-runtime",
                "private": True,
                "version": "0.0.0",
                "dependencies": {CODEX_PACKAGE: CODEX_VERSION},
            }
        ),
        encoding="utf-8",
    )
    lock_dir.joinpath("package-lock.json").write_text(
        json.dumps(
            {
                "name": "mcp-session-bridge-codex-runtime",
                "version": "0.0.0",
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {CODEX_PACKAGE: CODEX_VERSION}},
                    "node_modules/@openai/codex": {
                        "version": CODEX_VERSION,
                        "resolved": (
                            "https://registry.npmjs.org/@openai/codex/-/"
                            f"codex-{CODEX_VERSION}.tgz"
                        ),
                        "integrity": "sha512-test",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    shutil.copy2(
        Path(__file__).parents[1] / "deploy/codex-runtime/run-app-server.sh",
        lock_dir / "run-app-server.sh",
    )
    return source


def test_runtime_lock_is_exact_and_integrity_pinned() -> None:
    lock = load_runtime_lock(Path(__file__).parents[1])

    assert lock.package == "@openai/codex"
    assert lock.version == "0.147.0"
    assert lock.integrity.startswith("sha512-")
    assert lock.resolved.endswith("/codex-0.147.0.tgz")


def test_layout_keeps_codex_runtime_and_state_isolated(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")

    assert layout.codex_runtime_root == layout.opt_root / "codex-runtime"
    assert layout.codex_state_root == layout._path("var/lib/mcp-session-bridge-codex")
    assert layout.codex_service_unit == layout.systemd_root / "mcp-session-bridge-codex.service"
    assert layout.codex_socket == layout._path("run/mcp-session-bridge-codex/app-server.sock")


def test_enable_stages_locked_runtime_and_hardened_unit(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    runner = RecordingRunner()
    manager = CodexRuntimeManager(layout, runner, _source_root(tmp_path))

    result = manager.enable()

    assert result["state"] == "complete"
    assert result["runtime"]["enabled"] is True
    unit = layout.codex_service_unit.read_text(encoding="utf-8")
    assert "User=mcp-session-bridge-codex" in unit
    assert "Group=mcp-session-bridge-codex" in unit
    assert "SupplementaryGroups=mcp-session-bridge-codex-socket" in unit
    assert "RuntimeDirectoryMode=0700" in unit
    assert "UMask=0077" in unit
    assert "run-app-server.sh" in unit
    wrapper = layout.codex_runtime_current / "run-app-server.sh"
    assert '--listen "unix://$socket_path"' in wrapper.read_text(encoding="utf-8")
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "PrivateDevices=true" in unit
    assert "InaccessiblePaths=" in unit
    assert any(call[:2] == ("npm", "ci") for call in runner.calls)
    assert ("systemctl", "enable", "--now", "mcp-session-bridge-codex.service") in runner.calls
    assert not any(call[-1:] == ("mcp-session-bridge.service",) and "stop" in call for call in runner.calls)


def test_failed_install_preserves_bridge_and_records_codex_failure(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    runner = RecordingRunner(fail_on=("npm", "ci"))
    manager = CodexRuntimeManager(layout, runner, _source_root(tmp_path))

    with pytest.raises(RuntimeError, match="forced failure"):
        manager.enable()

    status = json.loads(layout.codex_status_file.read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["bridge_unchanged"] is True
    assert not any("mcp-session-bridge.service" in call for call in runner.calls)


def test_disable_is_idempotent_and_keeps_installed_runtime(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    runner = RecordingRunner()
    manager = CodexRuntimeManager(layout, runner, _source_root(tmp_path))
    manager.enable()

    first = manager.disable()
    second = manager.disable()

    assert first["runtime"]["enabled"] is False
    assert second["runtime"]["enabled"] is False
    assert layout.codex_runtime_current.is_symlink()
    assert runner.calls.count(
        ("systemctl", "disable", "--now", "mcp-session-bridge-codex.service")
    ) == 2


def test_repair_failure_keeps_previous_runtime_current(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    runner = RecordingRunner()
    manager = CodexRuntimeManager(layout, runner, _source_root(tmp_path))
    manager.enable()
    previous = layout.codex_runtime_current.resolve()
    previous.joinpath("marker").write_text("keep", encoding="utf-8")
    runner.fail_on = ("npm", "ci")

    with pytest.raises(RuntimeError, match="forced failure"):
        manager.repair()

    assert layout.codex_runtime_current.resolve() == previous
    assert previous.joinpath("marker").read_text(encoding="utf-8") == "keep"


def test_repair_restart_failure_rolls_back_runtime_and_unit(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    runner = RecordingRunner()
    manager = CodexRuntimeManager(layout, runner, _source_root(tmp_path))
    manager.enable()
    previous = layout.codex_runtime_current.resolve()
    previous.joinpath("marker").write_text("keep", encoding="utf-8")
    previous_unit = layout.codex_service_unit.read_text(encoding="utf-8")
    runner.fail_on = ("systemctl", "restart")

    with pytest.raises(RuntimeError, match="forced failure"):
        manager.repair()

    assert layout.codex_runtime_current.resolve() == previous
    assert previous.joinpath("marker").read_text(encoding="utf-8") == "keep"
    assert layout.codex_service_unit.read_text(encoding="utf-8") == previous_unit


def test_state_preparation_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    layout.codex_state_root.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    layout.codex_home.symlink_to(external, target_is_directory=True)
    manager = CodexRuntimeManager(layout, RecordingRunner(), _source_root(tmp_path))

    with pytest.raises(RuntimeError, match="unsafe Codex state path"):
        manager.enable()

    assert stat.S_IMODE(external.stat().st_mode) == 0o755
    assert layout.codex_home.is_symlink()


def test_runtime_wrapper_sets_socket_group_mode(tmp_path: Path) -> None:
    wrapper = Path(__file__).parents[1] / "deploy/codex-runtime/run-app-server.sh"
    fake = tmp_path / "fake-codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import signal, socket, sys, time\n"
        "path = next(x[7:] for x in sys.argv if x.startswith('unix://'))\n"
        "sock = socket.socket(socket.AF_UNIX)\n"
        "sock.bind(path)\n"
        "sock.listen(1)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    socket_path = runtime_dir / "app.sock"
    group = grp.getgrgid(os.getgid()).gr_name
    process = subprocess.Popen(
        ["sh", str(wrapper), str(socket_path), group, str(fake)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            if socket_path.exists() and stat.S_IMODE(socket_path.stat().st_mode) == 0o660:
                break
            time.sleep(0.05)
        assert socket_path.exists()
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o660
        probe = socket.socket(socket.AF_UNIX)
        probe.connect(str(socket_path))
        probe.close()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_bridge_health_probe_retries_during_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def urlopen(*_args: object, **_kwargs: object) -> Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionRefusedError
        return Response()

    monkeypatch.setattr("bridge_cli.codex_runtime.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("bridge_cli.codex_runtime.time.sleep", lambda _seconds: None)

    assert CodexRuntimeManager._bridge_http_ready() is True
    assert attempts == 3


def test_release_reconcile_failure_is_best_effort_for_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = Layout.for_root(tmp_path / "root")

    def fail_reconcile(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("codex install failed")

    monkeypatch.setattr(CodexRuntimeManager, "reconcile_after_release", fail_reconcile)

    result = UpdateManager(layout, RecordingRunner())._reconcile_codex_runtime(tmp_path)

    assert result["state"] == "failed_preserved"
    assert result["bridge_unchanged"] is True
    assert "codex install failed" in str(result["error"])


def test_release_reconcile_skips_unchanged_healthy_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = Layout.for_root(tmp_path / "root")
    runner = RecordingRunner()
    manager = CodexRuntimeManager(layout, runner, _source_root(tmp_path))
    manager.enable()
    lock = load_runtime_lock(manager.source_root)
    persisted = json.loads(layout.codex_status_file.read_text(encoding="utf-8"))
    assert persisted["lock_digest"] == lock.digest
    monkeypatch.setattr(
        manager,
        "inspect",
        lambda: {
            "runtime": {
                "installed_version": CODEX_VERSION,
                "expected_version": CODEX_VERSION,
                "active": True,
                "socket_ready": True,
            }
        },
    )
    monkeypatch.setattr(manager, "_protocol_ready", lambda: True)
    npm_calls = sum(call[:2] == ("npm", "ci") for call in runner.calls)

    result = manager.reconcile_after_release()

    assert result["state"] == "complete"
    assert result["changed"] is False
    assert sum(call[:2] == ("npm", "ci") for call in runner.calls) == npm_calls


def test_codex_runtime_cli_has_conservative_lifecycle_seams() -> None:
    parser = build_parser()

    for action in ("status", "enable", "repair", "disable", "verify", "logs"):
        args = parser.parse_args(["codex-runtime", action])
        assert args.command == "codex-runtime"
        assert args.codex_runtime_action == action
