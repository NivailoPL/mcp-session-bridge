from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.admin import ADMIN_COOKIE
from app.security import SECRET_KEY_PLACEHOLDER, token_urlsafe
from bridge_cli.config import read_env_file, update_env_file
from bridge_cli.install import ManagedInstaller
from bridge_cli.layout import Layout
from tests.test_bridge_setup import RecordingRunner, source_tree

ROOT = Path(__file__).resolve().parents[1]


def generate_credentials(env: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/set_owner_password.py"),
         "--env", str(env), "--write-once-file", str(env.parent / "credentials")],
        capture_output=True, text=True, check=False,
    )


def test_documented_setup_generates_distinct_keys_and_rejects_example_cookie(tmp_path, load_main):
    keys = []
    credentials = []
    password_hashes = []
    for index in range(2):
        env = tmp_path / f"setup-{index}.env"
        env.write_text((ROOT / ".env.example").read_text())
        update_env_file(env, {"BRIDGE_DB_PATH": str(tmp_path / f"data-{index}.sqlite3")})
        result = generate_credentials(env)
        assert result.returncode == 0, result.stderr
        values = read_env_file(env)
        keys.append(values["BRIDGE_SECRET_KEY"])
        password_hashes.append(values["BRIDGE_OWNER_PASSWORD_HASH"])
        credentials.append(read_env_file(env.parent / "credentials")["password"])
        assert len(keys[-1]) >= 32
        assert keys[-1] != SECRET_KEY_PLACEHOLDER
    assert keys[0] != keys[1]

    main = load_main(env={
        "BRIDGE_SECRET_KEY": keys[0], "BRIDGE_OWNER_PASSWORD_HASH": password_hashes[0],
    })
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")
    data = base64.urlsafe_b64encode(json.dumps({
        "username": "owner", "csrf": "fake", "exp": int(time.time()) + 3600,
    }).encode()).decode().rstrip("=")
    signature = hmac.new(SECRET_KEY_PLACEHOLDER.encode(), data.encode(), hashlib.sha256).hexdigest()
    client.cookies.set(ADMIN_COOKIE, f"{data}.{signature}", path="/admin")
    assert client.get("/admin/api/me").status_code == 401
    login = client.post("/admin/login", data={
        "username": "owner", "password": credentials[0],
    }, follow_redirects=False)
    assert login.status_code == 303
    assert client.get("/admin/api/me").status_code == 200


@pytest.mark.parametrize("key", [SECRET_KEY_PLACEHOLDER, "", "   ", "short-secret"])
def test_startup_rejects_unsafe_keys_without_creating_database(tmp_path, load_main, key):
    with pytest.raises((ValueError, RuntimeError), match="BRIDGE_SECRET_KEY"):
        load_main(env={"BRIDGE_SECRET_KEY": key})
    assert not (tmp_path / "bridge.sqlite3").exists()


def test_password_change_preserves_existing_key_and_custom_configuration(tmp_path):
    env = tmp_path / "bridge.env"
    key = token_urlsafe(48)
    database = tmp_path / "existing.sqlite3"
    database.write_bytes(b"existing data must remain unchanged")
    env.write_text(f'# operator setting\nBRIDGE_SECRET_KEY="{key}"\n')
    update_env_file(env, {"BRIDGE_DB_PATH": str(database), "CUSTOM_SETTING": "keep me"})
    assert generate_credentials(env).returncode == 0
    values = read_env_file(env)
    assert values["BRIDGE_SECRET_KEY"] == key
    assert values["CUSTOM_SETTING"] == "keep me"
    assert "# operator setting" in env.read_text()
    assert database.read_bytes() == b"existing data must remain unchanged"


@pytest.mark.parametrize("key", [SECRET_KEY_PLACEHOLDER, "", "short-secret"])
def test_script_does_not_rotate_existing_database_key(tmp_path, key):
    env = tmp_path / "bridge.env"
    database = tmp_path / "existing.sqlite3"
    database.write_bytes(b"existing database")
    update_env_file(env, {"BRIDGE_DB_PATH": str(database), "BRIDGE_SECRET_KEY": key})
    before = env.read_bytes()
    result = generate_credentials(env)
    assert result.returncode != 0
    assert "BRIDGE_SECRET_KEY" in result.stderr
    assert env.read_bytes() == before
    assert not (tmp_path / "credentials").exists()


def test_installer_refuses_to_adopt_placeholder_without_rewriting_source(tmp_path):
    source = source_tree(tmp_path)
    env = source / ".env"
    update_env_file(env, {"BRIDGE_SECRET_KEY": SECRET_KEY_PLACEHOLDER})
    before = env.read_bytes()
    layout = Layout.for_root(tmp_path / "target")
    installer = ManagedInstaller(layout, source, RecordingRunner())
    with pytest.raises(ValueError, match="BRIDGE_SECRET_KEY"):
        installer.configure_domain("bridge.example.test", legacy_env=env)
    assert env.read_bytes() == before
    assert not layout.pending_env_file.exists()


def test_new_installer_can_configure_owner_before_domain_and_preserves_key(tmp_path):
    layout = Layout.for_root(tmp_path / "target")
    installer = ManagedInstaller(layout, source_tree(tmp_path), RecordingRunner())
    installer.configure_administrator("owner", "a-long-owner-password")
    installer.configure_domain("bridge.example.test")
    first = read_env_file(layout.pending_env_file)["BRIDGE_SECRET_KEY"]
    assert len(first) >= 32 and first != SECRET_KEY_PLACEHOLDER
    installer.configure_domain("other.example.test")
    assert read_env_file(layout.pending_env_file)["BRIDGE_SECRET_KEY"] == first


def test_activation_rechecks_pending_key_before_running_commands(tmp_path):
    layout = Layout.for_root(tmp_path / "target")
    runner = RecordingRunner()
    installer = ManagedInstaller(layout, source_tree(tmp_path), runner)
    installer.configure_domain("bridge.example.test")
    installer.configure_administrator("owner", "a-long-owner-password")
    installer.stage_release()
    installer.stage_database(None)
    installer.stage_service()
    update_env_file(layout.pending_env_file, {"BRIDGE_SECRET_KEY": SECRET_KEY_PLACEHOLDER})
    runner.calls.clear()
    with pytest.raises(ValueError, match="BRIDGE_SECRET_KEY"):
        installer.activate()
    assert runner.calls == []
