import importlib
import sys
from pathlib import Path

from starlette.testclient import TestClient

from app.security import password_hash
from app.time_format import DISPLAY_TIMEZONE_SETTING_KEY


def test_admin_api_requires_login_and_csrf_for_mutations(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    session = main.store.create_session("s1", "Admin test", "manual-context")
    exchange = main.store.save_exchange("s1", "Claude", "Message.", "Answer to correct.")

    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    assert client.get("/admin/api/sessions").status_code == 401
    assert client.delete(f"/admin/api/exchanges/{exchange.exchange_id}").status_code == 401

    login = client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    me = client.get("/admin/api/me")
    assert me.status_code == 200
    csrf_token = me.json()["csrf_token"]

    assert client.request(
        "DELETE",
        f"/admin/api/exchanges/{exchange.exchange_id}",
        json={"reason": "duplicate"},
    ).status_code == 403

    deleted = client.request(
        "DELETE",
        f"/admin/api/exchanges/{exchange.exchange_id}",
        json={"reason": "duplicate"},
        headers={"x-csrf-token": csrf_token},
    )
    assert deleted.status_code == 200
    assert deleted.json()["exchange"]["is_deleted"] is True
    assert main.store.list_exchanges(session.session_id) == []

    session_payload = client.get(f"/admin/api/sessions/{session.session_id}").json()
    assert session_payload["exchanges"][0]["deleted_reason"] == "duplicate"

    restored = client.post(
        f"/admin/api/exchanges/{exchange.exchange_id}/restore",
        headers={"x-csrf-token": csrf_token},
    )
    assert restored.status_code == 200
    assert restored.json()["exchange"]["is_deleted"] is False
    assert len(main.store.list_exchanges(session.session_id)) == 1


def test_admin_can_update_display_timezone(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    login = client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    csrf_token = client.get("/admin/api/me").json()["csrf_token"]

    invalid = client.post(
        "/admin/api/timezone",
        json={"timezone": "Not/AZone"},
        headers={"x-csrf-token": csrf_token},
    )
    assert invalid.status_code == 400

    updated = client.post(
        "/admin/api/timezone",
        json={"timezone": "Europe/Paris"},
        headers={"x-csrf-token": csrf_token},
    )

    assert updated.status_code == 200
    assert updated.json()["display_timezone"] == "Europe/Paris"
    assert main.store.get_app_setting(DISPLAY_TIMEZONE_SETTING_KEY) == "Europe/Paris"
    assert client.get("/admin/api/me").json()["display_timezone"] == "Europe/Paris"

    legacy_updated = client.put(
        "/admin/api/timezone",
        json={"timezone": "UTC"},
        headers={"x-csrf-token": csrf_token},
    )
    assert legacy_updated.status_code == 200
    assert legacy_updated.json()["display_timezone"] == "UTC"


def _load_main(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIDGE_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "bridge.sqlite3"))
    monkeypatch.setenv("BRIDGE_SUMMARIES_DIR", str(tmp_path / "summaries"))
    monkeypatch.setenv("BRIDGE_OWNER_USERNAME", "owner")
    monkeypatch.setenv("BRIDGE_OWNER_PASSWORD_HASH", password_hash("secret-admin-password"))
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "test-secret")

    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")
