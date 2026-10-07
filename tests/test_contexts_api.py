"""The get_context MCP tool and the admin API behind the Contexts workspace."""
from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from tests.conftest import ADMIN_BASE_URL


SESSION_A = "20261005-120000-projekt-dzialu-contexts-a1b2c3"
SESSION_B = "20260922-081530-redakcja-id-0f9e8d"


def _setup(load_main):
    main = load_main()
    main.store.create_session(SESSION_A, "Projekt działu Contexts")
    main.store.create_session(SESSION_B, "Redakcja ID")
    main.store.save_exchange(SESSION_A, "Claude", f"Claude, tutaj: {SESSION_A}", f"Patrz {SESSION_B}.")
    main.store.save_exchange(SESSION_B, "ChatGPT", "Hej", "https://example.test/admin/sessions")
    return main


def _call_tool(main, name: str, arguments: dict):
    async def call():
        return await main.mcp._tool_manager.call_tool(name, arguments, convert_result=False)

    return asyncio.run(call())


# ---------- MCP ----------

def test_get_context_returns_redacted_chunks_without_naming_sources(load_main) -> None:
    main = _setup(load_main)
    ctx = main.store.create_context("Tajna nazwa kontekstu")["context_id"]
    client_store = main.store
    from app.contexts import build_redactor, snapshot_session

    redactor = build_redactor(client_store, public_base_url="https://example.test")
    client_store.add_context_block(ctx, snapshot_session(client_store, SESSION_A, redactor=redactor))
    client_store.add_context_block(ctx, snapshot_session(client_store, SESSION_B, redactor=redactor))

    result = _call_tool(main, "get_context", {"context_id": ctx})

    assert result["ok"] is True
    text = result["context_markdown"]
    assert result["chunk_index"] == 1 and result["block_count"] == 2
    assert "## Session A · Projekt działu Contexts" in text
    for secret in (SESSION_A, SESSION_B, ctx, "example.test", "Tajna nazwa"):
        assert secret not in text
    assert set(result) >= {"token_count", "chunk_count", "has_more", "next_chunk_index", "context_sha256"}


def test_get_context_reports_unknown_ids_and_bad_chunks(load_main) -> None:
    main = _setup(load_main)
    ctx = main.store.create_context("A")["context_id"]

    assert _call_tool(main, "get_context", {"context_id": "ctx_doesnotexist00"})["ok"] is False
    assert _call_tool(main, "get_context", {"context_id": ctx, "chunk_index": 5})["ok"] is False


def test_get_context_is_listed_and_mentioned_in_server_instructions(load_main) -> None:
    main = load_main()

    async def names():
        return {tool.name for tool in await main.mcp.list_tools()}

    assert "get_context" in asyncio.run(names())
    assert "get_context" in main.SERVER_INSTRUCTIONS
    assert "list_contexts" not in asyncio.run(names())


# ---------- admin API ----------

def test_context_api_requires_login_and_csrf(load_main, admin_client) -> None:
    main = _setup(load_main)
    anonymous = TestClient(main.app, base_url=ADMIN_BASE_URL)
    assert anonymous.get("/admin/api/contexts").status_code == 401

    client, _ = admin_client(main)
    assert client.post("/admin/api/contexts", json={"name": "A"}).status_code == 403


def test_build_a_context_end_to_end(load_main, admin_client) -> None:
    main = _setup(load_main)
    saved = main.store.save_session_file(SESSION_A, "brief.md", "# Brief")
    client, csrf = admin_client(main)
    h = {"x-csrf-token": csrf}

    ctx = client.post("/admin/api/contexts", json={"name": "Fresh start", "color": "#7a7df0"}, headers=h).json()["context"]
    cid = ctx["context_id"]
    a = client.post(f"/admin/api/contexts/{cid}/blocks", json={"source_type": "session", "session_id": SESSION_A}, headers=h)
    f = client.post(f"/admin/api/contexts/{cid}/blocks", json={"source_type": "file", "file_id": saved.file_id, "position": 0}, headers=h)
    dup = client.post(f"/admin/api/contexts/{cid}/blocks", json={"source_type": "session", "session_id": SESSION_A}, headers=h)

    assert a.status_code == 200 and f.status_code == 200
    assert dup.status_code == 409
    blocks = f.json()["context"]["blocks"]
    assert [b["source_type"] for b in blocks] == ["file", "session"]
    assert all(b["status"] == "current" for b in blocks)
    assert "content" not in blocks[0]

    preview = client.get(f"/admin/api/contexts/{cid}/preview").json()
    assert preview["markdown"].startswith("# Context")
    assert SESSION_A not in preview["markdown"]
    for r in preview["redactions"]:
        assert preview["markdown"][r["start"]:r["end"]] == r["placeholder"]

    listed = client.get("/admin/api/contexts").json()["contexts"]
    assert [c["name"] for c in listed] == ["Fresh start"]


def test_out_of_date_block_refreshes(load_main, admin_client) -> None:
    main = _setup(load_main)
    client, csrf = admin_client(main)
    h = {"x-csrf-token": csrf}
    cid = client.post("/admin/api/contexts", json={}, headers=h).json()["context"]["context_id"]
    block = client.post(
        f"/admin/api/contexts/{cid}/blocks", json={"source_type": "session", "session_id": SESSION_B}, headers=h
    ).json()["block"]

    main.store.save_exchange(SESSION_B, "ChatGPT", "Jeszcze", "Dobrze")
    assert client.get("/admin/api/contexts").json()["contexts"][0]["blocks"][0]["status"] == "outdated"

    refreshed = client.post(f"/admin/api/context-blocks/{block['block_id']}/refresh", headers=h).json()
    assert refreshed["block"]["status"] == "current"


