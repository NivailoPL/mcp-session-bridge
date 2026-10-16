"""Admin API for highlights and strands, and the manual-only Codex strand runs."""
from __future__ import annotations

import asyncio
from typing import Any

from starlette.testclient import TestClient

from tests.conftest import ADMIN_BASE_URL

SESSION = "20261015-200000-organic-synthetic-a1b2c3"


class FakeCodex:
    def __init__(self, result: Any = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def extract_structured(self, message: str, *, model: str, effort: str, output_schema: dict) -> dict:
        self.calls.append({"message": message, "model": model, "effort": effort})
        if self.error:
            raise self.error
        return self.result


def _setup(load_main):
    main = load_main()
    main.store.create_session(SESSION, "Organic vs synthetic")
    ids = [
        main.store.save_exchange(SESSION, "Claude", f"Pytanie {index}", f"Kotwica numer {index}.").exchange_id
        for index in range(4)
    ]
    return main, ids


# ---------- highlights ----------

def test_highlight_endpoints_require_login_and_csrf(load_main, admin_client) -> None:
    main, ids = _setup(load_main)
    anonymous = TestClient(main.app, base_url=ADMIN_BASE_URL)
    body = {"exchange_id": ids[0], "side": "model", "quote": "Kotwica numer 0"}
    assert anonymous.post(f"/admin/api/sessions/{SESSION}/highlights", json=body).status_code == 401
    client, _ = admin_client(main)
    assert client.post(f"/admin/api/sessions/{SESSION}/highlights", json=body).status_code == 403


def test_highlight_round_trip(load_main, admin_client) -> None:
    main, ids = _setup(load_main)
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}

    created = client.post(
        f"/admin/api/sessions/{SESSION}/highlights",
        json={"exchange_id": ids[1], "side": "model", "quote": "Kotwica numer 1"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    highlight = created.json()["highlight"]
    assert highlight["status"] == "ok"

    missing = client.post(
        f"/admin/api/sessions/{SESSION}/highlights",
        json={"exchange_id": ids[1], "side": "user", "quote": "Kotwica numer 1"},
        headers=headers,
    )
    assert missing.status_code == 422 and missing.json()["code"] == "quote_not_found"

    listed = client.get(f"/admin/api/sessions/{SESSION}/highlights").json()["highlights"]
    assert [item["highlight_id"] for item in listed] == [highlight["highlight_id"]]
    assert client.get("/admin/api/sessions/unknown/highlights").status_code == 404

    path = f"/admin/api/highlights/{highlight['highlight_id']}"
    assert client.delete(path, headers=headers).status_code == 200
    assert client.delete(path, headers=headers).status_code == 404


# ---------- strands ----------

def test_manual_strand_crud_and_pending_actions(load_main, admin_client) -> None:
    main, ids = _setup(load_main)
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}

    created = client.post(
        f"/admin/api/sessions/{SESSION}/strands",
        json={"title": "Ręczny", "ranges": [{"start": ids[0], "end": ids[1]}]},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    strand = created.json()["strand"]

    bad = client.post(
        f"/admin/api/sessions/{SESSION}/strands",
        json={"title": "Zły", "ranges": [{"start": ids[2], "end": ids[0]}]},
        headers=headers,
    )
    assert bad.status_code == 422

    updated = client.patch(f"/admin/api/strands/{strand['strand_id']}", json={"title": "Nowy"}, headers=headers)
    assert updated.json()["strand"]["title"] == "Nowy"

    listed = client.get(f"/admin/api/sessions/{SESSION}/strands").json()
    assert listed["latest_run"] is None and len(listed["strands"]) == 1

    assert client.delete(f"/admin/api/strands/{strand['strand_id']}", headers=headers).status_code == 200
    accepted = client.post(f"/admin/api/sessions/{SESSION}/strands/accept", json={}, headers=headers)
    assert accepted.json()["accepted"] == 0


def test_strand_settings_round_trip(load_main, admin_client) -> None:
    main, _ = _setup(load_main)
    client, csrf = admin_client(main)
    current = client.get("/admin/api/settings/strands").json()
    assert current["settings"] == current["defaults"]
    saved = client.put(
        "/admin/api/settings/strands",
        json={"model": "gpt-5.6-sol", "effort": "low", "prompt": "Podziel."},
        headers={"x-csrf-token": csrf},
    )
    assert saved.status_code == 200 and saved.json()["settings"]["effort"] == "low"
    rejected = client.put(
        "/admin/api/settings/strands",
        json={"model": "gpt-5.6-sol", "effort": "max", "prompt": "Podziel."},
        headers={"x-csrf-token": csrf},
    )
    assert rejected.status_code == 422


# ---------- Codex runs ----------

def test_run_stores_pending_proposals_and_revert_removes_them(load_main) -> None:
    main, ids = _setup(load_main)
    manual = main.store.create_strand(SESSION, "Ręczny", [[ids[3], ids[3]]])
    fake = FakeCodex({"strands": [
        {"title": "Taksonomia", "ranges": [{"start": ids[0], "end": ids[1]}]},
        {"title": "Highlighty", "ranges": [{"start": ids[2], "end": ids[2]}]},
    ]})
    main.strand_runtime.codex = fake

    run = asyncio.run(main.strand_runtime.run_now(SESSION, actor="owner"))

    assert run["status"] == "completed" and run["proposed_count"] == 2
    prompt = fake.calls[0]["message"]
    assert f"Exchange {ids[0]}" in prompt and "Organic vs synthetic" not in prompt
    statuses = sorted(item["status"] for item in main.store.list_strands(SESSION))
    assert statuses == ["accepted", "pending", "pending"]
    assert main.store.revert_strand_run(run["run_id"]) == 2
    assert [item["strand_id"] for item in main.store.list_strands(SESSION)] == [manual["strand_id"]]


def test_invalid_codex_result_fails_the_run_and_stores_nothing(load_main) -> None:
    main, ids = _setup(load_main)
    main.strand_runtime.codex = FakeCodex({"strands": [{"title": "X", "ranges": [{"start": 999_999, "end": 999_999}]}]})
    run = asyncio.run(main.strand_runtime.run_now(SESSION, actor="owner"))
    assert run["status"] == "failed" and run["error_code"] == "range_unknown"
    assert main.store.list_strands(SESSION) == []


def test_codex_errors_fail_the_run_with_a_readable_message(load_main) -> None:
    from app.codex_app_server import CodexUnavailableError

    main, _ = _setup(load_main)
    main.strand_runtime.codex = FakeCodex(error=CodexUnavailableError("down"))
    run = asyncio.run(main.strand_runtime.run_now(SESSION, actor="owner"))
    assert run["status"] == "failed" and run["error_code"] == "codex_unavailable"
    assert "Nothing was changed" in run["error_message"]


def test_sensitive_session_needs_confirmation_and_pending_blocks_new_runs(load_main, admin_client) -> None:
    main, ids = _setup(load_main)
    group = main.store.create_session_group("Prywatne", "#b4553f", "folder")
    main.store.update_session_group(group.group_id, is_sensitive=True)
    main.store.set_session_group(SESSION, group.group_id)
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}

    refused = client.post(f"/admin/api/sessions/{SESSION}/strand-runs", json={}, headers=headers)
    assert refused.status_code == 409 and refused.json()["code"] == "sensitive_unconfirmed"

    main.strand_runtime.codex = FakeCodex({"strands": [{"title": "A", "ranges": [{"start": ids[0], "end": ids[0]}]}]})
    run = asyncio.run(main.strand_runtime.run_now(SESSION, actor="owner", confirm_sensitive=True))
    assert run["status"] == "completed"

    blocked = client.post(
        f"/admin/api/sessions/{SESSION}/strand-runs", json={"confirm_sensitive": True}, headers=headers
    )
    assert blocked.status_code == 409 and blocked.json()["code"] == "pending_exists"


def test_saving_exchanges_never_starts_a_strand_run(load_main) -> None:
    main, _ = _setup(load_main)
    fake = FakeCodex({"strands": []})
    main.strand_runtime.codex = fake
    main.store.save_exchange(SESSION, "Claude", "Jeszcze jedno", "Odpowiedź")
    assert fake.calls == []
    assert main.store.latest_strand_run(SESSION) is None


def test_token_feature_switches_gate_codex_runs(load_main, admin_client) -> None:
    main, ids = _setup(load_main)
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}
    assert client.get("/admin/api/settings/token-features").json()["features"] == {
        "enabled": True, "strands": True, "strands_active": True,
    }
    main.strand_runtime.codex = FakeCodex({"strands": [{"title": "A", "ranges": [{"start": ids[0], "end": ids[0]}]}]})

    for switch in ({"strands": False}, {"enabled": False, "strands": True}):
        saved = client.put("/admin/api/settings/token-features", json=switch, headers=headers).json()["features"]
        assert saved["strands_active"] is False
        refused = client.post(f"/admin/api/sessions/{SESSION}/strand-runs", json={}, headers=headers)
        assert refused.status_code == 409 and refused.json()["code"] == "feature_off"
        assert client.get(f"/admin/api/sessions/{SESSION}/strands").json()["strands_enabled"] is False
    assert fake_calls(main) == []

    client.put("/admin/api/settings/token-features", json={"enabled": True}, headers=headers)
    assert client.put("/admin/api/settings/token-features", json={"enabled": "yes"}, headers=headers).status_code == 422
    manual = client.post(
        f"/admin/api/sessions/{SESSION}/strands",
        json={"title": "Ręczny działa zawsze", "ranges": [{"start": ids[0], "end": ids[0]}]},
        headers=headers,
    )
    assert manual.status_code == 200


