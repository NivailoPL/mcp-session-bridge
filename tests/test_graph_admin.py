from __future__ import annotations

import importlib
import sys
from pathlib import Path

from starlette.testclient import TestClient

from app.security import password_hash


def _load_main(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIDGE_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "bridge.sqlite3"))
    monkeypatch.setenv("BRIDGE_OWNER_USERNAME", "owner")
    monkeypatch.setenv("BRIDGE_OWNER_PASSWORD_HASH", password_hash("secret-admin-password"))
    monkeypatch.setenv("BRIDGE_SECRET_KEY", "test-secret")
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def _admin_client(main):
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")
    response = client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/graph"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client, client.get("/admin/api/me").json()["csrf_token"]


def test_graph_page_and_assets_require_admin_login(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    anonymous = TestClient(main.app, base_url="http://127.0.0.1:8787")

    for path in ("/admin/graph", "/admin/assets/graph-viewer.css", "/admin/assets/graph-viewer.js"):
        response = anonymous.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/admin/login")

    client, _ = _admin_client(main)
    page = client.get("/admin/graph")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert 'aria-current="page">Graph</a>' in page.text
    assert "Contexts" in page.text
    assert "Map" in page.text
    assert "Config" in page.text
    assert client.get("/admin/assets/graph-viewer.css").status_code == 200
    assert client.get("/admin/assets/graph-viewer.js").status_code == 200


def test_graph_config_api_requires_csrf_and_enforces_lock(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    client, csrf = _admin_client(main)

    assert client.get("/admin/api/graph/config").status_code == 200
    assert client.post("/admin/api/graph/config/unlock").status_code == 403

    unlocked = client.post(
        "/admin/api/graph/config/unlock", headers={"x-csrf-token": csrf}
    )
    assert unlocked.status_code == 200
    assert unlocked.json()["config"]["locked"] is False

    saved = client.put(
        "/admin/api/graph/config/draft",
        json={
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "prompt": "Extract durable concepts with literal evidence.",
            "max_concepts": 35,
            "inactivity_hours": 6,
            "include_sensitive": False,
        },
        headers={"x-csrf-token": csrf},
    )
    assert saved.status_code == 200
    assert saved.json()["config"]["draft"]["inactivity_hours"] == 6
    assert saved.json()["config"]["active_profile"]["inactivity_hours"] == 24

    activated = client.post(
        "/admin/api/graph/config/activate", headers={"x-csrf-token": csrf}
    )
    assert activated.status_code == 200
    assert activated.json()["config"]["active_profile"]["version"] == 2
    assert activated.json()["config"]["locked"] is True


def test_graph_cannot_enable_without_authenticated_codex(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    client, csrf = _admin_client(main)

    class LoggedOutCodex:
        async def status(self):
            return {"available": True, "authenticated": False, "account": None, "version": "0.147.0"}

    main.admin.codex = LoggedOutCodex()
    blocked = client.put(
        "/admin/api/graph/state",
        json={"enabled": True},
        headers={"x-csrf-token": csrf},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "graph_provider_not_ready"
    assert main.store.get_graph_config()["enabled"] is False

    class ReadyCodex:
        async def status(self):
            return {
                "available": True,
                "authenticated": True,
                "account": {"type": "chatgpt", "plan_type": "plus"},
                "version": "0.147.0",
            }

    main.admin.codex = ReadyCodex()
    enabled = client.put(
        "/admin/api/graph/state",
        json={"enabled": True},
        headers={"x-csrf-token": csrf},
    )
    assert enabled.status_code == 200
    assert enabled.json()["config"]["enabled"] is True

    disabled = client.put(
        "/admin/api/graph/state",
        json={"enabled": False},
        headers={"x-csrf-token": csrf},
    )
    assert disabled.status_code == 200
    assert disabled.json()["config"]["enabled"] is False


def test_sessions_view_exposes_workspace_navigation_contract() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")
    assert 'aria-current="page">Sessions</a>' in viewer
    assert 'href="/admin/graph">Graph</a>' in viewer
    assert 'aria-disabled="true">Contexts</span>' in viewer


def test_processing_and_analysis_apis_require_auth_and_return_durable_state(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    anonymous = TestClient(main.app, base_url="http://127.0.0.1:8787")
    assert anonymous.get("/admin/api/graph/jobs").status_code == 401
    assert anonymous.get("/admin/api/graph/analysis").status_code == 401
    assert anonymous.get("/admin/api/graph/lab").status_code == 401

    client, _ = _admin_client(main)
    assert client.get("/admin/api/graph/jobs").json() == {"ok": True, "jobs": []}
    assert client.get("/admin/api/graph/analysis").json() == {"ok": True, "sessions": []}
    assert client.get("/admin/api/graph/lab").json() == {"ok": True, "runs": []}
    missing = client.get("/admin/api/graph/analysis/unknown")
    assert missing.status_code == 404


def test_failed_graph_job_details_reach_processing_and_analysis_ui(tmp_path, monkeypatch) -> None:
    main = _load_main(tmp_path, monkeypatch)
    client, _ = _admin_client(main)
    draft = main.store.unlock_graph_profile("owner")
    main.store.update_graph_draft({**draft, "inactivity_hours": 1}, "owner")
    main.store.activate_graph_draft("owner")
    main.store.create_session("failed-session", "Failed Graph session", "manual-context")
    exchange = main.store.save_exchange(
        "failed-session", "model", "A durable Graph decision.", "The decision is stored."
    )
    with main.store._connect() as connection:
        connection.execute(
            "UPDATE exchanges SET created_at = 1000, assistant_created_at = 1000 WHERE exchange_id = ?",
            (exchange.exchange_id,),
        )
    main.store.set_graph_enabled(True)
    main.store.enqueue_eligible_graph_jobs(now=10_000)
    job = main.store.claim_graph_job("test-worker", now=10_000, lease_seconds=30)
    assert job is not None
    main.store.fail_graph_job(
        job["job_id"],
        lease_owner="test-worker",
        error_code="evidence_quote_not_literal",
        error_message="Evidence quote must appear literally in its source exchange.",
        retryable=False,
        now=10_001,
    )

    processing = client.get("/admin/api/graph/jobs").json()["jobs"][0]
    assert processing["error_code"] == "evidence_quote_not_literal"
    assert processing["error_message"] == "Evidence quote must appear literally in its source exchange."

    analysis = client.get("/admin/api/graph/analysis").json()["sessions"][0]
    assert analysis["latest_job_id"] == job["job_id"]
    assert analysis["latest_job_status"] == "terminal_failed"
    assert analysis["latest_job_attempts"] == 1
    assert analysis["latest_job_max_attempts"] == 3
    assert analysis["latest_job_error_code"] == "evidence_quote_not_literal"
    assert analysis["latest_job_error_message"] == "Evidence quote must appear literally in its source exchange."

    script = client.get("/admin/assets/graph-viewer.js").text
    assert "renderJobDetail" in script
    assert "job.error_code" in script
    assert "job.error_message" in script
    assert "renderUnavailableAnalysis" in script
    assert "session.latest_job_error_code" in script
    assert "analysisDetailGeneration" in script
    assert "generation !== state.analysisDetailGeneration" in script
    assert "button.disabled = true" not in script
