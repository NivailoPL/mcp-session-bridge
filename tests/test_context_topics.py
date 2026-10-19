"""Topics as context blocks: snapshot, trimming of turns already above, sensitive groups, status."""
from __future__ import annotations

import sqlite3

from app.context_topics import library_topics, snapshot_topic
from app.contexts import assemble_context, block_status_detail, build_redactor, snapshot_session
from app.storage import Store
from tests.test_topic_proposals import SESSIONS, ScriptedCodex, find_and_wait, first_map, lab, runtime_with  # noqa: F401


def accepted_map(store, ids):
    rt = runtime_with(store, ScriptedCodex(first_map(ids)))
    find_and_wait(rt)
    rt.accept(store.list_topic_proposals()[0]["proposal_id"], actor="owner", note="")
    return rt


def test_topic_block_brings_its_turns_and_skips_those_already_above(lab) -> None:
    store, ids = lab
    accepted_map(store, ids)
    redactor = build_redactor(store)
    topic = snapshot_topic(store, "radio_job", redactor=redactor)
    assert topic.source_type == "topic" and topic.title == "Radio"
    assert "radio grafik dyżurów" in topic.content and "radio mobbing kierownik" in topic.content
    assert "jelita" not in topic.content
    assert topic.meta["session_count"] == 2 and topic.meta["chapter_count"] == 3

    context = store.create_context("t")
    session = snapshot_session(store, "20261018-100000-radio-a", redactor=redactor)
    store.add_context_block(context["context_id"], session)
    store.add_context_block(context["context_id"], topic)
    markdown = assemble_context(store, context["context_id"]).markdown
    assert "## Topic B · Radio" in markdown
    topic_part = markdown.split("## Topic B · Radio", 1)[1]
    assert "radio grafik dyżurów" not in topic_part and "radio mobbing kierownik" in topic_part

    library = {item["topic_key"]: item for item in library_topics(store)}
    assert library["radio_job"]["chapter_count"] == 3 and "unsorted" not in library


def test_sensitive_chapters_stay_out_unless_asked(lab) -> None:
    store, ids = lab
    accepted_map(store, ids)
    group = store.create_session_group("Prywatne", "#7a7df0", "folder")
    store.set_session_group("20261018-110000-radio-b", group.group_id)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE session_groups SET is_sensitive = 1 WHERE group_id = ?", (group.group_id,))
    redactor = build_redactor(store)
    plain = snapshot_topic(store, "radio_job", redactor=redactor)
    assert "radio mobbing kierownik" not in plain.content and plain.meta["sensitive_left_out"] == 1
    card = next(i for i in library_topics(store) if i["topic_key"] == "radio_job")
    assert card["sensitive_count"] == 1 and card["chapter_count"] == 2
    store.set_topic_settings({"include_sensitive": True})
    assert snapshot_topic(store, "radio_job", redactor=redactor).meta["sensitive_left_out"] == 0
    assert next(i for i in library_topics(store) if i["topic_key"] == "radio_job")["chapter_count"] == 3
    store.set_topic_settings({"include_sensitive": False})
    full = snapshot_topic(store, "radio_job", include_sensitive=True, redactor=redactor)
    assert "radio mobbing kierownik" in full.content and full.meta["sensitive_left_out"] == 0


def test_topic_block_goes_outdated_then_missing(lab) -> None:
    store, ids = lab
    rt = accepted_map(store, ids)
    context = store.create_context("t")
    block = store.add_context_block(context["context_id"], snapshot_topic(store, "health_gut", redactor=build_redactor(store)))
    assert block_status_detail(store, block)[0] == "current"
    gut = ids["jelita dieta pylera"]
    exchange_id = store.topic_strand_ranges([gut])[gut][0][0]
    store.mask_exchange_response(exchange_id)
    status, detail = block_status_detail(store, block)
    assert status == "outdated" and detail["new_chapters"] == 0
    rt.start_over(actor="owner")
    assert block_status_detail(store, block)[0] == "missing"


def test_old_context_block_table_is_rebuilt_once(tmp_path) -> None:
    path = tmp_path / "old.sqlite3"
    Store(path)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            DROP TABLE context_blocks;
            CREATE TABLE context_blocks (
                block_id INTEGER PRIMARY KEY AUTOINCREMENT, context_id TEXT NOT NULL, position INTEGER NOT NULL,
                source_type TEXT NOT NULL CHECK (source_type IN ('session', 'file')), source_key TEXT NOT NULL,
                source_session_id TEXT, source_file_id INTEGER, title TEXT NOT NULL, content TEXT NOT NULL,
                source_hash TEXT NOT NULL, found_ids TEXT NOT NULL DEFAULT '{}', token_count INTEGER NOT NULL DEFAULT 0,
                snapshot_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
                FOREIGN KEY(context_id) REFERENCES contexts(context_id) ON DELETE CASCADE);
            INSERT INTO contexts (context_id, name, color, is_locked, sort_order, created_at, updated_at)
                VALUES ('ctx_old', 'old', '#7a7df0', 0, 0, 1, 1);
            INSERT INTO context_blocks (context_id, position, source_type, source_key, title, content, source_hash,
                snapshot_at, created_at) VALUES ('ctx_old', 0, 'file', 'file:1', 'a.md', 'hello', 'h', 1, 1);
            """
        )
    store = Store(path)
    with sqlite3.connect(path) as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'context_blocks'").fetchone()[0]
    assert "'topic'" in sql
    block = store.get_context("ctx_old")["blocks"][0]
    assert block["title"] == "a.md" and block["meta"] == {}
    Store(path)
