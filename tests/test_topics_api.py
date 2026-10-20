"""Admin API of the Topics lab: map versions, key, settings and manual Jev runs."""
from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from app.topics import POOL_KEY, TOPIC_OPENROUTER_KEY_SETTING
from tests.conftest import ADMIN_BASE_URL
from tests.test_topic_runtime import FakeJev

SESSION = "20261017-120000-radio-a1b2c3"
MAP = {"radio_job": "Praca w Radiu Kraków.", "car": "Samochód."}


def _setup(load_main, admin_client):
    main = load_main()
    main.store.create_session(SESSION, "Radio")
    for title in ("Radio grafik", "Nowa sesja"):
        exchange = main.store.save_exchange(SESSION, "Claude", title, "ok").exchange_id
        main.store.create_strand(SESSION, title, [[exchange, exchange]])
    log: list = []
    main.topic_runtime.jev_factory = lambda key, model: FakeJev(log)
    client, csrf = admin_client(main)
    return main, client, {"x-csrf-token": csrf}, log


def test_topics_page_and_api_require_login(load_main) -> None:
    main = load_main()
    anonymous = TestClient(main.app, base_url=ADMIN_BASE_URL, follow_redirects=False)
    assert anonymous.get("/admin/topics").status_code in (302, 303, 401)
    assert anonymous.get("/admin/api/topics").status_code == 401


def test_page_serves_workspace(load_main, admin_client) -> None:
    main, client, _, _ = _setup(load_main, admin_client)
    page = client.get("/admin/topics")
    assert page.status_code == 200 and 'data-label="TOPICS"' in page.text
    assert client.get("/admin/assets/topics-viewer.js").status_code == 200


def test_key_is_encrypted_and_never_returned(load_main, admin_client) -> None:
    main, client, headers, _ = _setup(load_main, admin_client)
    assert client.get("/admin/api/topics").json()["key_set"] is False
    assert client.put("/admin/api/topics/key", json={"api_key": "sk-or-secret"}).status_code == 403
    saved = client.put("/admin/api/topics/key", json={"api_key": "sk-or-secret"}, headers=headers)
    assert saved.json() == {"ok": True, "key_set": True}
    assert "sk-or-secret" not in main.store.get_app_setting(TOPIC_OPENROUTER_KEY_SETTING)
    assert main.topics_admin.read_key() == "sk-or-secret"
    overview = client.get("/admin/api/topics").text
    journal = client.get("/admin/api/topics/journal").text
    assert "sk-or-secret" not in overview and "sk-or-secret" not in journal
    client.put("/admin/api/topics/key", json={"api_key": ""}, headers=headers)
    assert main.topics_admin.read_key() == ""


def test_import_classify_and_export_round_trip(load_main, admin_client) -> None:
    main, client, headers, log = _setup(load_main, admin_client)
    assert client.post("/admin/api/topics/estimate", json={}, headers=headers).json()["code"] == "map_missing"
    imported = client.post(
        "/admin/api/topics/versions", json={"topics": MAP, "origin": "import", "note": "v6"}, headers=headers
    )
    assert imported.status_code == 200, imported.text
    version = imported.json()["version"]
    assert [t["key"] for t in version["topics"]] == ["radio_job", "car", POOL_KEY]

    assert client.post("/admin/api/topics/runs", json={}, headers=headers).json()["code"] == "key_missing"
    client.put("/admin/api/topics/key", json={"api_key": "sk-or-secret"}, headers=headers)
    estimate = client.post("/admin/api/topics/estimate", json={"scope": {"mode": "unclassified"}}, headers=headers)
    assert estimate.json()["estimate"]["send_count"] == 2

    # The admin route only queues the run; run it to the end in-process like the strand tests do.
    async def classify():
        run = await main.topic_runtime.start_classify({"mode": "all"}, actor="owner")
        await main.topic_runtime.wait()
        return run["run_id"]

    run_id = asyncio.run(classify())
    run = client.get(f"/admin/api/topics/runs/{run_id}").json()["run"]
    assert run["status"] == "completed" and run["done_count"] == 2
    again = client.post("/admin/api/topics/runs", json={}, headers=headers)
    assert again.status_code == 409 and again.json()["code"] == "nothing_to_do"

    chapters = client.get("/admin/api/topics/assignments").json()["chapters"]
    assert {c["title"]: c["topic_key"] for c in chapters} == {"Radio grafik": "radio_job", "Nowa sesja": POOL_KEY}

    export = client.get(f"/admin/api/topics/runs/{run_id}/export")
    assert "attachment" in export.headers["content-disposition"]
    body = export.json()
    assert body["input"]["criteria"][POOL_KEY].startswith("Do ułożenia")
    assert len(body["items"]) == 2 and body["items"][0]["response"]["usage"]["cost"] == 0.0001
    assert "sk-or-secret" not in export.text

    actions = [entry["action"] for entry in client.get("/admin/api/topics/journal").json()["journal"]]
    assert actions[:3] == ["classify.finish", "classify.start", "settings.key"]


