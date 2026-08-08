from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe
from typing import Any, Callable
from urllib.parse import urlparse

from app.security import password_hash
from bridge_cli.caddy import has_site
from bridge_cli.config import read_env_file, update_env_file
from bridge_cli.files import atomic_write_json, atomic_write_text, read_json
from bridge_cli.layout import Layout
from bridge_cli.migrations import migrate_database
from bridge_cli.runner import Runner
from bridge_cli.system_files import sqlite_backup, switch_release
from bridge_cli.validation import normalize_domain, validate_owner_updates
from bridge_cli.version import BRIDGE_VERSION


_MANAGED_UNITS = (
    "mcp-session-bridge.service",
    "mcp-session-bridge-restart.path",
    "mcp-session-bridge-status.timer",
)


@dataclass(frozen=True)
class SetupAnswers:
    domain: str
    username: str | None
    password: str | None

    def validate(self) -> None:
        normalize_domain(self.domain)
        validate_owner_updates(self.username, self.password)


class ManagedInstaller:
    def __init__(self, layout: Layout, source_root: Path, runner: Runner):
        self.layout = layout
        self.source_root = source_root.resolve()
        self.runner = runner
        self._directories_prepared = False

    def ensure_global_cli(self) -> str:
        command = self.layout.command_path
        command.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        desired = _bootstrap_launcher(self.source_root)
        if command.exists():
            existing = command.read_text(encoding="utf-8")
            if (
                "# MCP Session Bridge managed launcher" in existing
                or (
                    f"exec {self.layout.current_link}/.venv/bin/python -m bridge_cli" in existing
                )
            ):
                return "managed"
            if "# MCP Session Bridge bootstrap launcher" not in existing:
                raise RuntimeError(
                    f"{command} already exists and is not owned by MCP Session Bridge."
                )
            if existing == desired:
                return "current"
            atomic_write_text(command, desired, 0o755)
            return "updated"
        atomic_write_text(command, desired, 0o755)
        return "installed"

    def install(
        self,
        answers: SetupAnswers,
        *,
        dry_run: bool = False,
        activate: bool = True,
        legacy_db: Path | None = None,
        legacy_env: Path | None = None,
        progress: Callable[[int, int, str, str], None] | None = None,
    ) -> dict[str, Any]:
        version = BRIDGE_VERSION
        steps = [
            "Validate host and answers",
            "Prepare managed directories",
            f"Stage Bridge {version}",
            "Initialize or adopt SQLite database",
            "Stage systemd and Caddy configuration",
            "Activate and verify services" if activate else "Leave services inactive",
        ]
        _report_progress(progress, 1, steps, "RUNNING")
        answers.validate()
        _report_progress(progress, 1, steps, "PASS")
        if dry_run:
            for index in range(2, len(steps) + 1):
                _report_progress(progress, index, steps, "PLANNED")
            return {
                "ok": True,
                "state": "dry_run",
                "version": version,
                "steps": steps,
                "progress_reported": progress is not None,
            }

        _report_progress(progress, 2, steps, "RUNNING")
        self._prepare_directories()
        legacy_db = None if self.layout.db_path.exists() else legacy_db
        legacy_env = None if self.layout.pending_env_file.exists() else legacy_env
        _report_progress(progress, 2, steps, "PASS")
        _report_progress(progress, 3, steps, "RUNNING")
        self.stage_release()
        _report_progress(progress, 3, steps, "PASS")
        _report_progress(progress, 4, steps, "RUNNING")
        self.stage_database(legacy_db)
        _report_progress(progress, 4, steps, "PASS")
        _report_progress(progress, 5, steps, "RUNNING")
        self.configure_domain(answers.domain, legacy_env)
        self.configure_administrator(answers.username, answers.password, legacy_env)
        self.stage_service()
        _report_progress(progress, 5, steps, "PASS")
        _report_progress(progress, 6, steps, "RUNNING")
        state = "prepared"
        next_step = "Run mcp-bridge setup and choose Activate installation."
        backup_dir = None
        if activate:
            activated = self.activate(legacy_db)
            state = str(activated["state"])
            next_step = str(activated["next_step"])
            backup_dir = activated.get("backup_dir")
        _report_progress(progress, 6, steps, "WAITING" if state == "waiting" else "PASS")
        return {
            "ok": True,
            "state": state,
            "next_step": next_step,
            "version": version,
            "steps": steps,
            "progress_reported": progress is not None,
            "backup_dir": str(backup_dir) if backup_dir else None,
        }

    def stage_release(self) -> Path:
        self._prepare_directories()
        release_dir = self._stage_release(BRIDGE_VERSION)
        setup = read_json(self.layout.setup_file) or {"format_version": 1}
        setup["release_dir"] = str(release_dir)
        atomic_write_json(self.layout.setup_file, setup, 0o600)
        return release_dir

    def stage_database(self, legacy_db: Path | None) -> Path:
        self._prepare_directories()
        source = None if self.layout.db_path.exists() else legacy_db
        self._install_database(source)
        return self.layout.db_path

    def configure_domain(self, domain: str, legacy_env: Path | None = None) -> None:
        normalized = normalize_domain(domain)
        self._prepare_directories()
        self._seed_configuration(legacy_env)
        existing = read_env_file(self.layout.pending_env_file)
        update_env_file(
            self.layout.pending_env_file,
            {
                "BRIDGE_PUBLIC_BASE_URL": f"https://{normalized}",
                "BRIDGE_RESOURCE_PATH": "/mcp",
                "BRIDGE_DB_PATH": str(self.layout.db_path),
                "BRIDGE_CONTEXT_PACKS_DIR": str(self.layout.context_packs_dir),
                "BRIDGE_SECRET_KEY": existing.get("BRIDGE_SECRET_KEY") or token_urlsafe(48),
                "BRIDGE_TRANSPORT_ALLOWED_HOSTS": f"127.0.0.1:8787,localhost:8787,{normalized}",
                "BRIDGE_RESTART_REQUEST_FILE": str(self.layout._path("run/mcp-session-bridge/restart-request")),
                "BRIDGE_OPERATIONAL_STATUS_FILE": str(self.layout.status_file),
                "BRIDGE_ALLOW_STARTUP_MIGRATIONS": "false",
            },
        )

    def configure_administrator(
        self,
        username: str | None,
        password: str | None,
        legacy_env: Path | None = None,
    ) -> None:
        validate_owner_updates(username, password)
        self._prepare_directories()
        self._seed_configuration(legacy_env)
        existing = read_env_file(self.layout.pending_env_file)
        updates: dict[str, str] = {}
        if username is not None:
            updates["BRIDGE_OWNER_USERNAME"] = username.strip()
        if password is not None:
            updates["BRIDGE_OWNER_PASSWORD_HASH"] = password_hash(password)
        if not existing.get("BRIDGE_OWNER_USERNAME") and "BRIDGE_OWNER_USERNAME" not in updates:
            raise ValueError("Owner username is not configured.")
        if not existing.get("BRIDGE_OWNER_PASSWORD_HASH") and "BRIDGE_OWNER_PASSWORD_HASH" not in updates:
            raise ValueError("Owner password is not configured.")
        if updates:
            update_env_file(self.layout.pending_env_file, updates)

    def stage_service(self) -> None:
        self._prepare_directories()
        values = read_env_file(self.layout.pending_env_file)
        required = {
            "BRIDGE_PUBLIC_BASE_URL",
            "BRIDGE_OWNER_USERNAME",
            "BRIDGE_OWNER_PASSWORD_HASH",
            "BRIDGE_SECRET_KEY",
        }
        missing = sorted(required - values.keys())
        if missing:
            raise RuntimeError("Setup sections are incomplete: " + ", ".join(missing))
        if not self.layout.db_path.exists():
            raise RuntimeError("Database is not staged. Choose the Database setup step first.")
        if self._staged_release() is None:
            raise RuntimeError("Bridge release is not staged. Choose the Bridge files step first.")
        domain = url_hostname(values["BRIDGE_PUBLIC_BASE_URL"])
        self._write_units(domain)
        existing = read_json(self.layout.installation_file) or {}
        mode = "managed" if existing.get("mode") == "managed" else "prepared"
        atomic_write_json(
            self.layout.installation_file,
            {
                "format_version": 1,
                "mode": mode,
                "version": BRIDGE_VERSION,
                "public_base_url": values["BRIDGE_PUBLIC_BASE_URL"],
                "resource_path": "/mcp",
                "prepared_at": int(time.time()),
                "source_root": str(self.source_root),
            },
        )

    def activate(self, legacy_db: Path | None = None) -> dict[str, Any]:
        installation = read_json(self.layout.installation_file) or {}
        release_dir = self._staged_release()
        if not self.layout.pending_service_unit.exists() or release_dir is None:
            raise RuntimeError("Managed service is not prepared. Complete the setup steps first.")
        values = read_env_file(self.layout.pending_env_file)
        domain = url_hostname(values.get("BRIDGE_PUBLIC_BASE_URL", ""))
        dns_ready = _dns_resolves(domain)
        if installation.get("mode") == "managed":
            legacy_db = None
        backup_dir = self._backup_existing(legacy_db=legacy_db, legacy_env=None)
        try:
            self._activate_services(
                legacy_db=legacy_db,
                domain=domain,
                release_dir=release_dir,
                backup_dir=backup_dir,
                verify_public=dns_ready,
            )
        except Exception as exc:
            try:
                self._restore_activation(backup_dir)
            except Exception as restore_exc:
                atomic_write_json(
                    self.layout.operation_file,
                    {
                        "format_version": 1,
                        "operation": "setup",
                        "state": "failed",
                        "rollback": "failed",
                        "finished_at": int(time.time()),
                        "backup_dir": str(backup_dir),
                    },
                )
                raise RuntimeError(
                    f"Activation failed and automatic restore also failed. Backup: {backup_dir}: {restore_exc}"
                ) from exc
            atomic_write_json(
                self.layout.operation_file,
                {
                    "format_version": 1,
                    "operation": "setup",
                    "state": "failed",
                    "rollback": "restored",
                    "finished_at": int(time.time()),
                    "backup_dir": str(backup_dir),
                },
            )
            raise RuntimeError(f"Activation failed; previous service configuration restored: {exc}") from exc
        state = "complete" if dns_ready else "waiting"
        next_step = "Run mcp-bridge status."
        if state == "waiting":
            next_step = "DNS is not resolving yet. Point the hostname at this server, then run mcp-bridge status."
        installation.update(
            {
                "format_version": 1,
                "mode": "managed",
                "version": BRIDGE_VERSION,
                "public_base_url": values["BRIDGE_PUBLIC_BASE_URL"],
                "resource_path": "/mcp",
                "installed_at": int(time.time()),
                "source_root": str(self.source_root),
                "backup_dir": str(backup_dir) if backup_dir else None,
            }
        )
        atomic_write_json(self.layout.installation_file, installation)
        operation = {
            "format_version": 1,
            "operation": "setup",
            "state": state,
            "version": BRIDGE_VERSION,
            "finished_at": int(time.time()),
            "backup_dir": str(backup_dir) if backup_dir else None,
        }
        atomic_write_json(self.layout.operation_file, operation)
        return {
            "ok": True,
            "state": state,
            "next_step": next_step,
            "version": BRIDGE_VERSION,
            "backup_dir": str(backup_dir) if backup_dir else None,
        }

    def _prepare_directories(self) -> None:
        if self._directories_prepared:
            return
        for path, mode in (
            (self.layout.releases_root, 0o755),
            (self.layout.etc_root, 0o750),
            (self.layout.data_root, 0o750),
            (self.layout.context_packs_dir, 0o750),
            (self.layout.state_root, 0o750),
            (self.layout.pending_root, 0o700),
            (self.layout.backup_root, 0o700),
            (self.layout.caddy_fragment.parent, 0o755),
            (self.layout.systemd_root, 0o755),
            (self.layout.command_path.parent, 0o755),
        ):
            path.mkdir(mode=mode, parents=True, exist_ok=True)
            path.chmod(mode)
        self.runner.run("chown", "-R", "root:root", str(self.layout.state_root))
        self._directories_prepared = True

    def _backup_existing(
        self, *, legacy_db: Path | None, legacy_env: Path | None
    ) -> Path | None:
        backup_dir = self.layout.backup_root / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        suffix = 1
        while backup_dir.exists():
            backup_dir = backup_dir.with_name(f"{backup_dir.name}-{suffix}")
            suffix += 1
        backup_dir.mkdir(mode=0o700, parents=True)
        tracked = [
            self.layout.db_path,
            self.layout.env_file,
            self.layout.service_unit,
            self.layout.restart_path_unit,
            self.layout.restart_service_unit,
            self.layout.status_service_unit,
            self.layout.status_timer_unit,
            self.layout.caddy_fragment,
            self.layout.caddyfile,
            self.layout.command_path,
        ]
        entries: list[dict[str, object]] = []
        for index, path in enumerate(tracked):
            entry: dict[str, object] = {"path": str(path), "existed": path.exists()}
            if path.exists():
                archive_name = f"{index:02d}-{path.name}"
                archive = backup_dir / archive_name
                if path == self.layout.db_path:
                    sqlite_backup(path, archive)
                else:
                    shutil.copy2(path, archive)
                entry["backup"] = archive_name
            entries.append(entry)
        for label, path in (("legacy-database", legacy_db), ("legacy-environment", legacy_env)):
            if path is None or not path.exists() or path in tracked:
                continue
            archive = backup_dir / label
            if path == legacy_db:
                sqlite_backup(path, archive)
            else:
                shutil.copy2(path, archive)
        active = self.runner.run("systemctl", "is-active", "mcp-session-bridge.service", check=False)
        unit_states: dict[str, dict[str, bool]] = {}
        for unit in _MANAGED_UNITS:
            unit_active = self.runner.run("systemctl", "is-active", unit, check=False)
            unit_enabled = self.runner.run("systemctl", "is-enabled", unit, check=False)
            unit_states[unit] = {
                "active": unit_active.returncode == 0 and unit_active.stdout.strip() == "active",
                "enabled": unit_enabled.returncode == 0,
            }
        atomic_write_json(
            backup_dir / "activation-manifest.json",
            {
                "format_version": 1,
                "service_was_active": active.returncode == 0 and active.stdout.strip() == "active",
                "unit_states": unit_states,
                "current_link_target": (
                    str(self.layout.current_link.readlink())
                    if self.layout.current_link.is_symlink()
                    else None
                ),
                "files": entries,
            },
            0o600,
        )
        return backup_dir

    def _stage_release(self, version: str) -> Path:
        release_dir = self.layout.release_dir(version)
        if release_dir.exists():
            return release_dir
        temporary = release_dir.with_name(f".{version}.staging-{os.getpid()}")
        if temporary.exists():
            shutil.rmtree(temporary)
        shutil.copytree(
            self.source_root,
            temporary,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", ".pytest_cache", "__pycache__", ".env", "data",
                "backups", "secrets", "examples/output", "._*"
            ),
        )
        temporary.rename(release_dir)
        return release_dir

    def _install_database(self, legacy_db: Path | None) -> None:
        if legacy_db is not None and legacy_db.exists() and legacy_db.resolve() != self.layout.db_path.resolve():
            sqlite_backup(legacy_db, self.layout.db_path)
        migrate_database(self.layout.db_path)
        self.layout.db_path.chmod(0o600)

    def _seed_configuration(self, legacy_env: Path | None) -> None:
        if not self.layout.pending_env_file.exists():
            source = self.layout.env_file if self.layout.env_file.exists() else legacy_env
            if source is not None and source.exists():
                shutil.copy2(source, self.layout.pending_env_file)
        existing = read_env_file(self.layout.pending_env_file)
        if not existing:
            return
        updates = {
            "BRIDGE_RESOURCE_PATH": "/mcp",
            "BRIDGE_DB_PATH": str(self.layout.db_path),
            "BRIDGE_CONTEXT_PACKS_DIR": str(self.layout.context_packs_dir),
            "BRIDGE_SECRET_KEY": existing.get("BRIDGE_SECRET_KEY") or token_urlsafe(48),
            "BRIDGE_RESTART_REQUEST_FILE": str(self.layout._path("run/mcp-session-bridge/restart-request")),
            "BRIDGE_OPERATIONAL_STATUS_FILE": str(self.layout.status_file),
            "BRIDGE_ALLOW_STARTUP_MIGRATIONS": "false",
        }
        public_url = existing.get("BRIDGE_PUBLIC_BASE_URL")
        if public_url:
            domain = url_hostname(public_url)
            updates["BRIDGE_TRANSPORT_ALLOWED_HOSTS"] = f"127.0.0.1:8787,localhost:8787,{domain}"
        update_env_file(self.layout.pending_env_file, updates)

    def _write_units(self, domain: str) -> None:
        current = self.layout.current_link
        data = self.layout.data_root
        runtime = self.layout._path("run/mcp-session-bridge")
        service = f"""[Unit]
Description=MCP Session Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mcp-session-bridge
Group=mcp-session-bridge
WorkingDirectory={current}
EnvironmentFile={self.layout.env_file}
ExecStart={current}/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8787 --no-access-log
Restart=on-failure
RestartSec=5
RuntimeDirectory=mcp-session-bridge
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={data} {runtime}

[Install]
WantedBy=multi-user.target
"""
        restart_path = f"""[Unit]
Description=Watch for authenticated MCP Session Bridge restart requests

[Path]
PathChanged={runtime}/restart-request

[Install]
WantedBy=multi-user.target
"""
        restart_service = f"""[Unit]
Description=Restart MCP Session Bridge after an authenticated admin request

[Service]
Type=oneshot
ExecStart=/bin/systemctl restart mcp-session-bridge.service
ExecStartPost=/usr/bin/rm -f {runtime}/restart-request
"""
        status_service = f"""[Unit]
Description=Refresh MCP Session Bridge operational status
After=mcp-session-bridge.service

[Service]
Type=oneshot
ExecStart={self.layout.command_path} status --refresh --write-snapshot
"""
        status_timer = """[Unit]
Description=Refresh MCP Session Bridge operational status periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30s
Persistent=true

[Install]
WantedBy=timers.target
"""
        caddy = f"""{domain} {{
    encode zstd gzip
    reverse_proxy 127.0.0.1:8787
}}
"""
        launcher = f"""#!/bin/sh
# MCP Session Bridge managed launcher
set -eu
exec {current}/.venv/bin/python -m bridge_cli "$@"
"""
        atomic_write_text(self.layout.pending_service_unit, service)
        atomic_write_text(self.layout.pending_restart_path_unit, restart_path)
        atomic_write_text(self.layout.pending_restart_service_unit, restart_service)
        atomic_write_text(self.layout.pending_status_service_unit, status_service)
        atomic_write_text(self.layout.pending_status_timer_unit, status_timer)
        atomic_write_text(self.layout.pending_caddy_fragment, caddy)
        atomic_write_text(self.layout.pending_launcher, launcher, 0o755)

    def _activate_services(
        self,
        *,
        legacy_db: Path | None,
        domain: str,
        release_dir: Path,
        backup_dir: Path | None,
        verify_public: bool,
    ) -> None:
        if self.runner.run("id", "-u", "mcp-session-bridge", check=False).returncode != 0:
            self.runner.run(
                "useradd", "--system", "--home", str(self.layout.data_root),
                "--shell", "/usr/sbin/nologin", "mcp-session-bridge"
            )
        if shutil.which("caddy") is None:
            self.runner.run("apt-get", "update")
            self.runner.run("apt-get", "install", "-y", "caddy")
        self.runner.run("uv", "sync", "--frozen", "--no-dev", "--project", str(release_dir))
        self.runner.run(
            "chown", "root:mcp-session-bridge", str(self.layout.data_root)
        )
        self.runner.run("chown", "-R", "root:root", str(self.layout.state_root))
        self.runner.run(
            "chown", "-R", "mcp-session-bridge:mcp-session-bridge",
            str(self.layout.context_packs_dir),
        )
        text = (
            self.layout.caddyfile.read_text(encoding="utf-8")
            if self.layout.caddyfile.exists() else ""
        )
        if not has_site(text, domain):
            shutil.copy2(self.layout.pending_caddy_fragment, self.layout.caddy_fragment)
            import_line = f"import {self.layout.caddy_fragment.parent}/*.caddy"
            if import_line not in text:
                atomic_write_text(self.layout.caddyfile, text.rstrip() + "\n\n" + import_line + "\n")
        self.runner.run("caddy", "validate", "--config", str(self.layout.caddyfile))

        active = self.runner.run("systemctl", "is-active", "mcp-session-bridge.service", check=False)
        if active.returncode == 0 and active.stdout.strip() == "active":
            self.runner.run("systemctl", "stop", "mcp-session-bridge.service")
            self._refresh_database_backup(backup_dir)
        if legacy_db is not None and legacy_db.exists() and legacy_db.resolve() != self.layout.db_path.resolve():
            self._sqlite_backup_atomic(legacy_db, self.layout.db_path)
            migrate_database(self.layout.db_path)

        switch_release(self.layout.current_link, release_dir, temporary_name=".current-setup")

        for staged, live in (
            (self.layout.pending_env_file, self.layout.env_file),
            (self.layout.pending_service_unit, self.layout.service_unit),
            (self.layout.pending_restart_path_unit, self.layout.restart_path_unit),
            (self.layout.pending_restart_service_unit, self.layout.restart_service_unit),
            (self.layout.pending_status_service_unit, self.layout.status_service_unit),
            (self.layout.pending_status_timer_unit, self.layout.status_timer_unit),
            (self.layout.pending_launcher, self.layout.command_path),
        ):
            shutil.copy2(staged, live)
        self.layout.command_path.chmod(0o755)
        self.runner.run("chown", "mcp-session-bridge:mcp-session-bridge", str(self.layout.db_path))
        self.runner.run("systemctl", "daemon-reload")
        self.runner.run(
            "systemctl", "enable",
            "mcp-session-bridge.service",
            "mcp-session-bridge-restart.path",
            "mcp-session-bridge-status.timer",
        )
        self.runner.run("systemctl", "restart", "mcp-session-bridge.service")
        self.runner.run("systemctl", "start", "mcp-session-bridge-restart.path")
        self.runner.run("systemctl", "start", "mcp-session-bridge-status.timer")
        local_health = self.runner.run(
            "curl", "--fail", "--silent", "--show-error", "--retry", "10",
            "--retry-delay", "1", "--connect-timeout", "3", "--max-time", "5",
            "--retry-max-time", "60", "http://127.0.0.1:8787/healthz",
        )
        _require_health_payload(local_health.stdout, "http://127.0.0.1:8787/healthz")
        self.runner.run("systemctl", "reload", "caddy")
        if verify_public:
            public_health = self.runner.run(
                "curl", "--fail", "--silent", "--show-error", "--retry", "10",
                "--retry-delay", "1", "--connect-timeout", "3", "--max-time", "5",
                "--retry-max-time", "60", f"https://{domain}/healthz",
            )
            _require_health_payload(public_health.stdout, f"https://{domain}/healthz")

    def _refresh_database_backup(self, backup_dir: Path | None) -> None:
        if backup_dir is None or not self.layout.db_path.exists():
            return
        manifest = read_json(backup_dir / "activation-manifest.json") or {}
        for entry in manifest.get("files", []):
            if not isinstance(entry, dict) or entry.get("path") != str(self.layout.db_path):
                continue
            backup = entry.get("backup")
            if isinstance(backup, str):
                sqlite_backup(self.layout.db_path, backup_dir / backup)
            return

    def _sqlite_backup_atomic(self, source: Path, destination: Path) -> None:
        temporary = destination.with_name(f".{destination.name}.activation-{os.getpid()}")
        try:
            sqlite_backup(source, temporary)
            temporary.replace(destination)
            destination.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _restore_activation(self, backup_dir: Path | None) -> None:
        if backup_dir is None:
            return
        manifest = read_json(backup_dir / "activation-manifest.json") or {}
        failures: list[str] = []

        def recover(*args: str) -> None:
            result = self.runner.run(*args, check=False)
            if result.returncode != 0:
                failures.append(result.stderr.strip() or " ".join(args))

        unit_paths = {
            "mcp-session-bridge.service": self.layout.service_unit,
            "mcp-session-bridge-restart.path": self.layout.restart_path_unit,
            "mcp-session-bridge-status.timer": self.layout.status_timer_unit,
        }
        present_units = [unit for unit, path in unit_paths.items() if path.exists()]
        for unit in present_units:
            recover("systemctl", "stop", unit)
        if present_units:
            recover("systemctl", "disable", *present_units)
        if self.layout.current_link.exists() or self.layout.current_link.is_symlink():
            self.layout.current_link.unlink()
        current_target = manifest.get("current_link_target")
        if isinstance(current_target, str):
            self.layout.current_link.symlink_to(current_target)
        for entry in manifest.get("files", []):
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            path = Path(entry["path"])
            backup = entry.get("backup")
            if entry.get("existed") and isinstance(backup, str):
                path.parent.mkdir(parents=True, exist_ok=True)
                if path == self.layout.db_path:
                    for suffix in ("", "-wal", "-shm"):
                        Path(f"{path}{suffix}").unlink(missing_ok=True)
                    sqlite_backup(backup_dir / backup, path)
                else:
                    shutil.copy2(backup_dir / backup, path)
            elif not entry.get("existed"):
                path.unlink(missing_ok=True)
        recover("systemctl", "daemon-reload")
        if self.layout.caddyfile.exists():
            recover("caddy", "validate", "--config", str(self.layout.caddyfile))
            recover("systemctl", "reload", "caddy")
        unit_states = manifest.get("unit_states")
        if not isinstance(unit_states, dict):
            unit_states = {
                "mcp-session-bridge.service": {
                    "active": bool(manifest.get("service_was_active")),
                    "enabled": False,
                }
            }
        enabled = [
            unit for unit, state in unit_states.items()
            if isinstance(state, dict) and state.get("enabled")
        ]
        if enabled:
            recover("systemctl", "enable", *enabled)
        for unit, state in unit_states.items():
            if not isinstance(state, dict) or not state.get("active"):
                continue
            action = "restart" if unit == "mcp-session-bridge.service" else "start"
            recover("systemctl", action, unit)
            result = self.runner.run("systemctl", "is-active", unit, check=False)
            if result.returncode != 0 or result.stdout.strip() != "active":
                failures.append(f"{unit} did not return to active state")
        if failures:
            raise RuntimeError("; ".join(failures))

    def _staged_release(self) -> Path | None:
        setup = read_json(self.layout.setup_file) or {}
        raw = setup.get("release_dir")
        if not isinstance(raw, str):
            return None
        release = Path(raw)
        return release if release.is_dir() else None


def _report_progress(
    progress: Callable[[int, int, str, str], None] | None,
    index: int,
    steps: list[str],
    state: str,
) -> None:
    if progress is not None:
        progress(index, len(steps), steps[index - 1], state)


def _dns_resolves(hostname: str) -> bool:
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    return bool(addresses)


def _require_health_payload(raw: str, url: str) -> None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{url} returned invalid health JSON.") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(f"{url} did not return the Bridge health contract.")


def _bootstrap_launcher(source_root: Path) -> str:
    project = shlex.quote(str(source_root))
    return f"""#!/bin/sh
# MCP Session Bridge bootstrap launcher
set -eu
exec uv run --project {project} --frozen python -m bridge_cli "$@"
"""


def url_hostname(public_base_url: str) -> str:
    hostname = urlparse(public_base_url).hostname
    if not hostname:
        raise ValueError("Public Bridge URL is not configured.")
    return hostname