def fake_calls(main):
    return main.strand_runtime.codex.calls


# ---------- chapter state and incremental updates ----------

def _first_run(main, ids):
    main.strand_runtime.codex = FakeCodex({"strands": [
        {"title": "Początek", "ranges": [{"start": ids[0], "end": ids[1]}]},
        {"title": "Dalej", "ranges": [{"start": ids[2], "end": ids[3]}]},
    ]})
    return asyncio.run(main.strand_runtime.run_now(SESSION, actor="owner"))


def test_chapter_state_moves_from_none_to_review_complete_and_behind(load_main) -> None:
    main, ids = _setup(load_main)
    assert main.store.chapter_state(SESSION)["state"] == "none"
    _first_run(main, ids)
    assert main.store.chapter_state(SESSION)["state"] == "review"
    main.store.accept_strands(SESSION)
    state = main.store.chapter_state(SESSION)
    assert state["state"] == "complete" and state["chapter_count"] == 2 and state["new_count"] == 0
    main.store.save_exchange(SESSION, "Claude", "Nowe pytanie", "Nowa odpowiedź")
    main.store.save_exchange(SESSION, "Claude", "Jeszcze", "Odpowiedź")
    state = main.store.chapter_state(SESSION)
    assert state["state"] == "behind" and state["new_count"] == 2