def test_lock_blocks_changes_through_the_api(load_main, admin_client) -> None:
    main = _setup(load_main)
    client, csrf = admin_client(main)
    h = {"x-csrf-token": csrf}
    cid = client.post("/admin/api/contexts", json={"name": "A"}, headers=h).json()["context"]["context_id"]
    block = client.post(
        f"/admin/api/contexts/{cid}/blocks", json={"source_type": "session", "session_id": SESSION_A}, headers=h
    ).json()["block"]

    locked = client.patch(f"/admin/api/contexts/{cid}", json={"is_locked": True}, headers=h)
    assert locked.json()["context"]["is_locked"] is True
    assert client.patch(f"/admin/api/contexts/{cid}", json={"name": "B"}, headers=h).status_code == 409
    assert client.delete(f"/admin/api/context-blocks/{block['block_id']}", headers=h).status_code == 409
    assert client.delete(f"/admin/api/contexts/{cid}", headers=h).status_code == 409
    assert client.post(
        f"/admin/api/contexts/{cid}/blocks", json={"source_type": "session", "session_id": SESSION_B}, headers=h
    ).status_code == 409


def test_move_copy_reorder_and_undo(load_main, admin_client) -> None:
    main = _setup(load_main)
    client, csrf = admin_client(main)
    h = {"x-csrf-token": csrf}
    a = client.post("/admin/api/contexts", json={"name": "A"}, headers=h).json()["context"]["context_id"]
    b = client.post("/admin/api/contexts", json={"name": "B"}, headers=h).json()["context"]["context_id"]
    first = client.post(f"/admin/api/contexts/{a}/blocks", json={"source_type": "session", "session_id": SESSION_A}, headers=h).json()["block"]
    second = client.post(f"/admin/api/contexts/{a}/blocks", json={"source_type": "session", "session_id": SESSION_B}, headers=h).json()["block"]

    reordered = client.patch(f"/admin/api/context-blocks/{second['block_id']}", json={"context_id": a, "position": 0}, headers=h).json()
    assert [blk["block_id"] for blk in reordered["contexts"][0]["blocks"]] == [second["block_id"], first["block_id"]]

    copied = client.patch(f"/admin/api/context-blocks/{first['block_id']}", json={"context_id": b, "copy": True}, headers=h).json()
    by_id = {c["context_id"]: c for c in copied["contexts"]}
    assert len(by_id[a]["blocks"]) == 2 and len(by_id[b]["blocks"]) == 1

    removed = client.delete(f"/admin/api/context-blocks/{second['block_id']}", headers=h).json()["block"]
    undone = client.post(f"/admin/api/contexts/{a}/blocks", json={"restore": removed, "position": removed["position"]}, headers=h).json()
    assert [blk["source_session_id"] for blk in undone["context"]["blocks"]] == [SESSION_B, SESSION_A]

    order = client.put("/admin/api/contexts", json={"context_ids": [b, a]}, headers=h).json()["contexts"]
    assert [c["name"] for c in order] == ["B", "A"]


def test_restore_rejects_a_forged_block(load_main, admin_client) -> None:
    main = _setup(load_main)
    client, csrf = admin_client(main)
    h = {"x-csrf-token": csrf}
    cid = client.post("/admin/api/contexts", json={}, headers=h).json()["context"]["context_id"]
    forged = {
        "source_type": "session", "source_key": "file:1", "source_session_id": SESSION_A, "source_file_id": None,
        "title": "x", "content": "x", "source_hash": "x", "found_ids": {}, "token_count": 1,
    }

    assert client.post(f"/admin/api/contexts/{cid}/blocks", json={"restore": forged}, headers=h).status_code == 400


def test_bad_payloads_are_rejected(load_main, admin_client) -> None:
    main = _setup(load_main)
    client, csrf = admin_client(main)
    h = {"x-csrf-token": csrf}
    cid = client.post("/admin/api/contexts", json={}, headers=h).json()["context"]["context_id"]

    assert client.post("/admin/api/contexts", json={"name": "A", "extra": 1}, headers=h).status_code == 400
    assert client.patch(f"/admin/api/contexts/{cid}", json={"is_locked": "yes"}, headers=h).status_code == 400
    assert client.post(f"/admin/api/contexts/{cid}/blocks", json={"source_type": "image"}, headers=h).status_code == 400
    assert client.post(f"/admin/api/contexts/{cid}/blocks", json={"source_type": "session", "session_id": "nope"}, headers=h).status_code == 404
    assert client.patch("/admin/api/context-blocks/999", json={"context_id": cid}, headers=h).status_code == 404
    assert client.patch(f"/admin/api/contexts/ctx_unknownunknown0", json={"name": "x"}, headers=h).status_code == 404


def test_library_lists_sessions_with_models_and_files_with_previews(load_main, admin_client) -> None:
    main = _setup(load_main)
    main.store.save_exchange(SESSION_A, "Codex", "q", "a")
    main.store.save_session_file(SESSION_A, "brief.md", "# Brief\n" + "x" * 2000)
    main.store.save_group_file_for_session(SESSION_B, "group.md", "Group note")
    client, _ = admin_client(main)

    sessions = client.get("/admin/api/context-library?kind=sessions").json()
    files = client.get("/admin/api/context-library?kind=files").json()

    by_id = {s["session_id"]: s for s in sessions["items"]}
    assert by_id[SESSION_A]["models"] == ["Claude", "Codex"]
    assert by_id[SESSION_A]["token_estimate"] > 0
    assert sessions["groups"]
    names = {f["filename"]: f for f in files["items"]}
    assert len(names["brief.md"]["preview"]) == 600
    assert names["brief.md"]["effective_group_id"] == "uncategorized"
    assert names["group.md"]["effective_group_id"] == "uncategorized"
    assert client.get("/admin/api/context-library?kind=images").status_code == 400
