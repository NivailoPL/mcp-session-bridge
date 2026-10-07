import sqlite3

import pytest

from app.context_redaction import Redactor
from app.contexts import (
    BLOCK_STATUS_CURRENT,
    BLOCK_STATUS_MISSING,
    BLOCK_STATUS_OUTDATED,
    CONTEXT_FRAMING,
    ContextSourceError,
    assemble_context,
    block_status,
    context_chunk,
    count_tokens,
    snapshot_file,
    snapshot_session,
)
from app.session_package import MASKED_MODEL_RESPONSE
from app.storage import (
    ContextBlockConflictError,
    ContextLockedError,
    ContextNotFoundError,
    Store,
)


SESSION_A = "20261005-120000-projekt-dzialu-contexts-a1b2c3"
SESSION_B = "20260922-081530-redakcja-id-0f9e8d"
HOST = "https://mcp.example.test"


@pytest.fixture()
def store(tmp_path) -> Store:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session(SESSION_A, "Projekt działu Contexts")
    store.create_session(SESSION_B, "Redakcja ID")
    store.save_exchange(SESSION_A, "Claude", "Zacznijmy dział Contexts.", f"Szkic jest w sesji {SESSION_B}.")
    store.save_exchange(SESSION_A, "ChatGPT", "A link?", f"{HOST}/admin/sessions?session={SESSION_A}")
    store.save_exchange(SESSION_B, "Codex", "Model zagrożeń?", "Tylko ID sesji otwiera dostęp.")
    return store


def redactor(store: Store) -> Redactor:
    return Redactor(session_ids=store.list_session_ids(), context_ids=store.list_context_ids(), hosts=[HOST])


def add_session(store: Store, context_id: str, session_id: str, **kwargs):
    return store.add_context_block(context_id, snapshot_session(store, session_id, redactor=redactor(store)), **kwargs)


def add_file(store: Store, context_id: str, file_id: int, **kwargs):
    return store.add_context_block(context_id, snapshot_file(store, file_id, redactor=redactor(store)), **kwargs)


# ---------- contexts ----------

def test_create_context_gets_an_unguessable_id_and_a_default_name(store) -> None:
    first = store.create_context(color="#7A7DF0")
    second = store.create_context("Rozmowa o pracę", "#c084fc")

    assert first["context_id"].startswith("ctx_") and len(first["context_id"]) == 20
    assert first["context_id"] != second["context_id"]
    assert first["name"] == "Context 1"
    assert first["color"] == "#7a7df0"
    assert [c["name"] for c in store.list_contexts()] == ["Context 1", "Rozmowa o pracę"]


def test_context_validates_name_and_color(store) -> None:
    with pytest.raises(ValueError, match="hex"):
        store.create_context("x", "blue")
    with pytest.raises(ValueError, match="80 characters"):
        store.create_context("x" * 81)


def test_tables_are_additive_and_keep_schema_version(store) -> None:
    assert store.schema_version() == 2
    with sqlite3.connect(store.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"contexts", "context_blocks"} <= tables


def test_rename_recolor_and_reorder(store) -> None:
    a = store.create_context("A")
    b = store.create_context("B")

    updated = store.update_context(a["context_id"], name="  Fresh   start ", color="#2dd4bf")
    ordered = store.reorder_contexts([b["context_id"], a["context_id"]])

    assert updated["name"] == "Fresh start"
    assert updated["color"] == "#2dd4bf"
    assert [c["name"] for c in ordered] == ["B", "Fresh start"]
    with pytest.raises(ValueError, match="exactly once"):
        store.reorder_contexts([a["context_id"]])


def test_delete_context_removes_its_blocks(store) -> None:
    ctx = store.create_context("A")
    add_session(store, ctx["context_id"], SESSION_A)

    store.delete_context(ctx["context_id"])

    assert store.get_context(ctx["context_id"]) is None
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM context_blocks").fetchone()[0] == 0


def test_unknown_context_raises(store) -> None:
    with pytest.raises(ContextNotFoundError):
        store.update_context("ctx_nope", name="x")


# ---------- the lock ----------

def test_locked_context_refuses_every_content_change(store) -> None:
    ctx = store.create_context("A")
    other = store.create_context("B")
    block = add_session(store, ctx["context_id"], SESSION_A)
    store.update_context(ctx["context_id"], is_locked=True)
    snapshot = snapshot_session(store, SESSION_B, redactor=redactor(store))

    for change in (
        lambda: store.add_context_block(ctx["context_id"], snapshot),
        lambda: store.delete_context_block(block["block_id"]),
        lambda: store.move_context_block(block["block_id"], context_id=ctx["context_id"], position=0),
        lambda: store.move_context_block(block["block_id"], context_id=other["context_id"]),
        lambda: store.refresh_context_block(block["block_id"], snapshot_session(store, SESSION_A, redactor=redactor(store))),
        lambda: store.update_context(ctx["context_id"], name="renamed"),
        lambda: store.update_context(ctx["context_id"], color="#000000"),
        lambda: store.delete_context(ctx["context_id"]),
    ):
        with pytest.raises(ContextLockedError):
            change()

    assert [b["block_id"] for b in store.get_context(ctx["context_id"])["blocks"]] == [block["block_id"]]


