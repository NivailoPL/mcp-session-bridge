from __future__ import annotations

from typing import Any

from bridge_cli.layout import Layout
from bridge_cli.operation_lock import operation_lock
from bridge_cli.runner import Runner


class ServiceManager:
    def __init__(self, layout: Layout, runner: Runner):
        self.layout = layout
        self.runner = runner

    def inspect(self) -> dict[str, Any]:
        result = self.runner.run("systemctl", "is-active", "mcp-session-bridge.service", check=False)
        active = result.returncode == 0 and result.stdout.strip() == "active"
        return {
            "format_version": 1,
            "operation": "service.inspect",
            "state": "complete",
            "service": {"active": active, "unit_exists": self.layout.service_unit.exists()},
        }

    def mutate(self, action: str) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ValueError(f"Unsupported service action: {action}")
        with operation_lock(self.layout.operation_lock_file, self.layout.legacy_operation_lock_file):
            self.runner.run("systemctl", action, "mcp-session-bridge.service")
            result = self.inspect()
        expected_active = action != "stop"
        if bool(result["service"]["active"]) != expected_active:
            raise RuntimeError(f"Bridge service did not reach the expected state after {action}.")
        return {**result, "operation": f"service.{action}", "changed": True}

    def logs(self, lines: int = 100) -> dict[str, Any]:
        result = self.runner.run(
            "journalctl", "-u", "mcp-session-bridge.service", "-n", str(lines),
            "--no-pager", check=False,
        )
        return {
            "format_version": 1,
            "operation": "service.logs",
            "state": "complete" if result.returncode == 0 else "failed",
            "logs": result.stdout or result.stderr,
        }
