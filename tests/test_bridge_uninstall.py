from __future__ import annotations

from pathlib import Path

import pytest

from app.storage import Store
from bridge_cli.layout import Layout
from bridge_cli.uninstall import UninstallManager


class Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Runner:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, check: bool = True):
        self.calls.append(tuple(args))
        if args[:2] == ("systemctl", "is-active"):
            return Result(0, "active\n")
        return Result()


def test_uninstall_dry_run_does_not_change_files(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    layout.service_unit.parent.mkdir(parents=True)
    layout.service_unit.write_text("owned", encoding="utf-8")
    Store(layout.db_path).create_session("keep", "Keep", "manual-context")
    export = tmp_path / "portable.sqlite3"
    manager = UninstallManager(layout, Runner())

    plan = manager.plan(export_to=export, remove_data=True, allow_unverified=True)
    result = manager.execute(plan, dry_run=True)

    assert result["state"] == "dry_run"
    assert layout.service_unit.exists()
    assert layout.db_path.exists()
    assert not export.exists()


def test_uninstall_exports_database_then_removes_only_managed_targets(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    layout.service_unit.parent.mkdir(parents=True)
    layout.service_unit.write_text("owned", encoding="utf-8")
    Store(layout.db_path).create_session("keep", "Keep", "manual-context")
    unrelated = layout.caddy_root / "unrelated.caddy"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("unrelated.test {}", encoding="utf-8")
    checkout = tmp_path / "source-checkout"
    checkout.mkdir()
    export = tmp_path / "portable.sqlite3"
    manager = UninstallManager(layout, Runner())

    result = manager.execute(
        manager.plan(export_to=export, remove_data=True, allow_unverified=True)
    )

    assert result["state"] == "complete"
    assert export.exists()
    assert manager.database.inspect(export).sessions == 1
    assert not layout.db_path.exists()
    assert unrelated.exists()
    assert checkout.exists()
    stop_index = next(i for i, call in enumerate(manager.runner.calls) if call[:2] == ("systemctl", "stop"))
    disable_call = next(call for call in manager.runner.calls if call[:3] == ("systemctl", "disable", "--now"))
    assert stop_index >= 0
    assert "mcp-session-bridge-restart.path" in disable_call
    assert "mcp-session-bridge-status.timer" in disable_call


def test_uninstall_refuses_unverified_legacy_paths_without_explicit_consent(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    Store(layout.db_path).create_session("keep", "Keep", "manual-context")

    with pytest.raises(RuntimeError, match="ownership manifest is missing"):
        UninstallManager(layout, Runner()).plan(
            export_to=tmp_path / "portable.sqlite3", remove_data=True
        )


def test_database_export_cannot_be_placed_inside_codex_state(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    Store(layout.db_path).create_session("keep", "Keep", "manual-context")
    layout.codex_state_root.mkdir(parents=True)

    with pytest.raises(ValueError, match="outside every Bridge-managed path"):
        UninstallManager(layout, Runner()).plan(
            export_to=layout.codex_state_root / "export.sqlite3",
            remove_data=True,
            allow_unverified=True,
        )


def test_uninstall_preserves_codex_login_state_without_data_removal(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    layout.codex_service_unit.parent.mkdir(parents=True)
    layout.codex_service_unit.write_text("owned", encoding="utf-8")
    layout.codex_runtime_root.mkdir(parents=True)
    layout.codex_state_root.mkdir(parents=True)
    layout.ownership_file.parent.mkdir(parents=True)
    layout.ownership_file.write_text(
        '{"owner":"mcp-session-bridge","codex_service_user":"mcp-session-bridge-codex",'
        f'"paths":["{layout.codex_service_unit}","{layout.codex_runtime_root}",'
        f'"{layout.codex_state_root}"]}}',
        encoding="utf-8",
    )
    runner = Runner()
    manager = UninstallManager(layout, runner)

    result = manager.execute(manager.plan(export_to=None, remove_data=False))

    assert result["state"] == "complete"
    assert not layout.codex_service_unit.exists()
    assert not layout.codex_runtime_root.exists()
    assert layout.codex_state_root.exists()
    assert (
        "systemctl", "disable", "--now", "mcp-session-bridge-codex.service"
    ) in runner.calls
    assert ("userdel", "mcp-session-bridge-codex") not in runner.calls


def test_uninstall_with_data_removal_purges_codex_state_and_user(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    layout.codex_service_unit.parent.mkdir(parents=True)
    layout.codex_service_unit.write_text("owned", encoding="utf-8")
    layout.codex_state_root.mkdir(parents=True)
    layout.ownership_file.parent.mkdir(parents=True)
    layout.ownership_file.write_text(
        '{"owner":"mcp-session-bridge","codex_service_user":"mcp-session-bridge-codex",'
        f'"paths":["{layout.codex_service_unit}","{layout.codex_state_root}"]}}',
        encoding="utf-8",
    )
    runner = Runner()
    manager = UninstallManager(layout, runner)

    result = manager.execute(
        manager.plan(export_to=None, remove_data=True)
    )

    assert result["state"] == "complete"
    assert not layout.codex_state_root.exists()
    assert ("userdel", "mcp-session-bridge-codex") in runner.calls
