from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.storage import Store
import bridge_cli.release as release_module

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

        if args[:2] == ("uv", "sync"):
            project = Path(args[args.index("--project") + 1])
            python = project / ".venv/bin/python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
            (project / ".venv/bin/uvicorn").write_text(
                f"#!{python}\n", encoding="utf-8"
            )

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


def test_explicit_update_always_refreshes_release_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    refreshes: list[bool] = []

    class StubUpdateManager:
        def __init__(self, layout: Layout, runner: RecordingRunner) -> None:
            pass

        def check(self, *, force: bool = False) -> dict[str, str]:
            refreshes.append(force)
            return {"state": "current", "current": "0.4.0"}

    monkeypatch.setattr(release_module, "UpdateManager", StubUpdateManager)

    result = release_module.run_release_command(
        SimpleNamespace(command="update", check=False),
        Layout.for_root(tmp_path),
        RecordingRunner(),
    )

    assert result == 0
    assert refreshes == [True]


def test_update_check_reports_available_release_without_installing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class StubUpdateManager:
        def __init__(self, layout: Layout, runner: RecordingRunner) -> None:
            pass

        def check(self, *, force: bool = False) -> dict[str, str]:
            assert force is True
            return {
                "state": "available",
                "current": "0.4.0",
                "latest": "0.4.1",
            }

        def update(self, release: ReleaseInfo | None = None) -> dict[str, str]:
            raise AssertionError("--check must not install an update")

    monkeypatch.setattr(release_module, "UpdateManager", StubUpdateManager)

    result = release_module.run_release_command(
        SimpleNamespace(command="update", check=True),
        Layout.for_root(tmp_path),
        RecordingRunner(),
    )

    assert result == 0


def test_update_declined_by_user_does_not_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = ReleaseInfo(
        version="0.4.1",
        asset_url="https://github.test/asset",
        release_url="https://github.test/release",
        digest="a" * 64,
        notes="Changes",
    )

    class StubClient:
        def latest(self) -> ReleaseInfo:
            return release

    class StubUpdateManager:
        client = StubClient()

        def __init__(self, layout: Layout, runner: RecordingRunner) -> None:
            pass

        def check(self, *, force: bool = False) -> dict[str, str]:
            assert force is True
            return {"state": "available", "current": "0.4.0", "latest": "0.4.1"}

        def update(self, release: ReleaseInfo | None = None) -> dict[str, str]:
            raise AssertionError("a declined update must not be installed")

    monkeypatch.setattr(release_module, "UpdateManager", StubUpdateManager)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    result = release_module.run_release_command(
        SimpleNamespace(command="update", check=False),
        Layout.for_root(tmp_path),
        RecordingRunner(),
    )

    assert result == 0


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


