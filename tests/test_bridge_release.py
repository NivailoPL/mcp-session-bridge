from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from bridge_cli.files import atomic_write_json
from bridge_cli.layout import Layout
from bridge_cli.release import (
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
