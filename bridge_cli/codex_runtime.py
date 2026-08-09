from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridge_cli.codex_contract import CODEX_VERSION
from bridge_cli.files import atomic_write_json, atomic_write_text, read_json
from bridge_cli.layout import Layout
from bridge_cli.runner import Runner
from bridge_cli.system_files import switch_release


CODEX_PACKAGE = "@openai/codex"
CODEX_SERVICE_USER = "mcp-session-bridge-codex"
CODEX_SOCKET_GROUP = "mcp-session-bridge-codex-socket"
CODEX_SERVICE_UNIT = "mcp-session-bridge-codex.service"


@dataclass(frozen=True)
class CodexRuntimeLock:
    package: str
    version: str
    resolved: str
    integrity: str
    digest: str


def load_runtime_lock(source_root: Path) -> CodexRuntimeLock:
    lock_path = source_root / "deploy/codex-runtime/package-lock.json"
    package_path = source_root / "deploy/codex-runtime/package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        dependency_version = package["dependencies"][CODEX_PACKAGE]
        root_version = lock["packages"][""]["dependencies"][CODEX_PACKAGE]
        entry = lock["packages"]["node_modules/@openai/codex"]
    except (KeyError, OSError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Codex runtime lock metadata is missing or invalid.") from exc
    version = str(entry.get("version", ""))
    resolved = str(entry.get("resolved", ""))
    integrity = str(entry.get("integrity", ""))
    if dependency_version != CODEX_VERSION or root_version != CODEX_VERSION or version != CODEX_VERSION:
        raise RuntimeError(f"Codex runtime must be exactly pinned to {CODEX_VERSION}.")
    if not resolved.endswith(f"/codex-{CODEX_VERSION}.tgz") or not integrity.startswith("sha512-"):
        raise RuntimeError("Codex runtime lock must pin the package tarball and SHA-512 integrity.")
    digest_builder = hashlib.sha256()
    for path in (package_path, lock_path, package_path.parent / "run-app-server.sh"):
        digest_builder.update(path.name.encode("utf-8"))
        digest_builder.update(path.read_bytes())
    digest = digest_builder.hexdigest()
    return CodexRuntimeLock(CODEX_PACKAGE, version, resolved, integrity, digest)


class CodexRuntimeManager:
    def __init__(self, layout: Layout, runner: Runner, source_root: Path):
        self.layout = layout
        self.runner = runner
        self.source_root = source_root.resolve()

    def inspect(self) -> dict[str, Any]:
        persisted = read_json(self.layout.codex_status_file) or {}
        active = self._systemctl_state("is-active", "active")
        enabled = self._systemctl_state("is-enabled", "enabled")
        current_version = self._current_version()
        desired_enabled = bool(persisted.get("desired_enabled", False))
        return {
            "format_version": 1,
            "operation": "codex-runtime.status",
            "state": "complete",
            "runtime": {
                "package": CODEX_PACKAGE,
                "expected_version": CODEX_VERSION,
                "installed_version": current_version,
                "installed": current_version is not None,
                "enabled": desired_enabled,
                "unit_enabled": enabled,
                "active": active,
                "socket": str(self.layout.codex_socket),
                "socket_ready": self.layout.codex_socket.exists(),
                "last_error": persisted.get("error"),
            },
        }

    def enable(self) -> dict[str, Any]:
        try:
            lock = self._stage_locked_runtime_and_unit()
            self.runner.run("systemctl", "enable", "--now", CODEX_SERVICE_UNIT)
            self._require_service_active()
            self._refresh_bridge_socket_membership()
            self._write_status("complete", desired_enabled=True, lock=lock)
        except Exception as exc:
            if self.layout.codex_service_unit.exists():
                self.runner.run(
                    "systemctl", "disable", "--now", CODEX_SERVICE_UNIT, check=False
                )
            self._write_status(
                "failed", desired_enabled=False, error=str(exc), bridge_unchanged=True
            )
            raise
        result = self.inspect()
        result.update(operation="codex-runtime.enable", changed=True)
        result["runtime"]["enabled"] = True
        return result

    def repair(self) -> dict[str, Any]:
        desired_enabled = bool((read_json(self.layout.codex_status_file) or {}).get("desired_enabled"))
        try:
            lock = self._stage_locked_runtime_and_unit()
            if desired_enabled:
                self.runner.run("systemctl", "restart", CODEX_SERVICE_UNIT)
                self._require_service_active()
            self._write_status("complete", desired_enabled=desired_enabled, lock=lock)
        except Exception as exc:
            self._write_status(
                "failed",
                desired_enabled=desired_enabled,
                error=str(exc),
                bridge_unchanged=True,
            )
            raise
        result = self.inspect()
        result.update(operation="codex-runtime.repair", changed=True)
        return result

    def disable(self) -> dict[str, Any]:
        self.runner.run("systemctl", "disable", "--now", CODEX_SERVICE_UNIT, check=False)
        self._write_status("complete", desired_enabled=False)
        result = self.inspect()
        result.update(operation="codex-runtime.disable", changed=True)
        result["runtime"]["enabled"] = False
        return result

    def verify(self) -> dict[str, Any]:
        inspection = self.inspect()
        runtime = inspection["runtime"]
        binary = self.layout.codex_runtime_current / "node_modules/.bin/codex"
        version_result = self.runner.run(str(binary), "--version", check=False)
        version_ok = version_result.returncode == 0 and CODEX_VERSION in version_result.stdout
        service_ok = bool(runtime["active"])
        socket_ok = bool(runtime["socket_ready"])
        ok = version_ok and service_ok and socket_ok
        return {
            "format_version": 1,
            "operation": "codex-runtime.verify",
            "state": "complete" if ok else "failed",
            "runtime": runtime,
            "checks": {
                "version": version_ok,
                "service": service_ok,
                "socket": socket_ok,
            },
        }

    def logs(self, lines: int = 100) -> dict[str, Any]:
        result = self.runner.run(
            "journalctl", "-u", CODEX_SERVICE_UNIT, "-n", str(lines), "--no-pager", check=False
        )
        return {
            "format_version": 1,
            "operation": "codex-runtime.logs",
            "state": "complete" if result.returncode == 0 else "failed",
            "logs": result.stdout or result.stderr,
        }

    def reconcile_after_release(self) -> dict[str, Any]:
        persisted = read_json(self.layout.codex_status_file) or {}
        if not persisted.get("desired_enabled"):
            return {"state": "disabled", "changed": False}
        lock = load_runtime_lock(self.source_root)
        inspection = self.inspect()["runtime"]
        if (
            inspection["installed_version"] == lock.version
            and inspection["active"]
            and inspection["socket_ready"]
            and persisted.get("lock_digest") == lock.digest
            and self.layout.codex_service_unit.exists()
            and self.layout.codex_service_unit.read_text(encoding="utf-8") == self._unit_text()
        ):
            return {"state": "complete", "changed": False, "runtime": inspection}
        try:
            result = self.repair()
        except Exception as exc:
            return {
                "state": "failed_preserved",
                "changed": False,
                "bridge_unchanged": True,
                "error": str(exc),
            }
        return {"state": "complete", "changed": True, "runtime": result["runtime"]}

    def _stage_locked_runtime_and_unit(self) -> CodexRuntimeLock:
        self._ensure_host_runtime()
        lock = self._install_locked_runtime()
        self._prepare_identity_and_state()
        atomic_write_text(self.layout.codex_service_unit, self._unit_text(), 0o644)
        self.runner.run("systemctl", "daemon-reload")
        return lock

    def _install_locked_runtime(self) -> CodexRuntimeLock:
        lock = load_runtime_lock(self.source_root)
        releases = self.layout.codex_runtime_releases
        releases.mkdir(mode=0o755, parents=True, exist_ok=True)
        target = self.layout.codex_runtime_release(lock.version)
        temporary = releases / f".{lock.version}.staging-{os.getpid()}"
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(mode=0o755)
        lock_root = self.source_root / "deploy/codex-runtime"
        shutil.copy2(lock_root / "package.json", temporary / "package.json")
        shutil.copy2(lock_root / "package-lock.json", temporary / "package-lock.json")
        shutil.copy2(lock_root / "run-app-server.sh", temporary / "run-app-server.sh")
        temporary.joinpath("run-app-server.sh").chmod(0o755)
        try:
            self.runner.run(
                "npm", "ci", "--omit=dev", "--ignore-scripts", "--prefix", str(temporary)
            )
            previous = releases / f".{lock.version}.previous-{os.getpid()}"
            if target.exists():
                target.rename(previous)
            try:
                temporary.rename(target)
                switch_release(
                    self.layout.codex_runtime_current,
                    target,
                    temporary_name=".current-codex-runtime",
                )
            except Exception:
                if target.exists():
                    shutil.rmtree(target)
                if previous.exists():
                    previous.rename(target)
                raise
            if previous.exists():
                shutil.rmtree(previous)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return lock

    def _ensure_host_runtime(self) -> None:
        if self.layout.root != Path("/"):
            return
        node = self.runner.run("node", "--version", check=False)
        npm = self.runner.run("npm", "--version", check=False)
        if node.returncode != 0 or npm.returncode != 0:
            self.runner.run("apt-get", "update")
            self.runner.run("apt-get", "install", "-y", "nodejs", "npm")
            node = self.runner.run("node", "--version", check=False)
            npm = self.runner.run("npm", "--version", check=False)
        try:
            major = int(node.stdout.strip().lstrip("v").split(".", 1)[0])
        except (ValueError, IndexError):
            major = 0
        if node.returncode != 0 or npm.returncode != 0 or major < 16:
            raise RuntimeError("Codex runtime requires Node.js 16 or newer and npm.")

    def _prepare_identity_and_state(self) -> None:
        if self.layout.root == Path("/"):
            if self.runner.run("id", "-u", "mcp-session-bridge", check=False).returncode != 0:
                raise RuntimeError("Managed Bridge setup must be active before enabling Codex.")
            if self.runner.run("getent", "group", CODEX_SOCKET_GROUP, check=False).returncode != 0:
                self.runner.run("groupadd", "--system", CODEX_SOCKET_GROUP)
            if self.runner.run("getent", "group", CODEX_SERVICE_USER, check=False).returncode != 0:
                self.runner.run("groupadd", "--system", CODEX_SERVICE_USER)
            if self.runner.run("id", "-u", CODEX_SERVICE_USER, check=False).returncode != 0:
                self.runner.run(
                    "useradd",
                    "--system",
                    "--gid",
                    CODEX_SERVICE_USER,
                    "--home",
                    str(self.layout.codex_state_root),
                    "--shell",
                    "/usr/sbin/nologin",
                    CODEX_SERVICE_USER,
                )
            self.runner.run("usermod", "-a", "-G", CODEX_SOCKET_GROUP, "mcp-session-bridge")
            self.runner.run("usermod", "-a", "-G", CODEX_SOCKET_GROUP, CODEX_SERVICE_USER)
        for path, mode in (
            (self.layout.codex_state_root, 0o750),
            (self.layout.codex_home, 0o700),
            (self.layout.codex_workspace, 0o700),
        ):
            path.mkdir(mode=mode, parents=True, exist_ok=True)
            path.chmod(mode)
        if self.layout.root == Path("/"):
            for path in (
                self.layout.codex_state_root,
                self.layout.codex_home,
                self.layout.codex_workspace,
            ):
                self.runner.run(
                    "chown", f"{CODEX_SERVICE_USER}:{CODEX_SERVICE_USER}", str(path)
                )
        self._record_ownership()

    def _record_ownership(self) -> None:
        ownership = read_json(self.layout.ownership_file) or {
            "format_version": 1,
            "owner": "mcp-session-bridge",
            "paths": [],
        }
        paths = {str(path) for path in ownership.get("paths", []) if isinstance(path, str)}
        paths.update(
            str(path)
            for path in (
                self.layout.codex_runtime_root,
                self.layout.codex_state_root,
                self.layout.codex_service_unit,
                self.layout.codex_status_file,
            )
        )
        ownership["paths"] = sorted(paths)
        ownership["codex_service_user"] = CODEX_SERVICE_USER
        atomic_write_json(self.layout.ownership_file, ownership, 0o640)

    def _write_status(
        self,
        state: str,
        *,
        desired_enabled: bool,
        lock: CodexRuntimeLock | None = None,
        error: str | None = None,
        bridge_unchanged: bool | None = None,
    ) -> None:
        existing = read_json(self.layout.codex_status_file) or {}
        payload = {
            "format_version": 1,
            "state": state,
            "desired_enabled": desired_enabled,
            "package": CODEX_PACKAGE,
            "version": lock.version if lock else existing.get("version", CODEX_VERSION),
            "lock_digest": lock.digest if lock else existing.get("lock_digest"),
            "updated_at": int(time.time()),
            "error": error,
        }
        if bridge_unchanged is not None:
            payload["bridge_unchanged"] = bridge_unchanged
        atomic_write_json(self.layout.codex_status_file, payload, 0o640)

    def _current_version(self) -> str | None:
        if not self.layout.codex_runtime_current.is_symlink():
            return None
        package = self.layout.codex_runtime_current / "node_modules/@openai/codex/package.json"
        if package.exists():
            try:
                return str(json.loads(package.read_text(encoding="utf-8"))["version"])
            except (KeyError, OSError, json.JSONDecodeError):
                return None
        return None

    def _systemctl_state(self, command: str, expected: str) -> bool:
        if not self.layout.codex_service_unit.exists():
            return False
        result = self.runner.run("systemctl", command, CODEX_SERVICE_UNIT, check=False)
        return result.returncode == 0 and result.stdout.strip() == expected

    def _require_service_active(self) -> None:
        if self.layout.root != Path("/"):
            return
        result = self.runner.run("systemctl", "is-active", CODEX_SERVICE_UNIT, check=False)
        if result.returncode != 0 or result.stdout.strip() != "active":
            raise RuntimeError(result.stderr.strip() or "Codex app-server did not become active.")

    def _refresh_bridge_socket_membership(self) -> None:
        if self.layout.root != Path("/"):
            return
        self.runner.run("systemctl", "restart", "mcp-session-bridge.service")
        result = self.runner.run(
            "systemctl", "is-active", "mcp-session-bridge.service", check=False
        )
        if result.returncode != 0 or result.stdout.strip() != "active":
            self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
            raise RuntimeError("Bridge did not recover after refreshing socket-group membership.")

    def _unit_text(self) -> str:
        runtime = self.layout.codex_runtime_current
        state = self.layout.codex_state_root
        socket = self.layout.codex_socket
        return f"""[Unit]
Description=MCP Session Bridge Codex app-server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={CODEX_SERVICE_USER}
Group={CODEX_SERVICE_USER}
SupplementaryGroups={CODEX_SOCKET_GROUP}
WorkingDirectory={self.layout.codex_workspace}
Environment=CODEX_HOME={self.layout.codex_home}
ExecStart={runtime}/run-app-server.sh {socket} {CODEX_SOCKET_GROUP} {runtime}/node_modules/.bin/codex
Restart=on-failure
RestartSec=5
RuntimeDirectory=mcp-session-bridge-codex
RuntimeDirectoryMode=0700
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadOnlyPaths={runtime}
ReadWritePaths={state} {self.layout.codex_runtime_dir}
InaccessiblePaths=-{self.layout.data_root} -{self.layout.etc_root} -{self.layout.current_link} -{self.layout.releases_root}

[Install]
WantedBy=multi-user.target
"""