def test_settings_validation_and_version_activation(load_main, admin_client) -> None:
    main, client, headers, _ = _setup(load_main, admin_client)
    bad = client.put("/admin/api/topics/settings", json={"weak_threshold": 3}, headers=headers)
    assert bad.status_code == 422
    unknown = client.put("/admin/api/topics/settings", json={"api_key": "x"}, headers=headers)
    assert unknown.status_code == 400
    good = client.put("/admin/api/topics/settings", json={"include_sensitive": True}, headers=headers)
    assert good.json()["settings"]["include_sensitive"] is True

    first = client.post("/admin/api/topics/versions", json={"topics": MAP, "origin": "import"}, headers=headers)
    first_id = first.json()["version"]["version_id"]
    client.post("/admin/api/topics/versions", json={"topics": {"car": "Auto."}, "parent_id": first_id}, headers=headers)
    assert client.post(f"/admin/api/topics/versions/{first_id}/activate", headers=headers).status_code == 200
    assert client.get("/admin/api/topics").json()["active_version"]["version_id"] == first_id
    assert client.get("/admin/api/topics/versions/999").status_code == 404
    bad_map = client.post("/admin/api/topics/versions", json={"topics": [{"key": "X"}]}, headers=headers)
    assert bad_map.status_code == 422


def test_find_and_proposal_routes(load_main, admin_client) -> None:
    main, client, headers, _ = _setup(load_main, admin_client)
    assert client.post("/admin/api/topics/find", json={}, headers=headers).json()["code"] == "key_missing"
    client.put("/admin/api/topics/key", json={"api_key": "sk-or-secret"}, headers=headers)
    main.topic_runtime.codex = None
    refused = client.post("/admin/api/topics/find", json={"detail": "fine"}, headers=headers)
    assert refused.status_code == 409 and refused.json()["code"] == "codex_unavailable"
    bad = client.post("/admin/api/topics/find", json={"detail": "huge"}, headers=headers)
    assert bad.status_code in (409, 422)
    assert client.get("/admin/api/topics/proposals").json()["proposals"] == []
    assert client.post("/admin/api/topics/proposals/99/accept", json={}, headers=headers).status_code == 404
    overview = client.get("/admin/api/topics").json()
    assert overview["pool_count"] == 2 and overview["pending_proposals"] == 0 and overview["job"] is None


def test_topics_graph_loads_d3_from_bridge_not_a_cdn(admin_client) -> None:
    client, _ = admin_client()
    page = Path("web/topics-viewer.html").read_text(encoding="utf-8")
    assert '<script src="/admin/assets/d3/d3.min.js"></script>' in page
    assert "cdnjs" not in page

    asset = client.get("/admin/assets/d3/d3.min.js")
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("text/javascript")
    assert asset.text.startswith("// https://d3js.org v7.9.0")

    anonymous = TestClient(client.app, base_url=ADMIN_BASE_URL)
    assert anonymous.get("/admin/assets/d3/d3.min.js", follow_redirects=False).status_code == 303
