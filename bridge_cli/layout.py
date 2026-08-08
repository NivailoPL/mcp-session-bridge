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
    def etc_root(self) -> Path:
        return self._path("etc/mcp-session-bridge")

    @property
    def env_file(self) -> Path:
        return self.etc_root / "bridge.env"

    @property
    def data_root(self) -> Path:
        return self._path("var/lib/mcp-session-bridge")

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
    def operation_file(self) -> Path:
        return self.state_root / "last-operation.json"

    @property
    def backup_root(self) -> Path:
        return self._path("var/backups/mcp-session-bridge")

    @property
    def command_path(self) -> Path:
        return self._path("usr/local/bin/mcp-bridge")

    @property
    def systemd_root(self) -> Path:
        return self._path("etc/systemd/system")

    @property
    def service_unit(self) -> Path:
        return self.systemd_root / "mcp-session-bridge.service"

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
