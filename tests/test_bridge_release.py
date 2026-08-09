from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from app.storage import Store

from bridge_cli.files import atomic_write_json
from bridge_cli.layout import Layout
from bridge_cli.release import (
    CheckoutDeployer,
    ReleaseClient,
    ReleaseInfo,
    UpdateManager,
    is_newer,
    safe_extract,
)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RecordingRunner:
    def __init__(self, fail_on: tuple[str, ...] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_on = fail_on

    def run(self, *args: str, check: bool = True):
        self.calls.append(tuple(args))

        class Result:
            returncode = 1 if self.fail_on and tuple(args[: len(self.fail_on)]) == self.fail_on else 0
            stdout = "active\n" if returncode == 0 else ""
            stderr = "forced failure" if returncode else ""

        result = Result()
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result


class CheckoutRunner(RecordingRunner):
    def run(self, *args: str, check: bool = True):
        if args[0] == "git":
            return subprocess.run(args, check=check, capture_output=True, text=True)
        if args[:2] == ("uv", "sync"):
            project = Path(args[args.index("--project") + 1])
            python = project / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
        return super().run(*args, check=check)


def test_semver_comparison_ignores_v_prefix_and_rejects_prereleases() -> None:
    assert is_newer("0.4.1", "0.4.0") is True
    assert is_newer("v1.0.0", "0.9.9") is True
    assert is_newer("0.4.0", "0.4.0") is False
    with pytest.raises(ValueError):
        is_newer("0.5.0-rc.1", "0.4.0")


def test_release_client_requires_stable_asset_with_github_digest() -> None:
    payload = {
        "tag_name": "v0.4.1",
        "draft": False,
        "prerelease": False,
        "html_url": "https://github.test/releases/v0.4.1",
        "body": "Changes",
        "assets": [
            {
                "name": "mcp-session-bridge-0.4.1.tar.gz",
                "browser_download_url": "https://github.test/bridge.tar.gz",
                "digest": "sha256:" + "a" * 64,
            }
        ],
    }

    client = ReleaseClient(opener=lambda *args, **kwargs: Response(json.dumps(payload).encode()))
    release = client.latest()

    assert release.version == "0.4.1"
    assert release.digest == "a" * 64
    assert release.asset_url.endswith("bridge.tar.gz")


def test_safe_extract_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("../../escape")
        data = b"bad"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    with pytest.raises(RuntimeError, match="unsafe path"):
        safe_extract(archive, tmp_path / "destination")

    assert not (tmp_path / "escape").exists()


def _release_archive(tmp_path: Path, version: str) -> tuple[Path, str]:
    content = tmp_path / "content"
    content.mkdir()
    (content / "pyproject.toml").write_text(
        f"[project]\nname='mcp-session-bridge'\nversion='{version}'\n",
        encoding="utf-8",
    )
    (content / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (content / "bridge_cli").mkdir()
    (content / "bridge_cli/__init__.py").write_text("", encoding="utf-8")
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in content.rglob("*"):
            tar.add(path, arcname=path.relative_to(content))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive, digest


class StubClient:
    def __init__(self, info: ReleaseInfo, archive: Path) -> None:
        self.info = info
        self.archive = archive

    def latest(self) -> ReleaseInfo:
        return self.info

    def download(self, release: ReleaseInfo, destination: Path) -> None:
        shutil.copy2(self.archive, destination)


def _managed_layout(tmp_path: Path) -> Layout:
    layout = Layout.for_root(tmp_path / "root")
    current = layout.release_dir("0.4.0")
    current.mkdir(parents=True)
    (current / "pyproject.toml").write_text(
        "[project]\nname='mcp-session-bridge'\nversion='0.4.0'\n", encoding="utf-8"
    )
    layout.current_link.parent.mkdir(parents=True, exist_ok=True)
    layout.current_link.symlink_to(current)
    layout.data_root.mkdir(parents=True)
    from bridge_cli.migrations import migrate_database

    migrate_database(layout.db_path)
    atomic_write_json(
        layout.installation_file,
        {"format_version": 1, "mode": "managed", "version": "0.4.0"},
    )
    return layout


def _checkout(tmp_path: Path) -> Path:
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        "[project]\nname='mcp-session-bridge'\nversion='0.4.0'\n",
        encoding="utf-8",
    )
    (source / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (source / "bridge_cli").mkdir()
    (source / "bridge_cli/__init__.py").write_text("", encoding="utf-8")
    subprocess.run(("git", "init", "-q", str(source)), check=True)
    subprocess.run(("git", "-C", str(source), "add", "."), check=True)
    subprocess.run(
        (
            "git", "-C", str(source), "-c", "user.name=Test", "-c",
            "user.email=test@example.test", "commit", "-qm", "Initial",
        ),
        check=True,
    )
    return source


def test_checkout_deploy_creates_commit_release_and_switches_current(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    source = _checkout(tmp_path)
    runner = CheckoutRunner()
    manager = CheckoutDeployer(layout, runner)

    plan = manager.plan(source)
    result = manager.deploy(source, expected_commit=plan.commit)

    assert result["state"] == "complete"
    assert result["commit"] == plan.commit
    assert result["release_id"] == f"0.4.0-git-{plan.commit[:12]}"
    assert layout.current_link.resolve() == layout.release_dir(result["release_id"]).resolve()
    installation = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    assert installation["commit"] == plan.commit
    assert installation["release_id"] == result["release_id"]
    assert Path(result["database_backup"]).exists()

    rollback = manager.rollback()

    assert rollback["state"] == "complete"
    assert layout.current_link.resolve() == layout.release_dir("0.4.0").resolve()
    restored = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    assert "commit" not in restored
    assert "release_id" not in restored


def test_checkout_deploy_rejects_tracked_uncommitted_changes(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    source = _checkout(tmp_path)
    (source / "pyproject.toml").write_text("changed\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="tracked changes"):
        CheckoutDeployer(layout, CheckoutRunner()).plan(source)


def test_failed_checkout_deploy_restores_previous_release(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    source = _checkout(tmp_path)
    runner = CheckoutRunner(fail_on=("systemctl", "start"))

    with pytest.raises(RuntimeError, match="rolled back"):
        CheckoutDeployer(layout, runner).deploy(source)

    assert layout.current_link.resolve() == layout.release_dir("0.4.0").resolve()


def test_system_database_migration_runs_as_service_user() -> None:
    runner = RecordingRunner()
    manager = UpdateManager(Layout.system(), runner)
    release = Path("/opt/mcp-session-bridge/releases/test")
    database = Path("/var/lib/mcp-session-bridge/bridge.sqlite3")

    manager._migrate_database(release, database, service_user=True)

    assert runner.calls[-1] == (
        "runuser", "--user", "mcp-session-bridge", "--", "env",
        f"PYTHONPATH={release}", str(release / ".venv/bin/python"),
        "-c",
        (
            "import sys; from pathlib import Path; "
            "from bridge_cli.migrations import migrate_database; "
            "migrate_database(Path(sys.argv[1]))"
        ),
        str(database),
    )


def test_system_database_owner_is_restored_after_backup_copy() -> None:
    runner = RecordingRunner()
    manager = UpdateManager(Layout.system(), runner)
    database = Path("/var/lib/mcp-session-bridge/bridge.sqlite3")

    manager._set_runtime_database_owner(database)

    assert runner.calls == [
        ("chown", "mcp-session-bridge:mcp-session-bridge", str(database)),
        ("chmod", "600", str(database)),
    ]


def test_update_switches_release_and_records_rollback_receipt(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    archive, digest = _release_archive(tmp_path, "0.4.1")
    info = ReleaseInfo(
        version="0.4.1",
        release_url="https://github.test/v0.4.1",
        asset_url="https://github.test/asset",
        digest=digest,
        notes="Changes",
    )
    runner = RecordingRunner()
    manager = UpdateManager(layout, runner, StubClient(info, archive))

    result = manager.update(info)

    assert result["state"] == "complete"
    assert layout.current_link.resolve() == layout.release_dir("0.4.1").resolve()
    receipt = json.loads(layout.operation_file.read_text(encoding="utf-8"))
    assert receipt["previous_version"] == "0.4.0"
    assert Path(receipt["database_backup"]).exists()

    rollback = manager.rollback()
    assert rollback["state"] == "complete"
    assert layout.current_link.resolve() == layout.release_dir("0.4.0").resolve()


def test_failed_update_restores_previous_release_and_database(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    archive, digest = _release_archive(tmp_path, "0.4.1")
    info = ReleaseInfo(
        version="0.4.1",
        release_url="https://github.test/v0.4.1",
        asset_url="https://github.test/asset",
        digest=digest,
        notes="Changes",
    )
    runner = RecordingRunner(fail_on=("systemctl", "start"))

    with pytest.raises(RuntimeError, match="rolled back"):
        UpdateManager(layout, runner, StubClient(info, archive)).update(info)

    assert layout.current_link.resolve() == layout.release_dir("0.4.0").resolve()


def test_failed_rollback_restores_current_release_and_database(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    archive, digest = _release_archive(tmp_path, "0.4.1")
    info = ReleaseInfo(
        version="0.4.1",
        release_url="https://github.test/v0.4.1",
        asset_url="https://github.test/asset",
        digest=digest,
        notes="Changes",
    )
    runner = RecordingRunner()
    manager = UpdateManager(layout, runner, StubClient(info, archive))
    manager.update(info)
    Store(layout.db_path, allow_startup_migrations=False).create_session(
        "after-update", "Must survive failed rollback", "manual-context"
    )
    runner.fail_on = ("systemctl", "is-active")

    with pytest.raises(RuntimeError, match="Rollback failed"):
        manager.rollback()

    assert layout.current_link.resolve() == layout.release_dir("0.4.1").resolve()
    current = Store(layout.db_path, allow_startup_migrations=False)
    assert current.get_session("after-update") is not None
