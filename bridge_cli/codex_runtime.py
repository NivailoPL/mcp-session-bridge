from __future__ import annotations

import hashlib
import asyncio
import json
import os
import shutil
import stat
import time
import urllib.request
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


@dataclass(frozen=True)
class _RuntimeStage:
    lock: CodexRuntimeLock
    target: Path
    previous_current: Path | None
    previous_target: Path | None
    previous_unit: str | None


class CodexBridgeRecoveryError(RuntimeError):
    pass


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
        self._socket_group_created = False

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
                "socket_ready": self._socket_ready(),
                "last_error": persisted.get("error"),
            },
        }

    def enable(self) -> dict[str, Any]:
        stage: _RuntimeStage | None = None
        try:
            stage = self._stage_locked_runtime_and_unit()
            self.runner.run("systemctl", "enable", "--now", CODEX_SERVICE_UNIT)
            self._require_service_active()
            self._refresh_bridge_socket_membership()
            self._write_status("complete", desired_enabled=True, lock=stage.lock)
            self._finalize_stage(stage)
        except Exception as exc:
            if self.layout.codex_service_unit.exists():
                self.runner.run(
                    "systemctl", "disable", "--now", CODEX_SERVICE_UNIT, check=False
                )
            if stage is not None:
                self._rollback_stage(stage)
            self._write_status(
                "failed",
                desired_enabled=False,
                error=str(exc),
                bridge_unchanged=not isinstance(exc, CodexBridgeRecoveryError),
            )
            raise
        result = self.inspect()
        result.update(operation="codex-runtime.enable", changed=True)
        result["runtime"]["enabled"] = True
        return result

    def repair(self) -> dict[str, Any]:
        desired_enabled = bool((read_json(self.layout.codex_status_file) or {}).get("desired_enabled"))
        stage: _RuntimeStage | None = None
        try:
            stage = self._stage_locked_runtime_and_unit()
            if desired_enabled:
                self.runner.run("systemctl", "restart", CODEX_SERVICE_UNIT)
                self._require_service_active()
            self._write_status("complete", desired_enabled=desired_enabled, lock=stage.lock)
            self._finalize_stage(stage)
        except Exception as exc:
            if stage is not None:
                self._rollback_stage(stage)
                if desired_enabled:
                    self.runner.run("systemctl", "restart", CODEX_SERVICE_UNIT, check=False)
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
        protocol_ok = service_ok and self._protocol_ready()
        ok = version_ok and service_ok and protocol_ok
        return {
            "format_version": 1,
            "operation": "codex-runtime.verify",
            "state": "complete" if ok else "failed",
            "runtime": runtime,
            "checks": {
                "version": version_ok,
                "service": service_ok,
                "socket": bool(runtime["socket_ready"]),
                "protocol": protocol_ok,
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
            and self._protocol_ready()
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

    def _stage_locked_runtime_and_unit(self) -> _RuntimeStage:
        self._ensure_host_runtime()
        previous_unit = (
            self.layout.codex_service_unit.read_text(encoding="utf-8")
            if self.layout.codex_service_unit.exists()
            else None
        )
        lock, target, previous_current, previous_target = self._install_locked_runtime()
        stage = _RuntimeStage(lock, target, previous_current, previous_target, previous_unit)
        try:
            self._prepare_identity_and_state()
            atomic_write_text(self.layout.codex_service_unit, self._unit_text(), 0o644)
            self.runner.run("systemctl", "daemon-reload")
        except Exception:
            self._rollback_stage(stage)
            raise
        return stage

    def _install_locked_runtime(self) -> tuple[CodexRuntimeLock, Path, Path | None, Path | None]:
        lock = load_runtime_lock(self.source_root)
        releases = self.layout.codex_runtime_releases
        releases.mkdir(mode=0o755, parents=True, exist_ok=True)
        target = self.layout.codex_runtime_release(lock.version)
        temporary = releases / f".{lock.version}.staging-{os.getpid()}"
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(mode=0o755)
        lock_root = self.source_root / "deploy/codex-runtime"
        previous_current = (
            self.layout.codex_runtime_current.resolve()
            if self.layout.codex_runtime_current.is_symlink()
            else None
        )
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
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return lock, target, previous_current, previous if previous.exists() else None

    def _finalize_stage(self, stage: _RuntimeStage) -> None:
        if stage.previous_target is not None and stage.previous_target.exists():
            shutil.rmtree(stage.previous_target)

    def _rollback_stage(self, stage: _RuntimeStage) -> None:
        if stage.target.exists():
            shutil.rmtree(stage.target)
        if stage.previous_target is not None and stage.previous_target.exists():
            stage.previous_target.rename(stage.target)
        if stage.previous_current is not None and stage.previous_current.exists():
            switch_release(
                self.layout.codex_runtime_current,
                stage.previous_current,
                temporary_name=".current-codex-runtime-rollback",
            )
        else:
            self.layout.codex_runtime_current.unlink(missing_ok=True)
        if stage.previous_unit is None:
            self.layout.codex_service_unit.unlink(missing_ok=True)
        else:
            atomic_write_text(self.layout.codex_service_unit, stage.previous_unit, 0o644)
        self.runner.run("systemctl", "daemon-reload", check=False)

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
                self._socket_group_created = True
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
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise RuntimeError(f"Refusing unsafe Codex state path: {path}")
            else:
                path.mkdir(mode=mode, parents=True)
            path.chmod(mode)
        if self.layout.root == Path("/"):
            self.runner.run(
                "chown", "--no-dereference", f"root:{CODEX_SERVICE_USER}",
                str(self.layout.codex_state_root),
            )
            for path in (self.layout.codex_home, self.layout.codex_workspace):
                self.runner.run(
                    "chown", "--no-dereference",
                    f"{CODEX_SERVICE_USER}:{CODEX_SERVICE_USER}", str(path),
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
        if self._socket_group_created:
            ownership["codex_socket_group_created"] = True
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
        for _ in range(20):
            if self._protocol_ready():
                return
            time.sleep(0.25)
        raise RuntimeError("Codex app-server protocol did not become ready.")

    def _refresh_bridge_socket_membership(self) -> None:
        if self.layout.root != Path("/"):
            return
        restarted = self.runner.run(
            "systemctl", "restart", "mcp-session-bridge.service", check=False
        )
        result = self.runner.run(
            "systemctl", "is-active", "mcp-session-bridge.service", check=False
        )
        if restarted.returncode != 0 or result.returncode != 0 or result.stdout.strip() != "active":
            self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
        if not self._bridge_http_ready():
            self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
            if not self._bridge_http_ready():
                raise CodexBridgeRecoveryError(
                    "Bridge did not recover after refreshing socket-group membership."
                )

    def _socket_ready(self) -> bool:
        try:
            return stat.S_ISSOCK(self.layout.codex_socket.stat().st_mode)
        except OSError:
            return False

    def _protocol_ready(self) -> bool:
        if not self._socket_ready():
            return False
        if self.layout.root != Path("/"):
            return True
        try:
            from app.codex_app_server import CodexAppServerClient

            async def probe() -> bool:
                client = CodexAppServerClient(
                    socket_path=self.layout.codex_socket,
                    workspace_dir=self.layout.codex_workspace,
                    expected_version=CODEX_VERSION,
                    rpc_timeout_seconds=3.0,
                )
                try:
                    await client.status()
                    return True
                finally:
                    await client.close()

            return asyncio.run(probe())
        except Exception:
            return False

    @staticmethod
    def _bridge_http_ready() -> bool:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8787/healthz", timeout=3) as response:
                return response.status == 200
        except Exception:
            return False

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
