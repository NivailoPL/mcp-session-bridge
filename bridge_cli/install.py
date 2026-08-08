from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from app.security import password_hash
from bridge_cli.config import read_env_file, update_env_file
from bridge_cli.files import atomic_write_json, atomic_write_text
from bridge_cli.layout import Layout
from bridge_cli.migrations import migrate_database
from bridge_cli.runner import Runner
from bridge_cli.version import BRIDGE_VERSION


@dataclass(frozen=True)
class SetupAnswers:
    domain: str
    username: str
    password: str

    def validate(self) -> None:
        domain = self.domain.strip().lower()
        if not domain or "." not in domain or "://" in domain or "/" in domain:
            raise ValueError("Provide a hostname such as bridge.example.com.")
        if not self.username.strip():
            raise ValueError("Owner username cannot be empty.")
        if len(self.password) < 10:
            raise ValueError("Owner password must contain at least 10 characters.")


class ManagedInstaller:
    def __init__(self, layout: Layout, source_root: Path, runner: Runner):
        self.layout = layout
        self.source_root = source_root.resolve()
        self.runner = runner

    def install(
        self,
        answers: SetupAnswers,
        *,
        dry_run: bool = False,
        activate: bool = True,
        legacy_db: Path | None = None,
        legacy_env: Path | None = None,
    ) -> dict[str, Any]:
        answers.validate()
        version = BRIDGE_VERSION
        steps = [
            "Validate host and answers",
            f"Stage Bridge {version}",
            "Create configuration and data directories",
            "Initialize or adopt SQLite database",
            "Install systemd and Caddy configuration",
            "Activate and verify services" if activate else "Leave services inactive",
        ]
        if dry_run:
            return {"ok": True, "state": "dry_run", "version": version, "steps": steps}

        self._prepare_directories()
        backup_dir = self._backup_existing(legacy_db=legacy_db, legacy_env=legacy_env)
        release_dir = self._stage_release(version)
        self._install_database(legacy_db)
        self._write_configuration(answers, legacy_env)
        self._write_units(answers.domain.strip().lower())
        self._activate_release(release_dir)
        manifest = {
            "format_version": 1,
            "mode": "managed",
            "version": version,
            "public_base_url": f"https://{answers.domain.strip().lower()}",
            "resource_path": "/mcp",
            "installed_at": int(time.time()),
            "source_root": str(self.source_root),
            "backup_dir": str(backup_dir) if backup_dir else None,
        }
        atomic_write_json(self.layout.installation_file, manifest)
        if activate:
            self._activate_services()
        operation = {
            "format_version": 1,
            "operation": "setup",
            "state": "complete",
            "version": version,
            "finished_at": int(time.time()),
            "backup_dir": str(backup_dir) if backup_dir else None,
        }
        atomic_write_json(self.layout.operation_file, operation)
        return {
            "ok": True,
            "state": "complete",
            "version": version,
            "steps": steps,
            "backup_dir": str(backup_dir) if backup_dir else None,
        }

    def _prepare_directories(self) -> None:
        for path, mode in (
            (self.layout.releases_root, 0o755),
            (self.layout.etc_root, 0o750),
            (self.layout.data_root, 0o750),
            (self.layout.context_packs_dir, 0o750),
            (self.layout.state_root, 0o750),
            (self.layout.backup_root, 0o700),
            (self.layout.caddy_fragment.parent, 0o755),
            (self.layout.systemd_root, 0o755),
            (self.layout.command_path.parent, 0o755),
        ):
            path.mkdir(mode=mode, parents=True, exist_ok=True)
            path.chmod(mode)

    def _backup_existing(
        self, *, legacy_db: Path | None, legacy_env: Path | None
    ) -> Path | None:
        candidates = [
            path
            for path in (legacy_db, legacy_env, self.layout.env_file, self.layout.service_unit)
            if path is not None and path.exists()
        ]
        if not candidates:
            return None
        backup_dir = self.layout.backup_root / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        suffix = 1
        while backup_dir.exists():
            backup_dir = backup_dir.with_name(f"{backup_dir.name}-{suffix}")
            suffix += 1
        backup_dir.mkdir(mode=0o700, parents=True)
        for path in candidates:
            if path == legacy_db or path == self.layout.db_path:
                self._sqlite_backup(path, backup_dir / "bridge.sqlite3")
            else:
                shutil.copy2(path, backup_dir / path.name)
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
            self._sqlite_backup(legacy_db, self.layout.db_path)
        migrate_database(self.layout.db_path)
        self.layout.db_path.chmod(0o600)

    @staticmethod
    def _sqlite_backup(source: Path, destination: Path) -> None:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_conn:
            with sqlite3.connect(destination) as destination_conn:
                source_conn.backup(destination_conn)
        destination.chmod(0o600)

    def _write_configuration(self, answers: SetupAnswers, legacy_env: Path | None) -> None:
        if legacy_env is not None and legacy_env.exists() and not self.layout.env_file.exists():
            shutil.copy2(legacy_env, self.layout.env_file)
        domain = answers.domain.strip().lower()
        existing = read_env_file(self.layout.env_file)
        update_env_file(
            self.layout.env_file,
            {
                "BRIDGE_PUBLIC_BASE_URL": f"https://{domain}",
                "BRIDGE_RESOURCE_PATH": "/mcp",
                "BRIDGE_DB_PATH": str(self.layout.db_path),
                "BRIDGE_CONTEXT_PACKS_DIR": str(self.layout.context_packs_dir),
                "BRIDGE_OWNER_USERNAME": answers.username.strip(),
                "BRIDGE_OWNER_PASSWORD_HASH": password_hash(answers.password),
                "BRIDGE_SECRET_KEY": existing.get("BRIDGE_SECRET_KEY") or token_urlsafe(48),
                "BRIDGE_TRANSPORT_ALLOWED_HOSTS": (
                    f"127.0.0.1:8787,localhost:8787,{domain}"
                ),
                "BRIDGE_RESTART_REQUEST_FILE": str(self.layout._path("run/mcp-session-bridge/restart-request")),
                "BRIDGE_ALLOW_STARTUP_MIGRATIONS": "false",
            },
        )

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
        status_service = """[Unit]
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
set -eu
exec {current}/.venv/bin/python -m bridge_cli "$@"
"""
        atomic_write_text(self.layout.service_unit, service)
        atomic_write_text(self.layout.restart_path_unit, restart_path)
        atomic_write_text(self.layout.restart_service_unit, restart_service)
        atomic_write_text(self.layout.status_service_unit, status_service)
        atomic_write_text(self.layout.status_timer_unit, status_timer)
        atomic_write_text(self.layout.caddy_fragment, caddy)
        atomic_write_text(self.layout.command_path, launcher, 0o755)

    def _activate_release(self, release_dir: Path) -> None:
        temporary_link = self.layout.current_link.with_name(".current-new")
        if temporary_link.exists() or temporary_link.is_symlink():
            temporary_link.unlink()
        temporary_link.symlink_to(release_dir)
        temporary_link.replace(self.layout.current_link)

    def _activate_services(self) -> None:
        if self.runner.run("id", "-u", "mcp-session-bridge", check=False).returncode != 0:
            self.runner.run(
                "useradd", "--system", "--home", str(self.layout.data_root),
                "--shell", "/usr/sbin/nologin", "mcp-session-bridge"
            )
        self.runner.run(
            "chown", "-R", "mcp-session-bridge:mcp-session-bridge", str(self.layout.data_root)
        )
        release_dir = self.layout.current_link.resolve()
        if shutil.which("caddy") is None:
            self.runner.run("apt-get", "update")
            self.runner.run("apt-get", "install", "-y", "caddy")
        self.runner.run("uv", "sync", "--frozen", "--no-dev", "--project", str(release_dir))
        self.runner.run("systemctl", "daemon-reload")
        self.runner.run(
            "systemctl", "enable", "--now",
            "mcp-session-bridge.service",
            "mcp-session-bridge-restart.path",
            "mcp-session-bridge-status.timer",
        )
        import_line = f"import {self.layout.caddy_fragment.parent}/*.caddy"
        text = (
            self.layout.caddyfile.read_text(encoding="utf-8")
            if self.layout.caddyfile.exists() else ""
        )
        if import_line not in text:
            atomic_write_text(self.layout.caddyfile, text.rstrip() + "\n\n" + import_line + "\n")
        self.runner.run("caddy", "validate", "--config", str(self.layout.caddyfile))
        self.runner.run("systemctl", "reload", "caddy")