def _commit_change(source: Path, name: str = "marker.txt") -> str:
    marker = source / name
    marker.write_text(f"change-{name}\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(source), "add", name), check=True)
    subprocess.run(
        (
            "git", "-C", str(source), "-c", "user.name=Test", "-c",
            "user.email=test@example.test", "commit", "-qm", f"Add {name}",
        ),
        check=True,
    )
    return subprocess.run(
        ("git", "-C", str(source), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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
    uv_sync = next(call for call in runner.calls if call[:2] == ("uv", "sync"))
    assert Path(uv_sync[uv_sync.index("--project") + 1]) == layout.release_dir(
        result["release_id"]
    )

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


def test_checkout_deploy_rejects_older_semantic_version_without_override(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    installation = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    installation.update({"version": "0.5.0"})
    atomic_write_json(layout.installation_file, installation)
    source = _checkout(tmp_path)
    manager = CheckoutDeployer(layout, CheckoutRunner())

    with pytest.raises(RuntimeError, match="older than active Bridge 0.5.0"):
        manager.plan(source)

    override = manager.plan(source, allow_downgrade=True)
    assert override.downgrade_override is True

    result = manager.deploy(source, allow_downgrade=True)
    assert result["state"] == "complete"
    assert result["downgrade_override"] is True


def test_checkout_deploy_rejects_older_commit_at_same_version(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    source = _checkout(tmp_path)
    first = subprocess.run(
        ("git", "-C", str(source), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    second = _commit_change(source)
    installation = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    installation.update(
        {"commit": second, "release_id": f"0.4.0-git-{second[:12]}"}
    )
    atomic_write_json(layout.installation_file, installation)
    subprocess.run(("git", "-C", str(source), "checkout", "-q", first), check=True)

    with pytest.raises(RuntimeError, match="older commit"):
        CheckoutDeployer(layout, CheckoutRunner()).plan(source)


def test_checkout_deploy_allows_fast_forward_commit(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    source = _checkout(tmp_path)
    first = subprocess.run(
        ("git", "-C", str(source), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    second = _commit_change(source)
    installation = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    installation.update(
        {"commit": first, "release_id": f"0.4.0-git-{first[:12]}"}
    )
    atomic_write_json(layout.installation_file, installation)

    plan = CheckoutDeployer(layout, CheckoutRunner()).plan(source)

    assert plan.commit == second
    assert plan.downgrade_override is False


def test_checkout_deploy_rejects_divergent_commit_without_override(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    source = _checkout(tmp_path)
    base = subprocess.run(
        ("git", "-C", str(source), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    active = _commit_change(source, "active.txt")
    subprocess.run(("git", "-C", str(source), "checkout", "-q", base), check=True)
    _commit_change(source, "divergent.txt")
    installation = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    installation.update(
        {"commit": active, "release_id": f"0.4.0-git-{active[:12]}"}
    )
    atomic_write_json(layout.installation_file, installation)

    with pytest.raises(RuntimeError, match="not a fast-forward"):
        CheckoutDeployer(layout, CheckoutRunner()).plan(source)


def test_checkout_deploy_rejects_higher_version_on_divergent_history(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    source = _checkout(tmp_path)
    base = subprocess.run(
        ("git", "-C", str(source), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    active = _commit_change(source, "active.txt")
    subprocess.run(("git", "-C", str(source), "checkout", "-q", base), check=True)
    project = source / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8").replace("version='0.4.0'", "version='0.4.1'"),
        encoding="utf-8",
    )
    subprocess.run(("git", "-C", str(source), "add", "pyproject.toml"), check=True)
    subprocess.run(
        (
            "git", "-C", str(source), "-c", "user.name=Test", "-c",
            "user.email=test@example.test", "commit", "-qm", "Bump divergent version",
        ),
        check=True,
    )
    installation = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    installation.update(
        {"commit": active, "release_id": f"0.4.0-git-{active[:12]}"}
    )
    atomic_write_json(layout.installation_file, installation)

    with pytest.raises(RuntimeError, match="not a fast-forward"):
        CheckoutDeployer(layout, CheckoutRunner()).plan(source)


def test_checkout_deploy_rejects_invalid_active_commit_metadata(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    installation = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    installation["commit"] = "not-a-full-commit"
    atomic_write_json(layout.installation_file, installation)

    with pytest.raises(RuntimeError, match="commit metadata is invalid"):
        CheckoutDeployer(layout, CheckoutRunner()).plan(_checkout(tmp_path))


def test_checkout_deploy_ignores_git_replacement_objects(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    source = _checkout(tmp_path)
    base = subprocess.run(
        ("git", "-C", str(source), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    active = _commit_change(source, "active.txt")
    subprocess.run(("git", "-C", str(source), "checkout", "-q", base), check=True)
    target = _commit_change(source, "divergent.txt")
    subprocess.run(("git", "-C", str(source), "replace", target, active), check=True)
    installation = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    installation.update(
        {"commit": active, "release_id": f"0.4.0-git-{active[:12]}"}
    )
    atomic_write_json(layout.installation_file, installation)

    with pytest.raises(RuntimeError, match="not a fast-forward"):
        CheckoutDeployer(layout, CheckoutRunner()).plan(source)


def test_failed_checkout_deploy_restores_previous_release(tmp_path: Path) -> None:
    layout = _managed_layout(tmp_path)
    source = _checkout(tmp_path)
    runner = CheckoutRunner(fail_on=("systemctl", "start"))

    with pytest.raises(RuntimeError, match="rolled back"):
        CheckoutDeployer(layout, runner).deploy(source)

    assert layout.current_link.resolve() == layout.release_dir("0.4.0").resolve()


def test_receipt_write_failure_restores_previous_installation_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _managed_layout(tmp_path)
    source = _checkout(tmp_path)
    manager = CheckoutDeployer(layout, CheckoutRunner())
    original_write = release_module.atomic_write_json
    failed = False

    def fail_completed_receipt(path: Path, value: dict, *args: object) -> None:
        nonlocal failed
        if path == layout.operation_file and value.get("state") == "complete" and not failed:
            failed = True
            raise OSError("forced receipt failure")
        original_write(path, value, *args)

    monkeypatch.setattr(release_module, "atomic_write_json", fail_completed_receipt)

    with pytest.raises(RuntimeError, match="rolled back"):
        manager.deploy(source)

    assert layout.current_link.resolve() == layout.release_dir("0.4.0").resolve()
    installation = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    assert installation["version"] == "0.4.0"
    assert "commit" not in installation
    assert "release_id" not in installation


def test_rollback_receipt_failure_restores_current_runtime_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _managed_layout(tmp_path)
    source = _checkout(tmp_path)
    manager = CheckoutDeployer(layout, CheckoutRunner())
    deployed = manager.deploy(source)
    original_write = release_module.atomic_write_json

    def fail_rollback_receipt(path: Path, value: dict, *args: object) -> None:
        if path == layout.operation_file and value.get("operation") == "rollback":
            raise OSError("forced rollback receipt failure")
        original_write(path, value, *args)

    monkeypatch.setattr(release_module, "atomic_write_json", fail_rollback_receipt)

    with pytest.raises(RuntimeError, match="current release and database were restored"):
        manager.rollback()

    assert layout.current_link.resolve() == layout.release_dir(deployed["release_id"]).resolve()
    installation = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    assert installation["commit"] == deployed["commit"]
    assert installation["release_id"] == deployed["release_id"]


def test_system_database_migration_runs_as_service_user_with_safe_import_path() -> None:
    runner = RecordingRunner()
    manager = UpdateManager(Layout.system(), runner)
    release = Path("/opt/mcp-session-bridge/releases/test")
    database = Path("/var/lib/mcp-session-bridge/bridge.sqlite3")

    manager._migrate_database(release, database, service_user=True)

    assert runner.calls[-1] == (
        "runuser", "--user", "mcp-session-bridge", "--", "env",
        f"PYTHONPATH={release}", str(release / ".venv/bin/python"),
        "-P",
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
    installation = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    installation.update(
        {
            "commit": "a" * 40,
            "release_id": "0.4.0-git-aaaaaaaaaaaa",
        }
    )
    atomic_write_json(layout.installation_file, installation)

    result = manager.update(info)

    assert result["state"] == "complete"
    assert layout.current_link.resolve() == layout.release_dir("0.4.1").resolve()
    receipt = json.loads(layout.operation_file.read_text(encoding="utf-8"))
    assert receipt["previous_version"] == "0.4.0"
    assert receipt["codex_runtime"]["state"] == "disabled"
    assert Path(receipt["database_backup"]).exists()
    updated = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    assert updated["commit"] is None
    assert updated["release_id"] == "0.4.1"

    rollback = manager.rollback()
    assert rollback["state"] == "complete"
    assert rollback["codex_runtime"]["state"] == "disabled"
    assert layout.current_link.resolve() == layout.release_dir("0.4.0").resolve()
    restored = json.loads(layout.installation_file.read_text(encoding="utf-8"))
    assert restored["commit"] == "a" * 40
    assert restored["release_id"] == "0.4.0-git-aaaaaaaaaaaa"


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
