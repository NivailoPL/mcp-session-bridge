from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Layout:
    root: Path

    @classmethod
    def system(cls) -> "Layout":
        return cls(Path("/"))

    @classmethod
    def for_root(cls, root: Path) -> "Layout":
        return cls(root.resolve())

    def _path(self, relative: str) -> Path:
        return self.root / relative

    @property
    def opt_root(self) -> Path:
        return self._path("opt/mcp-session-bridge")

    @property
    def releases_root(self) -> Path:
        return self.opt_root / "releases"

    def release_dir(self, version: str) -> Path:
        return self.releases_root / version

    @property
    def current_link(self) -> Path:
        return self.opt_root / "current"

    @property
    def codex_runtime_root(self) -> Path:
        return self.opt_root / "codex-runtime"

    @property
    def codex_runtime_releases(self) -> Path:
        return self.codex_runtime_root / "releases"

    def codex_runtime_release(self, version: str) -> Path:
        return self.codex_runtime_releases / version

    @property
    def codex_runtime_current(self) -> Path:
        return self.codex_runtime_root / "current"

    @property
    def etc_root(self) -> Path:
        return self._path("etc/mcp-session-bridge")

    @property
    def env_file(self) -> Path:
        return self.etc_root / "bridge.env"

    @property
    def data_root(self) -> Path:
        return self._path("var/lib/mcp-session-bridge")

    @property
    def codex_state_root(self) -> Path:
        return self._path("var/lib/mcp-session-bridge-codex")

    @property
    def codex_home(self) -> Path:
        return self.codex_state_root / "codex-home"

    @property
    def codex_workspace(self) -> Path:
        return self.codex_state_root / "workspace"

    @property
    def codex_status_file(self) -> Path:
        return self.state_root / "codex-runtime.json"

    @property
    def db_path(self) -> Path:
        return self.data_root / "bridge.sqlite3"

    @property
    def context_packs_dir(self) -> Path:
        return self.data_root / "context-packs"

    @property
    def state_root(self) -> Path:
        return self.data_root / "state"

    @property
    def installation_file(self) -> Path:
        return self.state_root / "installation.json"

    @property
    def status_file(self) -> Path:
        return self.state_root / "status.json"

    @property
    def update_file(self) -> Path:
        return self.state_root / "update.json"

    @property
    def database_status_file(self) -> Path:
        return self.state_root / "database-status.json"

    @property
    def operation_file(self) -> Path:
        return self.state_root / "last-operation.json"

    @property
    def setup_file(self) -> Path:
        return self.state_root / "setup.json"

    @property
    def pending_root(self) -> Path:
        return self.state_root / "pending"

    @property
    def pending_env_file(self) -> Path:
        return self.pending_root / "bridge.env"

    @property
    def pending_service_unit(self) -> Path:
        return self.pending_root / "mcp-session-bridge.service"

    @property
    def pending_restart_path_unit(self) -> Path:
        return self.pending_root / "mcp-session-bridge-restart.path"

    @property
    def pending_restart_service_unit(self) -> Path:
        return self.pending_root / "mcp-session-bridge-restart.service"

    @property
    def pending_status_service_unit(self) -> Path:
        return self.pending_root / "mcp-session-bridge-status.service"

    @property
    def pending_status_timer_unit(self) -> Path:
        return self.pending_root / "mcp-session-bridge-status.timer"

    @property
    def pending_caddy_fragment(self) -> Path:
        return self.pending_root / "mcp-session-bridge.caddy"

    @property
    def pending_launcher(self) -> Path:
        return self.pending_root / "mcp-bridge"

    @property
    def backup_root(self) -> Path:
        return self._path("var/backups/mcp-session-bridge")

    @property
    def command_path(self) -> Path:
        return self._path("usr/local/bin/mcp-bridge")

    @property
    def operation_lock_file(self) -> Path:
        return self._path("run/lock/mcp-session-bridge/operations.lock")

    @property
    def legacy_operation_lock_file(self) -> Path:
        return self.state_root / "operations.lock"

    @property
    def ownership_file(self) -> Path:
        return self.state_root / "ownership.json"

    @property
    def systemd_root(self) -> Path:
        return self._path("etc/systemd/system")

    @property
    def service_unit(self) -> Path:
        return self.systemd_root / "mcp-session-bridge.service"

    @property
    def codex_service_unit(self) -> Path:
        return self.systemd_root / "mcp-session-bridge-codex.service"

    @property
    def codex_runtime_dir(self) -> Path:
        return self._path("run/mcp-session-bridge-codex")

    @property
    def codex_socket(self) -> Path:
        return self.codex_runtime_dir / "app-server.sock"

    @property
    def restart_path_unit(self) -> Path:
        return self.systemd_root / "mcp-session-bridge-restart.path"

    @property
    def restart_service_unit(self) -> Path:
        return self.systemd_root / "mcp-session-bridge-restart.service"

    @property
    def status_service_unit(self) -> Path:
        return self.systemd_root / "mcp-session-bridge-status.service"

    @property
    def status_timer_unit(self) -> Path:
        return self.systemd_root / "mcp-session-bridge-status.timer"

    @property
    def caddy_root(self) -> Path:
        return self._path("etc/caddy")

    @property
    def caddyfile(self) -> Path:
        return self.caddy_root / "Caddyfile"

    @property
    def caddy_fragment(self) -> Path:
        return self.caddy_root / "conf.d/mcp-session-bridge.caddy"
