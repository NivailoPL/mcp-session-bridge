from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable

from bridge_cli.files import atomic_write_json, read_json
from bridge_cli.layout import Layout
from bridge_cli.runner import Runner


LATEST_RELEASE_URL = (
    "https://api.github.com/repos/NivailoPL/mcp-session-bridge/releases/latest"
)
_STABLE_VERSION = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    release_url: str
    asset_url: str
    digest: str
    notes: str


class ReleaseClient:
    def __init__(
        self,
        *,
        opener: Callable[..., BinaryIO] = urllib.request.urlopen,
        latest_url: str = LATEST_RELEASE_URL,
    ):
        self.opener = opener
        self.latest_url = latest_url

    def latest(self) -> ReleaseInfo:
        request = urllib.request.Request(
            self.latest_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "mcp-session-bridge-updater",
            },
        )
        with self.opener(request, timeout=10) as response:
            payload = json.load(response)
        if payload.get("draft") or payload.get("prerelease"):
            raise RuntimeError("GitHub latest release is not a stable published release.")
        version = _parse_version(str(payload.get("tag_name", "")))
        expected_name = f"mcp-session-bridge-{version}.tar.gz"
        asset = next(
            (item for item in payload.get("assets", []) if item.get("name") == expected_name),
            None,
        )
        if not asset:
            raise RuntimeError(f"Release {version} is missing {expected_name}.")
        digest = str(asset.get("digest") or "")
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise RuntimeError(f"Release {version} asset has no valid GitHub SHA-256 digest.")
        asset_url = str(asset.get("browser_download_url") or "")
        if not asset_url.startswith("https://"):
            raise RuntimeError(f"Release {version} asset has no valid HTTPS download URL.")
        return ReleaseInfo(
            version=version,
            release_url=str(payload.get("html_url") or ""),
            asset_url=asset_url,
            digest=digest.split(":", 1)[1].lower(),
            notes=str(payload.get("body") or ""),
        )

    def download(self, release: ReleaseInfo, destination: Path) -> None:
        request = urllib.request.Request(
            release.asset_url,
            headers={"User-Agent": "mcp-session-bridge-updater"},
        )
        with self.opener(request, timeout=60) as response:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)


