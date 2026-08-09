from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridge_cli.database import DatabaseManager
from bridge_cli.layout import Layout
from bridge_cli.operation_lock import operation_lock
from bridge_cli.runner import Runner
from bridge_cli.service import ServiceManager
from bridge_cli.files import read_json
from bridge_cli.codex_runtime import (
    CODEX_SERVICE_UNIT,
    CODEX_SERVICE_USER,
    CODEX_SOCKET_GROUP,
)


@dataclass(frozen=True)
class UninstallPlan:
    targets: tuple[str, ...]
    database_export: str | None
    remove_data: bool
    ownership_verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": 1,
            "operation": "installation.uninstall",
            "targets": list(self.targets),
            "database_export": self.database_export,
            "remove_data": self.remove_data,
            "ownership_verified": self.ownership_verified,
        }


class UninstallManager:
    def __init__(self, layout: Layout, runner: Runner):
        self.layout = layout
        self.runner = runner
        self.database = DatabaseManager(layout, runner)

    def plan(
        self, *, export_to: Path | None, remove_data: bool, allow_unverified: bool = False
    ) -> UninstallPlan:
        if remove_data and self.layout.db_path.exists() and export_to is None:
            raise ValueError("Removing Bridge data requires an explicit database export path.")
        if export_to is not None:
            export_to = export_to.expanduser().resolve()
            if export_to.exists():
                raise ValueError(f"Database export already exists: {export_to}")
            if any(export_to.is_relative_to(root.resolve()) for root in self._managed_roots()):
                raise ValueError("Database export must be outside every Bridge-managed path.")
        known_targets = self._owned_targets(remove_data)
        ownership = read_json(self.layout.ownership_file)
        recorded = {
            str(path) for path in (ownership or {}).get("paths", []) if isinstance(path, str)
        }
        verified = bool(ownership and ownership.get("owner") == "mcp-session-bridge")
        if not verified and not allow_unverified:
            raise RuntimeError(
                "Installation ownership manifest is missing. Re-run managed activation, or explicitly allow unverified legacy removal."
            )
        selected = (
            [path for path in known_targets if str(path) in recorded]
            if verified
            else list(known_targets)
        )
        targets = tuple(str(path) for path in selected if path.exists() or path.is_symlink())
        return UninstallPlan(
            targets, str(export_to) if export_to else None, remove_data, verified
        )

    def execute(self, plan: UninstallPlan, *, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return {**plan.to_dict(), "state": "dry_run", "changed": False}
        export_result = None
        with operation_lock(self.layout.operation_lock_file, self.layout.legacy_operation_lock_file):
            was_active = bool(ServiceManager(self.layout, self.runner).inspect()["service"]["active"])
            units = (
                "mcp-session-bridge.service",
                "mcp-session-bridge-restart.path",
                "mcp-session-bridge-status.timer",
                "mcp-session-bridge-restart.service",
                "mcp-session-bridge-status.service",
            )
            codex_owned = str(self.layout.codex_service_unit) in plan.targets
            codex_state_owned = str(self.layout.codex_state_root) in plan.targets
            if was_active:
                self.runner.run("systemctl", "stop", "mcp-session-bridge.service")
            try:
                if plan.database_export and self.layout.db_path.exists():
                    export_result = self.database.backup(Path(plan.database_export))
            except Exception:
                if was_active:
                    self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
                raise
            self.runner.run("systemctl", "disable", "--now", *units, check=False)
            if codex_owned:
                self.runner.run(
                    "systemctl", "disable", "--now", CODEX_SERVICE_UNIT,
                    check=False,
                )
            ownership = read_json(self.layout.ownership_file) or {}
            removed: list[str] = []
            for raw in plan.targets:
                path = Path(raw)
                if path == self.layout.command_path and path.exists():
                    text = path.read_text(encoding="utf-8", errors="replace")
                    if "MCP Session Bridge" not in text:
                        continue
                if path.is_symlink() or path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    shutil.rmtree(path)
                removed.append(str(path))
            if (
                codex_state_owned
                and plan.remove_data
                and ownership.get("codex_service_user") == CODEX_SERVICE_USER
            ):
                self.runner.run("userdel", CODEX_SERVICE_USER, check=False)
                self.runner.run(
                    "gpasswd", "-d", "mcp-session-bridge", CODEX_SOCKET_GROUP, check=False
                )
                if ownership.get("codex_socket_group_created") is True:
                    self.runner.run("groupdel", CODEX_SOCKET_GROUP, check=False)
            self.runner.run("systemctl", "daemon-reload", check=False)
            if self.layout.caddyfile.exists():
                validation = self.runner.run("caddy", "validate", "--config", str(self.layout.caddyfile), check=False)
                if validation.returncode == 0:
                    self.runner.run("systemctl", "reload", "caddy", check=False)
        return {
            **plan.to_dict(),
            "state": "complete",
            "changed": bool(removed),
            "removed": removed,
            "database_export_result": export_result,
            "note": "The source checkout was not removed.",
        }

    def _owned_targets(self, remove_data: bool) -> tuple[Path, ...]:
        targets: tuple[Path, ...] = (
            self.layout.service_unit,
            self.layout.restart_path_unit,
            self.layout.restart_service_unit,
            self.layout.status_service_unit,
            self.layout.status_timer_unit,
            self.layout.caddy_fragment,
            self.layout.command_path,
            self.layout.opt_root,
            self.layout.env_file,
            self.layout.codex_service_unit,
            self.layout.codex_runtime_root,
            self.layout.codex_status_file,
        )
        if not remove_data:
            return targets
        return (
            *targets,
            self.layout.db_path,
            Path(f"{self.layout.db_path}-wal"),
            Path(f"{self.layout.db_path}-shm"),
            self.layout.context_packs_dir,
            self.layout.pending_root,
            self.layout.installation_file,
            self.layout.status_file,
            self.layout.update_file,
            self.layout.operation_file,
            self.layout.setup_file,
            self.layout.database_status_file,
            self.layout.ownership_file,
            self.layout.codex_state_root,
        )

    def _managed_roots(self) -> tuple[Path, ...]:
        return (
            self.layout.opt_root,
            self.layout.etc_root,
            self.layout.data_root,
            self.layout.backup_root,
            self.layout.codex_state_root,
        )
