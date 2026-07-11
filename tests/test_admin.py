import base64
import importlib
import json
import re
import sys
from pathlib import Path

from starlette.testclient import TestClient

from app.security import password_hash
from app.storage import SESSION_GROUP_ICON_KEYS
from app.time_format import DISPLAY_TIMEZONE_SETTING_KEY


def test_admin_viewer_group_ui_contract() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")

    icon_match = re.search(r"const GROUP_ICONS = (\[[\s\S]*?\]);", viewer)
    assert icon_match is not None
    icon_keys = set(json.loads(icon_match.group(1)))
    assert len(icon_keys) >= 40
    assert icon_keys <= SESSION_GROUP_ICON_KEYS

    assert 'id="groupDeleteButton"' in viewer
    assert 'icon_key: "all_sessions"' in viewer
    assert 'node.dataset.count = String(conversationCount);' in viewer
    assert 'spanCls("group-file-identity")' in viewer
    assert 'setStatus(`Selected ${sessionId}.`, "ok");' not in viewer
    assert 'spanCls("file-meta", "No files")' not in viewer


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
    first_exchange = session_payload["exchanges"][0]
    assert first_exchange["deleted_reason"] == "duplicate"
    assert first_exchange["user_message_token_count"] > 0
    assert first_exchange["assistant_response_token_count"] > 0
    assert first_exchange["total_token_count"] == (
        first_exchange["user_message_token_count"] + first_exchange["assistant_response_token_count"]
    )
    assert session_payload["session"]["token_count"] == first_exchange["total_token_count"]

    restored = client.post(
        f"/admin/api/exchanges/{exchange.exchange_id}/restore",
        headers={"x-csrf-token": csrf_token},
    )
    assert restored.status_code == 200
    assert restored.json()["exchange"]["is_deleted"] is False
    assert len(main.store.list_exchanges(session.session_id)) == 1