def is_newer(candidate: str, current: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


def _parse_version(value: str) -> str:
    match = _STABLE_VERSION.fullmatch(value)
    if not match:
        raise ValueError(f"Not a stable semantic version: {value}")
    return ".".join(match.groups())


def _version_tuple(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in _parse_version(value).split("."))  # type: ignore[return-value]


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"Release archive contains unsafe path: {member.name}")
            if member.issym() or member.islnk():
                raise RuntimeError(f"Release archive contains unsupported link: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise RuntimeError(f"Release archive contains unsupported entry: {member.name}")
        tar.extractall(destination, filter="data")


class UpdateManager:
    def __init__(self, layout: Layout, runner: Runner, client: Any | None = None):
        self.layout = layout
        self.runner = runner
        self.client = client or ReleaseClient()

    def check(self, *, force: bool = False) -> dict[str, Any]:
        installation = self._installation()
        current = str(installation["version"])
        cached = read_json(self.layout.update_file)
        if not force and cached and int(cached.get("checked_at", 0)) > time.time() - 21600:
            return cached
        try:
            release = self.client.latest()
            state = "available" if is_newer(release.version, current) else "current"
            payload = {
                "format_version": 1,
                "state": state,
                "current": current,
                "latest": release.version,
                "last_checked": _now_iso(),
                "checked_at": int(time.time()),
                "release_url": release.release_url,
            }
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            payload = dict(cached or {})
            payload.update(
                {
                    "format_version": 1,
                    "state": payload.get("state", "unknown"),
                    "current": current,
                    "last_checked": _now_iso(),
                    "checked_at": int(time.time()),
                    "error": str(exc),
                }
            )
        atomic_write_json(self.layout.update_file, payload)
        return payload

    def update(self, release: ReleaseInfo | None = None) -> dict[str, Any]:
        with self.operation_lock():
            installation = self._installation()
            previous_version = str(installation["version"])
            release = release or self.client.latest()
            if not is_newer(release.version, previous_version):
                return {"ok": True, "state": "current", "version": previous_version}
            self.layout.releases_root.mkdir(mode=0o755, parents=True, exist_ok=True)
            self.layout.backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            previous_release = self.layout.current_link.resolve()
            backup_dir = self._new_backup_dir("update")
            database_backup = backup_dir / "bridge.sqlite3"
            stopped = False
            live_backup_ready = False
            try:
                release_dir = self._prepare_release(release)
                preflight_db = backup_dir / "preflight.sqlite3"
                self._sqlite_backup(self.layout.db_path, preflight_db)
                self.runner.run(
                    str(release_dir / ".venv/bin/python"), "-m", "bridge_cli",
                    "migrate", "--db", str(preflight_db),
                )
                self.runner.run("systemctl", "stop", "mcp-session-bridge.service")
                stopped = True
                self._sqlite_backup(self.layout.db_path, database_backup)
                live_backup_ready = True
                self.runner.run(
                    str(release_dir / ".venv/bin/python"), "-m", "bridge_cli",
                    "migrate", "--db", str(self.layout.db_path),
                )
                self._switch_release(release_dir)
                self.runner.run("systemctl", "start", "mcp-session-bridge.service")
                active = self.runner.run(
                    "systemctl", "is-active", "mcp-session-bridge.service", check=False
                )
                if active.returncode != 0:
                    raise RuntimeError(active.stderr.strip() or "Bridge did not become active.")
                if self.layout.root == Path("/"):
                    self._verify_http(installation)
                installation["version"] = release.version
                installation["updated_at"] = int(time.time())
                atomic_write_json(self.layout.installation_file, installation)
                receipt = {
                    "format_version": 1,
                    "operation": "update",
                    "state": "complete",
                    "previous_version": previous_version,
                    "version": release.version,
                    "previous_release": str(previous_release),
                    "database_backup": str(database_backup),
                    "finished_at": int(time.time()),
                }
                atomic_write_json(self.layout.operation_file, receipt)
                atomic_write_json(
                    self.layout.update_file,
                    {
                        "format_version": 1,
                        "state": "current",
                        "current": release.version,
                        "latest": release.version,
                        "last_checked": _now_iso(),
                        "checked_at": int(time.time()),
                        "release_url": release.release_url,
                    },
                )
                return {"ok": True, **receipt}
            except Exception as exc:
                if stopped:
                    self.runner.run("systemctl", "stop", "mcp-session-bridge.service", check=False)
                    self._switch_release(previous_release)
                    if live_backup_ready:
                        self._restore_database(database_backup)
                    self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
                failed = {
                    "format_version": 1,
                    "operation": "update",
                    "state": "failed_rolled_back" if stopped else "failed",
                    "previous_version": previous_version,
                    "version": release.version,
                    "error": str(exc),
                    "finished_at": int(time.time()),
                }
                atomic_write_json(self.layout.operation_file, failed)
                if stopped:
                    raise RuntimeError(f"Update failed and was rolled back: {exc}") from exc
                raise

    def rollback(self) -> dict[str, Any]:
        with self.operation_lock():
            receipt = read_json(self.layout.operation_file)
            if not receipt or receipt.get("operation") != "update" or receipt.get("state") != "complete":
                raise RuntimeError("No completed update receipt is available for rollback.")
            previous_release = Path(str(receipt["previous_release"]))
            database_backup = Path(str(receipt["database_backup"]))
            if not previous_release.exists() or not database_backup.exists():
                raise RuntimeError("Rollback release or database backup is missing.")
            current_release = self.layout.current_link.resolve()
            safety_dir = self._new_backup_dir("rollback-safety")
            current_database_backup = safety_dir / "bridge.sqlite3"
            self._sqlite_backup(self.layout.db_path, current_database_backup)
            self.runner.run("systemctl", "stop", "mcp-session-bridge.service")
            try:
                self._switch_release(previous_release)
                self._restore_database(database_backup)
                self.runner.run("systemctl", "start", "mcp-session-bridge.service")
                active = self.runner.run(
                    "systemctl", "is-active", "mcp-session-bridge.service", check=False
                )
                if active.returncode != 0:
                    raise RuntimeError(active.stderr.strip() or "Bridge did not become active.")
                if self.layout.root == Path("/"):
                    self._verify_http(self._installation())
            except Exception as exc:
                self.runner.run("systemctl", "stop", "mcp-session-bridge.service", check=False)
                self._switch_release(current_release)
                self._restore_database(current_database_backup)
                self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
                raise RuntimeError(
                    f"Rollback failed; the current release and database were restored: {exc}"
                ) from exc
            installation = self._installation()
            installation["version"] = str(receipt["previous_version"])
            installation["updated_at"] = int(time.time())
            atomic_write_json(self.layout.installation_file, installation)
            rollback_receipt = {
                "format_version": 1,
                "operation": "rollback",
                "state": "complete",
                "previous_version": receipt["version"],
                "version": receipt["previous_version"],
                "database_backup": str(database_backup),
                "finished_at": int(time.time()),
            }
            atomic_write_json(self.layout.operation_file, rollback_receipt)
            return {"ok": True, **rollback_receipt}

    def _prepare_release(self, release: ReleaseInfo) -> Path:
        final = self.layout.release_dir(release.version)
        if final.exists():
            return final
        with tempfile.TemporaryDirectory(prefix=".bridge-update-", dir=self.layout.releases_root) as raw:
            temporary = Path(raw)
            archive = temporary / "release.tar.gz"
            extracted = temporary / "extracted"
            self.client.download(release, archive)
            digest = _sha256_file(archive)
            if not hmac.compare_digest(digest, release.digest):
                raise RuntimeError("Release SHA-256 does not match GitHub asset digest.")
            safe_extract(archive, extracted)
            root = extracted
            children = list(extracted.iterdir())
            if not (root / "pyproject.toml").exists() and len(children) == 1 and children[0].is_dir():
                root = children[0]
            project = root / "pyproject.toml"
            if not project.exists():
                raise RuntimeError("Release archive is missing pyproject.toml.")
            with project.open("rb") as handle:
                project_version = str(tomllib.load(handle).get("project", {}).get("version", ""))
            if project_version != release.version:
                raise RuntimeError(
                    f"Release version mismatch: asset says {project_version}, tag says {release.version}."
                )
            staging = final.with_name(f".{release.version}.staging-{os.getpid()}")
            if staging.exists():
                shutil.rmtree(staging)
            shutil.copytree(root, staging)
            self.runner.run("uv", "sync", "--frozen", "--no-dev", "--project", str(staging))
            staging.rename(final)
        return final

    def _installation(self) -> dict[str, Any]:
        installation = read_json(self.layout.installation_file)
        if not installation or installation.get("mode") != "managed":
            raise RuntimeError("Managed installation metadata is missing. Run bridge setup first.")
        return installation

    def _switch_release(self, release: Path) -> None:
        temporary = self.layout.current_link.with_name(".current-update")
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        temporary.symlink_to(release)
        temporary.replace(self.layout.current_link)

    def _new_backup_dir(self, operation: str) -> Path:
        path = self.layout.backup_root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{operation}"
        suffix = 1
        while path.exists():
            path = path.with_name(f"{path.name}-{suffix}")
            suffix += 1
        path.mkdir(mode=0o700, parents=True)
        return path

    @staticmethod
    def _sqlite_backup(source: Path, destination: Path) -> None:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_conn:
            with sqlite3.connect(destination) as destination_conn:
                source_conn.backup(destination_conn)
        destination.chmod(0o600)

    def _restore_database(self, backup: Path) -> None:
        for suffix in ("", "-wal", "-shm"):
            target = Path(f"{self.layout.db_path}{suffix}")
            if target.exists():
                target.unlink()
        self._sqlite_backup(backup, self.layout.db_path)

    @staticmethod
    def _verify_http(installation: dict[str, Any]) -> None:
        public_base_url = str(installation.get("public_base_url") or "").rstrip("/")
        urls = ["http://127.0.0.1:8787/healthz"]
        if public_base_url:
            urls.extend(
                [
                    f"{public_base_url}/healthz",
                    f"{public_base_url}/.well-known/oauth-authorization-server",
                ]
            )
        for url in urls:
            last_error: Exception | None = None
            for _ in range(10):
                try:
                    with urllib.request.urlopen(url, timeout=5) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError) as exc:
                    last_error = exc
                time.sleep(1)
            else:
                raise RuntimeError(f"Post-update verification failed for {url}: {last_error}")

    @contextmanager
    def operation_lock(self):
        self.layout.state_root.mkdir(mode=0o750, parents=True, exist_ok=True)
        lock_path = self.layout.state_root / "operations.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another Bridge setup, update, or rollback is running.") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_release_command(args: Any, layout: Layout, runner: Runner) -> int:
    manager = UpdateManager(layout, runner)
    if args.command == "rollback":
        confirmation = input("Rollback to the release and database before the last update? [y/N]: ")
        if confirmation.strip().lower() != "y":
            print("Rollback cancelled.")
            return 0
        result = manager.rollback()
        print(f"PASS Rolled back to Bridge {result['version']}.")
        return 0

    status = manager.check(force=bool(args.check))
    if args.check or status.get("state") != "available":
        if status.get("state") == "available":
            print(f"UPDATE AVAILABLE: {status.get('current')} -> {status.get('latest')}")
        elif status.get("state") == "current":
            print(f"PASS Bridge {status.get('current')} is current.")
        else:
            print(f"NOT CHECKED: {status.get('error', 'update state is unknown')}")
        return 0 if status.get("state") != "unknown" else 1

    release = manager.client.latest()
    print(f"Update available: {status.get('current')} -> {release.version}")
    if release.notes:
        print(release.notes[:2000])
    if input("Install this update now? [y/N]: ").strip().lower() != "y":
        print("Update cancelled.")
        return 0
    result = manager.update(release)
    print(f"PASS Updated Bridge to {result['version']}.")
    return 0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