def test_discarding_every_proposal_still_counts_as_analyzed(load_main) -> None:
    main, ids = _setup(load_main)
    _first_run(main, ids)
    main.store.discard_pending_strands(SESSION)
    assert main.store.chapter_state(SESSION)["state"] == "complete"


def test_manual_only_and_revert_states(load_main) -> None:
    main, ids = _setup(load_main)
    main.store.create_strand(SESSION, "Ręczny", [[ids[0], ids[0]]])
    assert main.store.chapter_state(SESSION)["state"] == "manual"
    run = _first_run(main, ids)
    main.store.accept_strands(SESSION)
    main.store.revert_strand_run(run["run_id"])
    assert main.store.chapter_state(SESSION)["state"] == "manual"


def test_update_run_sends_only_new_exchanges_and_extends_chapters(load_main) -> None:
    main, ids = _setup(load_main)
    _first_run(main, ids)
    main.store.accept_strands(SESSION)
    chapters = {item["title"]: item["strand_id"] for item in main.store.list_strands(SESSION)}
    new = [main.store.save_exchange(SESSION, "Claude", f"Nowe {i}", f"Odp {i}").exchange_id for i in range(3)]
    fake = FakeCodex({
        "extend": [{"chapter_id": chapters["Dalej"], "ranges": [{"start": new[0], "end": new[1]}]}],
        "new": [{"title": "Nowy wątek", "ranges": [{"start": new[2], "end": new[2]}]}],
    })
    main.strand_runtime.codex = fake

    run = asyncio.run(main.strand_runtime.run_now(SESSION, actor="owner"))

    assert run["status"] == "completed" and run["mode"] == "update" and run["exchange_count"] == 3
    prompt = fake.calls[0]["message"]
    assert f"Exchange {new[0]}" in prompt and f"Chapter {chapters['Dalej']}: Dalej" in prompt
    assert f"Exchange {ids[0]}\n" not in prompt  # old exchanges are not resent, only the last two as context
    dalej = next(item for item in main.store.list_strands(SESSION) if item["title"] == "Dalej")
    assert [(r["start_exchange_id"], r["end_exchange_id"]) for r in dalej["pending_ranges"]] == [(new[0], new[1])]
    assert main.store.chapter_state(SESSION)["state"] == "review"

    assert main.store.accept_strands(SESSION) == 2
    dalej = next(item for item in main.store.list_strands(SESSION) if item["title"] == "Dalej")
    assert len(dalej["ranges"]) == 2 and dalej["pending_ranges"] == []
    assert main.store.chapter_state(SESSION)["state"] == "complete"

    main.store.revert_strand_run(run["run_id"])
    titles = sorted(item["title"] for item in main.store.list_strands(SESSION))
    dalej = next(item for item in main.store.list_strands(SESSION) if item["title"] == "Dalej")
    assert titles == ["Dalej", "Początek"] and len(dalej["ranges"]) == 1


