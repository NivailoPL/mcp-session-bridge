from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bridge_cli.database_probe import verify_writable_database
from bridge_cli.files import atomic_write_json, read_json
from bridge_cli.health import verify_local_health
from bridge_cli.layout import Layout
from bridge_cli.migrations import CURRENT_SCHEMA_VERSION, migrate_database
from bridge_cli.operation_lock import operation_lock
from bridge_cli.runner import Runner
from bridge_cli.service import ServiceManager
from bridge_cli.system_files import remove_sqlite_sidecars, sha256_file, sqlite_backup


REQUIRED_TABLES = frozenset({"schema_migrations", "sessions", "exchanges"})


@dataclass(frozen=True)
class DatabaseInfo:
    path: str
    exists: bool
    healthy: bool
    integrity: str
    schema_version: int | None
    sessions: int
    size_bytes: int
    wal_bytes: int
    shm_bytes: int
    earliest_session_at: int | None
    latest_conversation_at: int | None
    mode: str | None
    error: str | None = None
    last_verified_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DatabaseManager:
    def __init__(self, layout: Layout, runner: Runner):
        self.layout = layout
        self.runner = runner

    def inspect(self, path: Path | None = None) -> DatabaseInfo:
        db_path = path or self.layout.db_path
        if not db_path.exists():
            return DatabaseInfo(
                path=str(db_path), exists=False, healthy=False, integrity="missing",
                schema_version=None, sessions=0, size_bytes=0, wal_bytes=0, shm_bytes=0,
                earliest_session_at=None, latest_conversation_at=None, mode=None,
            )
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
                integrity_row = connection.execute("PRAGMA quick_check").fetchone()
                integrity = str(integrity_row[0]) if integrity_row else "no result"
                tables = {
                    str(row[0])
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                missing = sorted(REQUIRED_TABLES - tables)
                if missing:
                    raise ValueError("not a Bridge database; missing tables: " + ", ".join(missing))
                schema_row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                schema = int(schema_row[0]) if schema_row and schema_row[0] is not None else 0
                if schema > CURRENT_SCHEMA_VERSION:
                    raise ValueError(
                        f"database schema {schema} is newer than supported schema {CURRENT_SCHEMA_VERSION}"
                    )
                sessions = int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
                earliest = connection.execute("SELECT MIN(created_at) FROM sessions").fetchone()[0]
                exchange_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(exchanges)")
                }
                timestamp = (
                    "COALESCE(assistant_created_at, created_at)"
                    if "assistant_created_at" in exchange_columns
                    else "created_at"
                )
                active_only = " WHERE deleted_at IS NULL" if "deleted_at" in exchange_columns else ""
                latest = connection.execute(
                    f"SELECT MAX({timestamp}) FROM exchanges{active_only}"
                ).fetchone()[0]
            mode = oct(db_path.stat().st_mode & 0o777)
            verification = read_json(self.layout.database_status_file) if db_path == self.layout.db_path else None
            return DatabaseInfo(
                path=str(db_path), exists=True, healthy=integrity == "ok", integrity=integrity,
                schema_version=schema, sessions=sessions, size_bytes=db_path.stat().st_size,
                wal_bytes=self._size(Path(f"{db_path}-wal")), shm_bytes=self._size(Path(f"{db_path}-shm")),
                earliest_session_at=int(earliest) if earliest else None,
                latest_conversation_at=int(latest) if latest else None, mode=mode,
                error=None if integrity == "ok" else f"SQLite quick_check: {integrity}",
                last_verified_at=int(verification["verified_at"]) if verification and verification.get("verified_at") else None,
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            return DatabaseInfo(
                path=str(db_path), exists=True, healthy=False, integrity="failed",
                schema_version=None, sessions=0, size_bytes=db_path.stat().st_size,
                wal_bytes=self._size(Path(f"{db_path}-wal")), shm_bytes=self._size(Path(f"{db_path}-shm")),
                earliest_session_at=None, latest_conversation_at=None,
                mode=oct(db_path.stat().st_mode & 0o777), error=str(exc),
            )

    def verify(self, path: Path | None = None, *, writable: bool = True) -> dict[str, Any]:
        info = self.inspect(path)
        if not info.healthy:
            raise RuntimeError(info.error or f"Database verification failed: {info.integrity}")
        target = path or self.layout.db_path
        if writable and target == self.layout.db_path:
            self._verify_runtime_database()
        if target == self.layout.db_path:
            atomic_write_json(
                self.layout.database_status_file,
                {"format_version": 1, "state": "healthy", "verified_at": int(time.time())},
                0o640,
            )
            info = self.inspect(target)
        return {"format_version": 1, "operation": "database.verify", "state": "complete", "database": info.to_dict()}

    def backup(self, destination: Path, source: Path | None = None) -> dict[str, Any]:
        source_path = source or self.layout.db_path
        source_info = self.inspect(source_path)
        if not source_info.healthy:
            raise RuntimeError(source_info.error or "Source database is not healthy.")
        destination = destination.expanduser().resolve()
        if destination.exists():
            if os.path.samefile(source_path, destination):
                raise ValueError("Backup destination is the same file as the source database.")
            raise ValueError(f"Backup destination already exists: {destination}")
        sqlite_backup(source_path, destination)
        copied = self.inspect(destination)
        if not copied.healthy:
            destination.unlink(missing_ok=True)
            raise RuntimeError(copied.error or "Backup verification failed.")
        return {
            "format_version": 1,
            "operation": "database.backup",
            "state": "complete",
            "artifact": {**copied.to_dict(), "sha256": sha256_file(destination), "sensitive": True},
        }

    def import_replace(self, source: Path) -> dict[str, Any]:
        source = source.expanduser().resolve()
        if not source.exists():
            raise ValueError(f"Import source does not exist: {source}")
        if self.layout.db_path.exists() and os.path.samefile(source, self.layout.db_path):
            raise ValueError("Import source and managed database are the same file.")
        source_info = self.inspect(source)
        if not source_info.healthy:
            raise ValueError(source_info.error or "Import source is not a healthy Bridge database.")
        self.layout.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".database-import-", dir=self.layout.data_root) as raw:
            staged = Path(raw) / "bridge.sqlite3"
            sqlite_backup(source, staged)
            migrate_database(staged)
            staged_info = self.inspect(staged)
            if not staged_info.healthy:
                raise RuntimeError(staged_info.error or "Staged database verification failed.")
            with operation_lock(self.layout.operation_lock_file, self.layout.legacy_operation_lock_file):
                return self._replace_staged(staged, source)

    def reset(self) -> dict[str, Any]:
        self.layout.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".database-reset-", dir=self.layout.data_root) as raw:
            staged = Path(raw) / "bridge.sqlite3"
            migrate_database(staged)
            info = self.inspect(staged)
            if not info.healthy:
                raise RuntimeError(info.error or "Empty staged database failed verification.")
            with operation_lock(self.layout.operation_lock_file, self.layout.legacy_operation_lock_file):
                return self._replace_staged(staged, staged, operation="database.reset")

    def optimize(self) -> dict[str, Any]:
        current = self.inspect()
        if not current.healthy:
            raise RuntimeError(current.error or "Managed database is not healthy.")
        self.layout.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".database-optimize-", dir=self.layout.data_root) as raw:
            staged = Path(raw) / "bridge.sqlite3"
            with operation_lock(self.layout.operation_lock_file, self.layout.legacy_operation_lock_file):
                active = self._service_active()
                if active:
                    self.runner.run("systemctl", "stop", "mcp-session-bridge.service")
                backup_dir = self._new_backup_dir("optimize")
                backup = backup_dir / "bridge.sqlite3"
                try:
                    sqlite_backup(self.layout.db_path, backup)
                    sqlite_backup(backup, staged)
                    with sqlite3.connect(staged) as connection:
                        connection.execute("PRAGMA optimize")
                        connection.execute("VACUUM")
                    info = self.inspect(staged)
                    if not info.healthy:
                        raise RuntimeError(info.error or "Optimized staging database failed verification.")
                except Exception:
                    if active:
                        self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
                    raise
                return self._replace_staged(
                    staged,
                    staged,
                    operation="database.optimize",
                    service_was_active=active,
                    existing_backup=backup,
                )

    def migrate(self) -> dict[str, Any]:
        current = self.inspect()
        if not current.exists:
            raise RuntimeError("Managed database is missing.")
        with operation_lock(self.layout.operation_lock_file, self.layout.legacy_operation_lock_file):
            active = self._service_active()
            if active:
                self.runner.run("systemctl", "stop", "mcp-session-bridge.service")
            backup_dir = self._new_backup_dir("migrate")
            backup = backup_dir / "bridge.sqlite3"
            sqlite_backup(self.layout.db_path, backup)
            try:
                migration = migrate_database(self.layout.db_path)
                self._prepare_runtime_database()
                if active:
                    self.runner.run("systemctl", "start", "mcp-session-bridge.service")
                    self._require_active()
                    self._verify_health()
                return {
                    "format_version": 1,
                    "operation": "database.migrate",
                    "state": "complete",
                    "backup": str(backup),
                    **migration,
                }
            except Exception as exc:
                if active:
                    self.runner.run("systemctl", "stop", "mcp-session-bridge.service", check=False)
                remove_sqlite_sidecars(self.layout.db_path)
                sqlite_backup(backup, self.layout.db_path)
                self._prepare_runtime_database()
                if active:
                    self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
                raise RuntimeError(f"Database migration failed; the previous database was restored: {exc}") from exc

    def _replace_staged(
        self,
        staged: Path,
        source: Path,
        *,
        operation: str = "database.import",
        service_was_active: bool | None = None,
        existing_backup: Path | None = None,
    ) -> dict[str, Any]:
        current_info = self.inspect()
        active = self._service_active() if service_was_active is None else service_was_active
        backup = existing_backup
        original_mode = self.layout.db_path.stat().st_mode & 0o777 if self.layout.db_path.exists() else 0o600
        stopped = False
        cutover_started = False
        replacement = self.layout.db_path.with_name(".bridge.sqlite3.replacement")
        try:
            if active and service_was_active is None:
                self.runner.run("systemctl", "stop", "mcp-session-bridge.service")
                stopped = True
            elif active:
                stopped = True
            if self.layout.db_path.exists() and backup is None:
                backup_dir = self._new_backup_dir(operation.rsplit(".", 1)[-1])
                backup = backup_dir / "bridge.sqlite3"
                sqlite_backup(self.layout.db_path, backup)
                if not self.inspect(backup).healthy:
                    raise RuntimeError("Final safety backup failed verification.")
            replacement.unlink(missing_ok=True)
            sqlite_backup(staged, replacement)
            replacement.chmod(0o660 if self.layout.root == Path("/") else original_mode)
            remove_sqlite_sidecars(self.layout.db_path)
            cutover_started = True
            replacement.replace(self.layout.db_path)
            runtime_verified = self._prepare_runtime_database()
            if active:
                self.runner.run("systemctl", "start", "mcp-session-bridge.service")
                self._require_active()
                self._verify_health()
            after = self.inspect()
            if not after.healthy:
                raise RuntimeError(after.error or "Imported database failed verification.")
            atomic_write_json(
                self.layout.database_status_file,
                {"format_version": 1, "state": "healthy", "verified_at": int(time.time())},
                0o640,
            )
            return {
                "format_version": 1, "operation": operation, "state": "complete",
                "source": str(source), "before": current_info.to_dict(),
                "after": after.to_dict(), "backup": str(backup) if backup else None,
                "runtime_verification": "complete" if runtime_verified else "pending_activation",
                "credentials_notice": "Reconnect harnesses and re-enter encrypted provider keys if the installation secret changed.",
            }
        except Exception as exc:
            if cutover_started:
                if active:
                    self.runner.run("systemctl", "stop", "mcp-session-bridge.service", check=False)
                remove_sqlite_sidecars(self.layout.db_path)
                if backup and backup.exists():
                    sqlite_backup(backup, self.layout.db_path)
                    self.layout.db_path.chmod(0o660 if self.layout.root == Path("/") else original_mode)
                    self._prepare_runtime_database()
                else:
                    self.layout.db_path.unlink(missing_ok=True)
                if active:
                    restored = self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
                    if restored.returncode != 0:
                        raise RuntimeError(
                            "Database replacement failed; the previous database was restored, "
                            f"but the service could not be restarted: {restored.stderr.strip() or exc}"
                        ) from exc
                    self._require_active()
                    self._verify_health()
                raise RuntimeError(f"Database replacement failed; the previous database state was restored: {exc}") from exc
            if stopped and active:
                self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
            raise
        finally:
            replacement.unlink(missing_ok=True)

    def _service_active(self) -> bool:
        result = ServiceManager(self.layout, self.runner).inspect()
        return bool(result["service"]["active"])

    def _require_active(self) -> None:
        if not self._service_active():
            raise RuntimeError("Bridge service did not become active.")

    def _prepare_runtime_database(self) -> bool:
        if self.layout.root != Path("/"):
            verify_writable_database(self.layout.db_path)
            return True
        user = self.runner.run("id", "-u", "mcp-session-bridge", check=False)
        if user.returncode != 0 or not self.layout.current_link.exists():
            self.layout.db_path.chmod(0o600)
            return False
        self.runner.run(
            "chown", "mcp-session-bridge:mcp-session-bridge", str(self.layout.db_path)
        )
        self.layout.db_path.chmod(0o660)
        self._verify_runtime_database()
        return True

    def _verify_runtime_database(self) -> None:
        if self.layout.root != Path("/"):
            verify_writable_database(self.layout.db_path)
            return
        if (
            self.runner.run("id", "-u", "mcp-session-bridge", check=False).returncode != 0
            or not self.layout.current_link.exists()
        ):
            raise RuntimeError("Service-user database verification is pending managed activation.")
        release = self.layout.current_link.resolve()
        probe = self.runner.run(
            "runuser", "--user", "mcp-session-bridge", "--",
            str(release / ".venv/bin/python"),
            str(release / "bridge_cli/database_probe.py"),
            str(self.layout.db_path),
            check=False,
        )
        if probe.returncode != 0:
            raise RuntimeError(
                probe.stderr.strip() or probe.stdout.strip() or "Service user cannot write the database."
            )

    def _verify_health(self) -> None:
        if self.layout.root == Path("/"):
            verify_local_health(self.runner)

    def _new_backup_dir(self, label: str) -> Path:
        base = self.layout.backup_root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-database-{label}"
        path = base
        suffix = 1
        while path.exists():
            path = base.with_name(f"{base.name}-{suffix}")
            suffix += 1
        path.mkdir(mode=0o700, parents=True)
        return path

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0