def test_locked_context_can_be_copied_from_and_unlocked(store) -> None:
    ctx = store.create_context("A")
    other = store.create_context("B")
    block = add_session(store, ctx["context_id"], SESSION_A)
    store.update_context(ctx["context_id"], is_locked=True)

    copied = store.move_context_block(block["block_id"], context_id=other["context_id"], copy=True)
    unlocked = store.update_context(ctx["context_id"], is_locked=False, name="Renamed on unlock")

    assert copied["context_id"] == other["context_id"]
    assert unlocked["is_locked"] is False
    assert unlocked["name"] == "Renamed on unlock"


# ---------- blocks ----------

def test_blocks_keep_order_and_positions_stay_contiguous(store) -> None:
    ctx = store.create_context("A")["context_id"]
    file_row = store.save_session_file(SESSION_A, "brief.md", "# Brief\nText.")
    a = add_session(store, ctx, SESSION_A)
    b = add_session(store, ctx, SESSION_B)
    f = add_file(store, ctx, file_row.file_id, position=0)

    blocks = store.get_context(ctx)["blocks"]
    assert [x["block_id"] for x in blocks] == [f["block_id"], a["block_id"], b["block_id"]]
    assert [x["position"] for x in blocks] == [0, 1, 2]

    store.move_context_block(f["block_id"], context_id=ctx, position=99)
    store.delete_context_block(a["block_id"])

    blocks = store.get_context(ctx)["blocks"]
    assert [x["block_id"] for x in blocks] == [b["block_id"], f["block_id"]]
    assert [x["position"] for x in blocks] == [0, 1]


def test_same_source_cannot_appear_twice_in_one_context(store) -> None:
    ctx = store.create_context("A")["context_id"]
    add_session(store, ctx, SESSION_A)

    with pytest.raises(ContextBlockConflictError):
        add_session(store, ctx, SESSION_A)


def test_move_between_contexts_and_copy(store) -> None:
    a = store.create_context("A")["context_id"]
    b = store.create_context("B")["context_id"]
    first = add_session(store, a, SESSION_A)
    second = add_session(store, a, SESSION_B)

    moved = store.move_context_block(first["block_id"], context_id=b)
    copied = store.move_context_block(second["block_id"], context_id=b, position=0, copy=True)

    assert moved["block_id"] == first["block_id"]
    assert [x["source_session_id"] for x in store.get_context(a)["blocks"]] == [SESSION_B]
    assert [x["source_session_id"] for x in store.get_context(b)["blocks"]] == [SESSION_B, SESSION_A]
    assert copied["block_id"] != second["block_id"]
    with pytest.raises(ContextBlockConflictError):
        store.move_context_block(second["block_id"], context_id=b)


def test_deleted_block_comes_back_whole_for_undo(store) -> None:
    ctx = store.create_context("A")["context_id"]
    block = add_session(store, ctx, SESSION_A)

    removed = store.delete_context_block(block["block_id"])

    assert removed["content"].startswith("### USER")
    assert removed["title"] == "Projekt działu Contexts"


# ---------- snapshots ----------

def test_session_snapshot_names_speakers_and_skips_metadata(store) -> None:
    snap = snapshot_session(store, SESSION_A, redactor=redactor(store))

    assert snap.source_key == f"session:{SESSION_A}"
    assert "### USER" in snap.content and "### Claude" in snap.content and "### ChatGPT" in snap.content
    assert "exchange_id" not in snap.content and "group_id" not in snap.content
    assert snap.found_ids == {"SESSION": sorted([SESSION_A, SESSION_B])}
    assert snap.token_count == count_tokens(snap.content) > 0


def test_session_snapshot_keeps_masks_and_drops_deleted_exchanges(store) -> None:
    exchanges = store.list_exchanges(SESSION_B)
    store.save_exchange(SESSION_B, "Codex", "Usuń mnie", "Do usunięcia")
    store.mask_exchange_response(exchanges[0].exchange_id)
    store.delete_exchange(store.list_exchanges(SESSION_B)[-1].exchange_id)

    snap = snapshot_session(store, SESSION_B, redactor=redactor(store))

    assert MASKED_MODEL_RESPONSE in snap.content
    assert "Tylko ID sesji" not in snap.content
    assert "Usuń mnie" not in snap.content


def test_pdf_and_text_files_snapshot_their_text_and_images_are_refused(store) -> None:
    text = store.save_session_file(SESSION_A, "notes.md", "# Notes\nbody")

    snap = snapshot_file(store, text.file_id, redactor=redactor(store))

    assert snap.title == "notes.md"
    assert snap.content == "# Notes\nbody\n"
    assert snap.source_hash == text.sha256
    with pytest.raises(ContextSourceError):
        snapshot_file(store, 999, redactor=redactor(store))