def test_admin_can_configure_ai_rename_and_update_session_title(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session("s1", "Chaotic long title", "manual-context")
    main.store.save_exchange("s1", "Claude", "Pierwsza wiadomość użytkownika o ewaluacji LLM.", "OK")
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    csrf_token = client.get("/admin/api/me").json()["csrf_token"]

    settings = client.get("/admin/api/ai-settings")
    assert settings.status_code == 200
    assert settings.json()["settings"]["configured"] is False

    blocked = client.post(
        "/admin/api/sessions/s1/rename/ai",
        headers={"x-csrf-token": csrf_token},
    )
    assert blocked.status_code == 400

    saved = client.put(
        "/admin/api/ai-settings",
        json={"api_key": "sk-test-secret", "model": "gpt-5.4-nano"},
        headers={"x-csrf-token": csrf_token},
    )
    assert saved.status_code == 200
    assert saved.json()["settings"]["configured"] is True
    assert saved.json()["settings"]["api_key_preview"] == "sk-...cret"
    assert "sk-test-secret" not in saved.text
    assert main.store.get_app_setting("ai_rename.api_key") != "sk-test-secret"

    import app.admin as admin_module

    captured = {}

    def fake_suggest(api_key: str, model: str, first_user_message: str) -> str:
        captured["api_key"] = api_key
        captured["model"] = model
        captured["first_user_message"] = first_user_message
        return "Ewaluacja LLM"

    monkeypatch.setattr(admin_module, "_suggest_session_title", fake_suggest)

    renamed = client.post(
        "/admin/api/sessions/s1/rename/ai",
        headers={"x-csrf-token": csrf_token},
    )
    assert renamed.status_code == 200
    assert renamed.json()["session"]["title"] == "Ewaluacja LLM"
    assert main.store.get_session("s1").title == "Ewaluacja LLM"
    assert captured == {
        "api_key": "sk-test-secret",
        "model": "gpt-5.4-nano",
        "first_user_message": "Pierwsza wiadomość użytkownika o ewaluacji LLM.",
    }

    manual = client.patch(
        "/admin/api/sessions/s1",
        json={"title": "Manualny tytuł"},
        headers={"x-csrf-token": csrf_token},
    )
    assert manual.status_code == 200
    assert manual.json()["session"]["title"] == "Manualny tytuł"

    too_long = client.patch(
        "/admin/api/sessions/s1",
        json={"title": "x" * 73},
        headers={"x-csrf-token": csrf_token},
    )
    assert too_long.status_code == 400

    removed = client.request(
        "DELETE",
        "/admin/api/ai-settings/key",
        headers={"x-csrf-token": csrf_token},
    )
    assert removed.status_code == 200
    assert removed.json()["settings"]["configured"] is False


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


def test_admin_can_manage_session_groups_and_move_sessions(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session("s1", "Admin group test", "manual-context")
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    csrf_token = client.get("/admin/api/me").json()["csrf_token"]

    groups = client.get("/admin/api/session-groups")
    assert groups.status_code == 200
    assert {group["group_id"] for group in groups.json()["groups"]} >= {"uncategorized", "brainstorming", "health"}

    created = client.post(
        "/admin/api/session-groups",
        json={"name": "Ideas", "color": "#22c55e", "icon_key": "car"},
        headers={"x-csrf-token": csrf_token},
    )
    assert created.status_code == 200
    assert created.json()["group"]["group_id"] == "ideas"
    assert created.json()["group"]["icon_key"] == "car"

    moved = client.patch(
        "/admin/api/sessions/s1",
        json={"group_id": "ideas"},
        headers={"x-csrf-token": csrf_token},
    )
    assert moved.status_code == 200
    assert moved.json()["session"]["group_id"] == "ideas"
    assert main.store.get_session("s1").group_id == "ideas"

    updated = client.patch(
        "/admin/api/session-groups/ideas",
        json={"name": "Idea Lab", "color": "#0ea5e9", "icon_key": "brain"},
        headers={"x-csrf-token": csrf_token},
    )
    assert updated.status_code == 200
    assert updated.json()["group"]["name"] == "Idea Lab"

    system_edit = client.patch(
        "/admin/api/session-groups/health",
        json={"name": "Wellness"},
        headers={"x-csrf-token": csrf_token},
    )
    assert system_edit.status_code == 400

    deleted = client.request(
        "DELETE",
        "/admin/api/session-groups/ideas",
        json={"destination_group_id": "health"},
        headers={"x-csrf-token": csrf_token},
    )
    assert deleted.status_code == 200
    assert deleted.json()["group"]["deleted_at"] is not None
    assert main.store.get_session("s1").group_id == "health"

    bad_move = client.patch(
        "/admin/api/sessions/s1",
        json={"group_id": "ideas"},
        headers={"x-csrf-token": csrf_token},
    )
    assert bad_move.status_code == 404


def test_admin_can_view_session_and_group_files(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session_group("Tests", "#22c55e", "science")
    main.store.create_session("s1", "File admin test", "manual-context", group_id="tests")
    session_file = main.store.save_session_file("s1", "plan.md", "# Plan")
    group_file = main.store.save_group_file("tests", "shared.md", "Shared context")
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )

    session_payload = client.get("/admin/api/sessions/s1")
    assert session_payload.status_code == 200
    assert session_payload.json()["files"]["session"][0]["filename"] == "plan.md"
    assert session_payload.json()["files"]["group"][0]["filename"] == "shared.md"

    downloaded = client.get(f"/admin/api/files/{group_file.file_id}")
    assert downloaded.status_code == 200
    assert downloaded.json()["file"]["content"] == "Shared context"

    missing = client.get("/admin/api/files/999999")
    assert missing.status_code == 404

    invalid = client.get("/admin/api/files/not-a-number")
    assert invalid.status_code == 400

    assert session_file.file_id != group_file.file_id


def _admin_client(main):
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")
    client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    return client, client.get("/admin/api/me").json()["csrf_token"]


def _encoded_file(content: bytes, *, filename: str = "notes.md", scope_type: str = "session") -> dict:
    return {
        "scope_type": scope_type,
        "filename": filename,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def test_admin_file_mutations_require_login_and_csrf(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session("s1", "File mutations", "manual-context")
    saved = main.store.save_session_file("s1", "existing.md", "old")
    anonymous = TestClient(main.app, base_url="http://127.0.0.1:8787")
    calls = [
        ("POST", "/admin/api/sessions/s1/files", _encoded_file(b"hello")),
        ("PATCH", f"/admin/api/sessions/s1/files/{saved.file_id}", {"content": "new", "expected_sha256": saved.sha256}),
        ("DELETE", f"/admin/api/sessions/s1/files/{saved.file_id}", None),
    ]
    for method, path, payload in calls:
        assert anonymous.request(method, path, json=payload).status_code == 401

    client, csrf = _admin_client(main)
    for method, path, payload in calls:
        assert client.request(method, path, json=payload).status_code == 403
    assert csrf


def test_admin_uploads_bounded_utf8_files_to_selected_session_or_group(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session_group("Tests", "#22c55e", "science")
    main.store.create_session("s1", "File mutations", "manual-context", group_id="tests")
    client, csrf = _admin_client(main)
    headers = {"x-csrf-token": csrf}

    uploaded = client.post(
        "/admin/api/sessions/s1/files",
        json=_encoded_file(b"\xef\xbb\xbf# Hello", filename="README.MD"),
        headers=headers,
    )
    assert uploaded.status_code == 200
    session_file = uploaded.json()["file"]
    assert session_file["scope_type"] == "session"
    assert session_file["session_id"] == "s1"
    assert session_file["mime_type"] == "text/markdown"
    assert session_file["created_by"] == "owner"
    assert main.store.get_session_file(session_file["file_id"]).content == "# Hello"

    group_upload = client.post(
        "/admin/api/sessions/s1/files",
        json=_encoded_file(b"a,b\n1,2", filename="data.csv", scope_type="group"),
        headers=headers,
    )
    assert group_upload.status_code == 200
    assert group_upload.json()["file"]["group_id"] == "tests"
    assert group_upload.json()["file"]["mime_type"] == "text/csv"

    bad_payloads = [
        {"scope_type": "session", "filename": "bad.md", "content_base64": "%%%"},
        _encoded_file(b"\xff", filename="bad.md"),
        _encoded_file(b"hello", filename="bad.exe"),
        _encoded_file(b"", filename="empty.md"),
        {"scope_type": "session", "files": [_encoded_file(b"one"), _encoded_file(b"two")]},
    ]
    for payload in bad_payloads:
        assert client.post("/admin/api/sessions/s1/files", json=payload, headers=headers).status_code == 400

    assert client.post(
        "/admin/api/sessions/s1/files",
        json=_encoded_file(b"x" * 1_000_001),
        headers=headers,
    ).status_code == 400

    oversized = b'{"padding":"' + b"x" * 1_500_000 + b'"}'
    misleading = client.build_request(
        "POST",
        "/admin/api/sessions/s1/files",
        content=oversized,
        headers={**headers, "content-type": "application/json", "content-length": "1"},
    )
    assert client.send(misleading).status_code == 413
    absent = client.build_request(
        "POST",
        "/admin/api/sessions/s1/files",
        content=oversized,
        headers={**headers, "content-type": "application/json"},
    )
    del absent.headers["content-length"]
    assert client.send(absent).status_code == 413
    assert len(main.store.list_session_files(session_id="s1")) == 1


def test_admin_edits_moves_and_deletes_only_visible_files(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    main.store.create_session_group("Tests", "#22c55e", "science")
    main.store.create_session_group("Other", "#ef4444", "camera")
    main.store.create_session("s1", "File mutations", "manual-context", group_id="tests")
    main.store.create_session("s2", "Other session", "manual-context", group_id="other")
    saved = main.store.save_session_file("s1", "notes.md", "old")
    unrelated = main.store.save_session_file("s2", "private.md", "untouched")
    client, csrf = _admin_client(main)
    headers = {"x-csrf-token": csrf}
    path = f"/admin/api/sessions/s1/files/{saved.file_id}"

    assert client.patch(
        path,
        json={"content": "new", "expected_sha256": saved.sha256, "scope_type": "group"},
        headers=headers,
    ).status_code == 400

    edited = client.patch(
        path,
        json={"content": "new", "expected_sha256": saved.sha256},
        headers=headers,
    )
    assert edited.status_code == 200
    assert edited.json()["file"]["file_id"] == saved.file_id
    assert edited.json()["file"]["sha256"] != saved.sha256
    assert "content" not in edited.json()["file"]

    stale = client.patch(
        path,
        json={"content": "stale write", "expected_sha256": saved.sha256},
        headers=headers,
    )
    assert stale.status_code == 409
    assert "content" not in stale.text

    moved = client.patch(path, json={"scope_type": "group"}, headers=headers)
    assert moved.status_code == 200
    assert moved.json()["file"]["group_id"] == "tests"
    assert client.patch(path, json={"scope_type": "group"}, headers=headers).status_code == 200
    moved_back = client.patch(path, json={"scope_type": "session"}, headers=headers)
    assert moved_back.status_code == 200
    assert moved_back.json()["file"]["session_id"] == "s1"

    assert client.patch(
        path,
        json={"scope_type": "group", "group_id": "other"},
        headers=headers,
    ).status_code == 400
    assert client.patch(
        f"/admin/api/sessions/s1/files/{unrelated.file_id}",
        json={"scope_type": "group"},
        headers=headers,
    ).status_code == 404

    deleted = client.delete(path, headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["file"]["file_id"] == saved.file_id
    assert "content" not in deleted.json()["file"]
    assert client.delete(path, headers=headers).status_code == 404
    assert client.get(f"/admin/api/files/{saved.file_id}").status_code == 404
    assert main.store.get_session_file(unrelated.file_id).content == "untouched"


def _load_main(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIDGE_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "bridge.sqlite3"))
    monkeypatch.setenv("BRIDGE_OWNER_USERNAME", "owner")
    monkeypatch.setenv("BRIDGE_OWNER_PASSWORD_HASH", password_hash("secret-admin-password"))
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "test-secret")

    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")