def test_update_rejects_ranges_outside_the_new_exchanges(load_main) -> None:
    main, ids = _setup(load_main)
    _first_run(main, ids)
    main.store.accept_strands(SESSION)
    new = main.store.save_exchange(SESSION, "Claude", "Nowe", "Odp").exchange_id
    main.strand_runtime.codex = FakeCodex({"extend": [], "new": [{"title": "Zły", "ranges": [{"start": ids[0], "end": new}]}]})
    run = asyncio.run(main.strand_runtime.run_now(SESSION, actor="owner"))
    assert run["status"] == "failed" and run["error_code"] == "range_unknown"


def test_complete_session_refuses_a_new_run_but_redo_starts_over(load_main) -> None:
    from app.strands import StrandError

    main, ids = _setup(load_main)
    main.store.create_strand(SESSION, "Ręczny", [[ids[3], ids[3]]])
    _first_run(main, ids)
    main.store.accept_strands(SESSION)
    try:
        asyncio.run(main.strand_runtime.run_now(SESSION, actor="owner"))
        raise AssertionError("expected already_complete")
    except StrandError as exc:
        assert exc.code == "already_complete"

    main.strand_runtime.codex = FakeCodex({"strands": [{"title": "Od nowa", "ranges": [{"start": ids[0], "end": ids[3]}]}]})
    run = asyncio.run(main.strand_runtime.run_now(SESSION, actor="owner", redo=True))
    assert run["mode"] == "full"
    titles = sorted(item["title"] for item in main.store.list_strands(SESSION))
    assert titles == ["Od nowa", "Ręczny"]


def test_session_list_carries_chapter_state(load_main, admin_client) -> None:
    main, ids = _setup(load_main)
    _first_run(main, ids)
    main.store.accept_strands(SESSION)
    client, _ = admin_client(main)
    sessions = client.get("/admin/api/sessions").json()["sessions"]
    listed = next(item for item in sessions if item["session_id"] == SESSION)
    assert listed["chapters"]["state"] == "complete"
    payload = client.get(f"/admin/api/sessions/{SESSION}/strands").json()
    assert payload["chapter_state"]["analyzed_count"] == 4