def test_unknown_session_is_refused(store) -> None:
    with pytest.raises(ContextSourceError):
        snapshot_session(store, "20990101-000000-nope-abcdef", redactor=redactor(store))


# ---------- out of date ----------

def test_block_goes_out_of_date_when_the_session_grows_or_is_edited(store) -> None:
    ctx = store.create_context("A")["context_id"]
    block = add_session(store, ctx, SESSION_B)
    assert block_status(store, block) == BLOCK_STATUS_CURRENT

    store.save_exchange(SESSION_B, "Codex", "Jeszcze jedno", "Dobrze")
    assert block_status(store, block) == BLOCK_STATUS_OUTDATED

    refreshed = store.refresh_context_block(block["block_id"], snapshot_session(store, SESSION_B, redactor=redactor(store)))
    assert block_status(store, refreshed) == BLOCK_STATUS_CURRENT

    store.mask_exchange_response(store.list_exchanges(SESSION_B)[0].exchange_id)
    assert block_status(store, refreshed) == BLOCK_STATUS_OUTDATED


def test_file_block_tracks_edits_and_deletion(store) -> None:
    ctx = store.create_context("A")["context_id"]
    saved = store.save_session_file(SESSION_A, "notes.md", "v1")
    block = add_file(store, ctx, saved.file_id)

    store.update_session_file(saved.file_id, "v2", expected_sha256=saved.sha256)
    assert block_status(store, block) == BLOCK_STATUS_OUTDATED

    store.delete_session_file(saved.file_id)
    assert block_status(store, block) == BLOCK_STATUS_MISSING
    assert store.get_context(ctx)["blocks"][0]["title"] == "notes.md"


def test_refresh_must_come_from_the_same_source(store) -> None:
    ctx = store.create_context("A")["context_id"]
    block = add_session(store, ctx, SESSION_A)

    with pytest.raises(ValueError, match="same source"):
        store.refresh_context_block(block["block_id"], snapshot_session(store, SESSION_B, redactor=redactor(store)))


# ---------- context.md ----------

def test_assembled_context_is_framed_lettered_and_redacted(store) -> None:
    ctx = store.create_context("Fresh start: tajna nazwa")["context_id"]
    saved = store.save_session_file(SESSION_A, "brief.md", f"Patrz {ctx} oraz {SESSION_A}.")
    add_session(store, ctx, SESSION_A)
    add_file(store, ctx, saved.file_id)

    assembled = assemble_context(store, ctx, public_base_url=HOST)
    md = assembled.markdown

    assert md.startswith("# Context\n\n" + CONTEXT_FRAMING)
    assert "## Session A · Projekt działu Contexts" in md
    assert "## File B · brief.md" in md
    assert "tajna nazwa" not in md
    for secret in (SESSION_A, SESSION_B, ctx, "mcp.example.test"):
        assert secret not in md
    assert "[SESSION-1]" in md and "[SESSION-2]" in md and "[URL-1]" in md and "[CONTEXT-1]" in md
    assert assembled.token_count == count_tokens(md)
    assert assembled.block_count == 2


def test_ids_found_at_snapshot_stay_redacted_after_the_source_disappears(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session(SESSION_A, "A")
    gone = "SESSION-NAMED-gone-but-remembered"
    store.save_exchange(SESSION_A, "Claude", "q", f"See {gone}")
    ctx = store.create_context("A")["context_id"]
    snap = snapshot_session(store, SESSION_A, redactor=Redactor(session_ids=[gone]))
    store.add_context_block(ctx, snap)

    md = assemble_context(store, ctx).markdown

    assert gone not in md
    assert "[SESSION-" in md


def test_empty_context_still_assembles(store) -> None:
    ctx = store.create_context("A")["context_id"]

    assert "_This context is empty._" in assemble_context(store, ctx).markdown


def test_context_chunks_cover_the_whole_document(store) -> None:
    ctx = store.create_context("A")["context_id"]
    add_session(store, ctx, SESSION_A)
    add_session(store, ctx, SESSION_B)
    assembled = assemble_context(store, ctx, public_base_url=HOST)

    first = context_chunk(assembled, 1, max_lines=6, max_chars=10_000)
    chunks = [first["context_markdown"]]
    index = first["next_chunk_index"]
    while index:
        part = context_chunk(assembled, index, max_lines=6, max_chars=10_000)
        chunks.append(part["context_markdown"])
        index = part["next_chunk_index"]

    assert first["chunk_count"] == len(chunks) > 1
    assert "".join(chunks) == assembled.markdown
    assert set(first) >= {"context_id", "token_count", "has_more", "context_sha256"}
    with pytest.raises(ValueError):
        context_chunk(assembled, 0, max_lines=6, max_chars=10_000)
