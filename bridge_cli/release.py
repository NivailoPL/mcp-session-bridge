from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable

from bridge_cli.files import atomic_write_json, read_json
from bridge_cli.layout import Layout
from bridge_cli.operation_lock import operation_lock as shared_operation_lock
from bridge_cli.runner import Runner
from bridge_cli.system_files import sha256_file, sqlite_backup, switch_release


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


@dataclass(frozen=True)
class CheckoutPlan:
    source_root: Path
    version: str
    commit: str
    branch: str
    release_id: str
    previous_release: Path
    already_active: bool
    untracked_files: tuple[str, ...]


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
            release_dir = self._prepare_release(release)
            result = self._activate_prepared_release(
                installation,
                release_dir,
                operation="update",
                target_version=release.version,
                installation_updates={"version": release.version, "updated_at": int(time.time())},
            )
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
            return result

    def rollback(self) -> dict[str, Any]:
        with self.operation_lock():
            receipt = read_json(self.layout.operation_file)
            if (
                not receipt
                or receipt.get("operation") not in {"update", "deploy"}
                or receipt.get("state") != "complete"
            ):
                raise RuntimeError("No completed update or deploy receipt is available for rollback.")
            previous_release = Path(str(receipt["previous_release"]))
            database_backup = Path(str(receipt["database_backup"]))
            if not previous_release.exists() or not database_backup.exists():
                raise RuntimeError("Rollback release or database backup is missing.")
            current_release = self.layout.current_link.resolve()
            safety_dir = self._new_backup_dir("rollback-safety")
            current_database_backup = safety_dir / "bridge.sqlite3"
            sqlite_backup(self.layout.db_path, current_database_backup)
            self.runner.run("systemctl", "stop", "mcp-session-bridge.service")
            try:
                switch_release(self.layout.current_link, previous_release, temporary_name=".current-update")
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
                switch_release(self.layout.current_link, current_release, temporary_name=".current-update")
                self._restore_database(current_database_backup)
                self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
                raise RuntimeError(
                    f"Rollback failed; the current release and database were restored: {exc}"
                ) from exc
            installation = self._installation()
            installation["version"] = str(receipt["previous_version"])
            if receipt.get("operation") == "deploy":
                _restore_optional_field(installation, "commit", receipt.get("previous_commit"))
                _restore_optional_field(
                    installation, "release_id", receipt.get("previous_release_id")
                )
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

    def _activate_prepared_release(
        self,
        installation: dict[str, Any],
        release_dir: Path,
        *,
        operation: str,
        target_version: str,
        installation_updates: dict[str, Any],
        receipt_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous_version = str(installation["version"])
        previous_release = self.layout.current_link.resolve()
        previous_commit = installation.get("commit")
        previous_release_id = installation.get("release_id")
        backup_dir = self._new_backup_dir(operation)
        database_backup = backup_dir / "bridge.sqlite3"
        stopped = False
        live_backup_ready = False
        try:
            preflight_db = backup_dir / "preflight.sqlite3"
            sqlite_backup(self.layout.db_path, preflight_db)
            self.runner.run(
                str(release_dir / ".venv/bin/python"), "-m", "bridge_cli",
                "migrate", "--db", str(preflight_db),
            )
            self.runner.run("systemctl", "stop", "mcp-session-bridge.service")
            stopped = True
            sqlite_backup(self.layout.db_path, database_backup)
            live_backup_ready = True
            self.runner.run(
                str(release_dir / ".venv/bin/python"), "-m", "bridge_cli",
                "migrate", "--db", str(self.layout.db_path),
            )
            switch_release(
                self.layout.current_link,
                release_dir,
                temporary_name=f".current-{operation}",
            )
            self.runner.run("systemctl", "start", "mcp-session-bridge.service")
            active = self.runner.run(
                "systemctl", "is-active", "mcp-session-bridge.service", check=False
            )
            if active.returncode != 0:
                raise RuntimeError(active.stderr.strip() or "Bridge did not become active.")
            if self.layout.root == Path("/"):
                self._verify_http(installation)
            installation.update(installation_updates)
            atomic_write_json(self.layout.installation_file, installation)
            receipt = {
                "format_version": 1,
                "operation": operation,
                "state": "complete",
                "previous_version": previous_version,
                "version": target_version,
                "previous_release": str(previous_release),
                "previous_commit": previous_commit,
                "previous_release_id": previous_release_id,
                "database_backup": str(database_backup),
                "finished_at": int(time.time()),
                **(receipt_fields or {}),
            }
            atomic_write_json(self.layout.operation_file, receipt)
            return {"ok": True, **receipt}
        except Exception as exc:
            if stopped:
                self.runner.run("systemctl", "stop", "mcp-session-bridge.service", check=False)
                switch_release(
                    self.layout.current_link,
                    previous_release,
                    temporary_name=f".current-{operation}",
                )
                if live_backup_ready:
                    self._restore_database(database_backup)
                self.runner.run("systemctl", "start", "mcp-session-bridge.service", check=False)
            failed = {
                "format_version": 1,
                "operation": operation,
                "state": "failed_rolled_back" if stopped else "failed",
                "previous_version": previous_version,
                "version": target_version,
                "error": str(exc),
                "finished_at": int(time.time()),
            }
            atomic_write_json(self.layout.operation_file, failed)
            if stopped:
                label = "Update" if operation == "update" else "Deploy"
                raise RuntimeError(f"{label} failed and was rolled back: {exc}") from exc
            raise

    def _prepare_release(self, release: ReleaseInfo) -> Path:
        final = self.layout.release_dir(release.version)
        if final.exists():
            return final
        with tempfile.TemporaryDirectory(prefix=".bridge-update-", dir=self.layout.releases_root) as raw:
            temporary = Path(raw)
            archive = temporary / "release.tar.gz"
            extracted = temporary / "extracted"
            self.client.download(release, archive)
            digest = sha256_file(archive)
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
            raise RuntimeError("Managed installation metadata is missing. Run mcp-bridge setup first.")
        return installation

    def _new_backup_dir(self, operation: str) -> Path:
        path = self.layout.backup_root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{operation}"
        suffix = 1
        while path.exists():
            path = path.with_name(f"{path.name}-{suffix}")
            suffix += 1
        path.mkdir(mode=0o700, parents=True)
        return path

    def _restore_database(self, backup: Path) -> None:
        for suffix in ("", "-wal", "-shm"):
            target = Path(f"{self.layout.db_path}{suffix}")
            if target.exists():
                target.unlink()
        sqlite_backup(backup, self.layout.db_path)

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

    def operation_lock(self):
        return shared_operation_lock(
            self.layout.operation_lock_file, self.layout.legacy_operation_lock_file
        )


class CheckoutDeployer(UpdateManager):
    def plan(self, source_root: Path) -> CheckoutPlan:
        source_root = source_root.expanduser().resolve()
        project = source_root / "pyproject.toml"
        if not project.exists():
            raise RuntimeError(f"Checkout is missing pyproject.toml: {source_root}")
        top_level = self.runner.run(
            "git", "-C", str(source_root), "rev-parse", "--show-toplevel"
        ).stdout.strip()
        if Path(top_level).resolve() != source_root:
            raise RuntimeError(f"Deploy must run from the repository root: {top_level}")
        dirty = self.runner.run(
            "git", "-C", str(source_root), "status", "--porcelain", "--untracked-files=no"
        ).stdout.strip()
        if dirty:
            raise RuntimeError("Checkout has tracked changes. Commit or discard them before deploy.")
        commit = self.runner.run(
            "git", "-C", str(source_root), "rev-parse", "HEAD"
        ).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise RuntimeError("Checkout HEAD is not a valid Git commit.")
        branch = self.runner.run(
            "git", "-C", str(source_root), "branch", "--show-current"
        ).stdout.strip() or "detached"
        untracked_raw = self.runner.run(
            "git", "-C", str(source_root), "ls-files", "--others", "--exclude-standard"
        ).stdout
        untracked = tuple(line for line in untracked_raw.splitlines() if line)
        with project.open("rb") as handle:
            version = str(tomllib.load(handle).get("project", {}).get("version", ""))
        _parse_version(version)
        release_id = f"{version}-git-{commit[:12]}"
        previous_release = self.layout.current_link.resolve()
        return CheckoutPlan(
            source_root=source_root,
            version=version,
            commit=commit,
            branch=branch,
            release_id=release_id,
            previous_release=previous_release,
            already_active=previous_release == self.layout.release_dir(release_id).resolve(),
            untracked_files=untracked,
        )

    def deploy(
        self, source_root: Path, *, expected_commit: str | None = None
    ) -> dict[str, Any]:
        with self.operation_lock():
            plan = self.plan(source_root)
            if expected_commit is not None and plan.commit != expected_commit:
                raise RuntimeError(
                    f"Checkout changed after confirmation: {expected_commit[:12]} -> {plan.commit[:12]}."
                )
            if plan.already_active:
                return {
                    "ok": True,
                    "operation": "deploy",
                    "state": "current",
                    "version": plan.version,
                    "commit": plan.commit,
                    "release_id": plan.release_id,
                }
            installation = self._installation()
            self.layout.releases_root.mkdir(mode=0o755, parents=True, exist_ok=True)
            self.layout.backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            release_dir = self._prepare_checkout(plan)
            return self._activate_prepared_release(
                installation,
                release_dir,
                operation="deploy",
                target_version=plan.version,
                installation_updates={
                    "version": plan.version,
                    "commit": plan.commit,
                    "release_id": plan.release_id,
                    "source_root": str(plan.source_root),
                    "deployed_at": int(time.time()),
                },
                receipt_fields={
                    "commit": plan.commit,
                    "release_id": plan.release_id,
                    "branch": plan.branch,
                    "untracked_files_excluded": len(plan.untracked_files),
                },
            )

    def _prepare_checkout(self, plan: CheckoutPlan) -> Path:
        final = self.layout.release_dir(plan.release_id)
        metadata_file = final / ".bridge-release.json"
        if final.exists():
            metadata = read_json(metadata_file)
            if metadata.get("commit") != plan.commit:
                raise RuntimeError(f"Existing release directory does not match {plan.commit}.")
            return final
        with tempfile.TemporaryDirectory(
            prefix=".bridge-deploy-", dir=self.layout.releases_root
        ) as raw:
            temporary = Path(raw)
            archive = temporary / "checkout.tar.gz"
            extracted = temporary / "checkout"
            self.runner.run(
                "git", "-C", str(plan.source_root), "archive", "--format=tar.gz",
                f"--output={archive}", plan.commit,
            )
            safe_extract(archive, extracted)
            with (extracted / "pyproject.toml").open("rb") as handle:
                archived_version = str(
                    tomllib.load(handle).get("project", {}).get("version", "")
                )
            if archived_version != plan.version:
                raise RuntimeError(
                    f"Archived checkout version changed: {archived_version} != {plan.version}."
                )
            staging = final.with_name(f".{plan.release_id}.staging-{os.getpid()}")
            if staging.exists():
                shutil.rmtree(staging)
            shutil.copytree(extracted, staging)
            try:
                self.runner.run("uv", "sync", "--frozen", "--no-dev", "--project", str(staging))
                atomic_write_json(
                    staging / ".bridge-release.json",
                    {
                        "format_version": 1,
                        "kind": "checkout",
                        "version": plan.version,
                        "commit": plan.commit,
                        "branch": plan.branch,
                        "created_at": int(time.time()),
                    },
                )
                staging.rename(final)
            except Exception:
                if staging.exists():
                    shutil.rmtree(staging)
                raise
        return final


def _restore_optional_field(target: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


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
