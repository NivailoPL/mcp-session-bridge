from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bridge_cli.config import read_env_file
from bridge_cli.files import atomic_write_json, read_json
from bridge_cli.layout import Layout
from bridge_cli.runner import Runner
from bridge_cli.status import CheckResult, StatusReport, UpdateStatus
from bridge_cli.version import BRIDGE_VERSION


class StatusCollector:
    def __init__(
        self,
        layout: Layout,
        runner: Runner,
        *,
        probe_http: bool = True,
        deep: bool = False,
    ):
        self.layout = layout
        self.runner = runner
        self.probe_http = probe_http
        self.deep = deep

    def collect(self) -> StatusReport:
        installation = read_json(self.layout.installation_file) or {"mode": "unmanaged"}
        checks = [
            self._installation_check(str(installation.get("mode", "unmanaged"))),
            self._config_check(),
            self._database_check(),
            self._dns_check(installation.get("public_base_url")),
            self._service_check("mcp-session-bridge.service", "service", "Bridge service"),
            self._service_check("caddy.service", "caddy", "Caddy"),
        ]
        if self.probe_http:
            checks.append(self._http_check())
        if self.deep:
            checks.extend(self._deep_checks())
        update = self._update_status(str(installation.get("version") or BRIDGE_VERSION))
        overall = _overall(checks)
        return StatusReport(
            overall=overall,
            version={
                "current": str(installation.get("version") or BRIDGE_VERSION),
                "database_schema": self._schema_version(),
                "release_id": installation.get("release_id"),
                "commit": installation.get("commit"),
            },
            update=update,
            checks=checks,
            installation={
                "mode": installation.get("mode", "unmanaged"),
                "public_base_url": installation.get("public_base_url"),
                "resource_path": installation.get("resource_path", "/mcp"),
                "installed_at": installation.get("installed_at"),
                "source_root": installation.get("source_root"),
            },
            last_operation=read_json(self.layout.operation_file),
        )

    def _installation_check(self, mode: str) -> CheckResult:
        if mode == "managed":
            return CheckResult("installation", "Installation", "pass", "Managed installation is active.")
        if mode == "prepared":
            return CheckResult(
                "installation",
                "Installation",
                "action_required",
                "Managed installation is prepared but not active.",
                "Run mcp-bridge setup and choose Activate installation.",
            )
        return CheckResult(
            "installation",
            "Installation",
            "action_required",
            "Bridge is not managed by the CLI yet.",
            "Run mcp-bridge setup to inspect or adopt the current installation.",
        )

    def write_snapshot(self, report: StatusReport) -> None:
        atomic_write_json(self.layout.status_file, report.to_dict())

    def _config_check(self) -> CheckResult:
        values = read_env_file(self.layout.env_file)
        required = {
            "BRIDGE_PUBLIC_BASE_URL",
            "BRIDGE_OWNER_PASSWORD_HASH",
            "BRIDGE_SECRET_KEY",
            "BRIDGE_DB_PATH",
        }
        missing = sorted(required - values.keys())
        if missing:
            return CheckResult(
                "config", "Configuration", "action_required",
                "Missing required settings: " + ", ".join(missing),
                "Run mcp-bridge setup or mcp-bridge configure.",
            )
        return CheckResult("config", "Configuration", "pass", "Required settings are present.")

    def _database_check(self) -> CheckResult:
        if not self.layout.db_path.exists():
            return CheckResult(
                "database", "Database", "action_required", "Database file is missing.",
                "Run mcp-bridge setup.",
            )
        try:
            with sqlite3.connect(f"file:{self.layout.db_path}?mode=ro", uri=True) as conn:
                result = conn.execute("PRAGMA quick_check").fetchone()
                schema = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()
        except sqlite3.Error as exc:
            return CheckResult("database", "Database", "failed", f"SQLite error: {exc}")
        if result is None or result[0] != "ok":
            return CheckResult("database", "Database", "failed", "SQLite quick_check failed.")
        return CheckResult(
            "database", "Database", "pass",
            f"SQLite is healthy; schema {schema[0] if schema else 'unknown'}.",
        )

    def _schema_version(self) -> int | None:
        if not self.layout.db_path.exists():
            return None
        try:
            with sqlite3.connect(f"file:{self.layout.db_path}?mode=ro", uri=True) as conn:
                row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                return row[0] if row else None
        except sqlite3.Error:
            return None

    def _dns_check(self, public_base_url: object) -> CheckResult:
        hostname = urlparse(str(public_base_url or "")).hostname
        if not hostname:
            return CheckResult(
                "dns", "Public DNS", "not_checked", "Public hostname is not configured."
            )
        try:
            addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return CheckResult(
                "dns",
                "Public DNS",
                "waiting",
                f"{hostname} does not resolve yet.",
                "Point DNS at this server, wait for propagation, then run mcp-bridge status.",
            )
        if not addresses:
            return CheckResult(
                "dns",
                "Public DNS",
                "waiting",
                f"{hostname} has no usable address yet.",
                "Check the DNS record, then run mcp-bridge status.",
            )
        return CheckResult("dns", "Public DNS", "pass", f"{hostname} resolves.")

    def _service_check(self, unit: str, check_id: str, label: str) -> CheckResult:
        if not self.layout.systemd_root.exists():
            return CheckResult(check_id, label, "not_checked", "systemd is outside this test root.")
        result = self.runner.run("systemctl", "is-active", unit, check=False)
        if result.returncode == 0 and result.stdout.strip() == "active":
            return CheckResult(check_id, label, "pass", "active")
        detail = result.stderr.strip() or result.stdout.strip() or "inactive"
        return CheckResult(
            check_id, label, "action_required", detail,
            f"Run systemctl status {unit}.",
        )

    def _http_check(self) -> CheckResult:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8787/healthz", timeout=3) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return CheckResult(
                "local_http", "Local health endpoint", "failed", str(exc),
                "Run mcp-bridge logs and mcp-bridge doctor.",
            )
        if response.status == 200 and payload.get("ok") is True:
            return CheckResult("local_http", "Local health endpoint", "pass", "HTTP 200")
        return CheckResult("local_http", "Local health endpoint", "failed", "Unexpected response.")

    def _deep_checks(self) -> list[CheckResult]:
        usage = shutil.disk_usage(self.layout.data_root if self.layout.data_root.exists() else self.layout.root)
        free_gib = usage.free / (1024 ** 3)
        disk_state = "pass" if free_gib >= 1 else "action_required"
        return [
            CheckResult(
                "disk", "Disk space", disk_state, f"{free_gib:.1f} GiB free",
                None if disk_state == "pass" else "Free at least 1 GiB before updating.",
            )
        ]

    def _update_status(self, current: str) -> UpdateStatus:
        cached = read_json(self.layout.update_file)
        if not cached:
            return UpdateStatus(state="unknown", current=current)
        state = str(cached.get("state", "unknown"))
        if state not in {"current", "available", "unknown"}:
            state = "unknown"
        return UpdateStatus(
            state=state,
            current=current,
            latest=cached.get("latest"),
            last_checked=cached.get("last_checked"),
            release_url=cached.get("release_url"),
            error=cached.get("error"),
        )


def _overall(checks: list[CheckResult]) -> str:
    states = {check.state for check in checks}
    if "failed" in states:
        return "failed"
    if "action_required" in states:
        return "attention"
    if "waiting" in states:
        return "incomplete"
    if states <= {"not_checked", "skipped"}:
        return "unknown"
    return "healthy"
