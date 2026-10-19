"""Automatic Topics: sorting after a chapter fill, Find when Unsorted fills up, accepting clean tests."""
from __future__ import annotations

import asyncio

import pytest

from app.storage import Store
from app.topics import TopicError, validate_topic_settings
from tests.test_topic_proposals import ScriptedCodex, WordJev
from app.topic_runtime import TopicRuntime

SESSIONS = {
    "20261019-100000-radio-a": ["radio grafik dyżurów", "radio przerwy nocne"],
    "20261019-110000-radio-b": ["radio mobbing kierownik", "zakwas dokarmianie"],
    "20261019-120000-mix-c": ["zakwas temperatura", "rower przerzutka", "rower łańcuch"],
    "20261019-130000-mix-d": ["zakwas mąka żytnia", "rower opony"],
}


@pytest.fixture()
def lab(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")
    ids: dict[str, int] = {}
    for session, titles in SESSIONS.items():
        store.create_session(session, session.split("-")[-2])
        for title in titles:
            exchange = store.save_exchange(session, "Claude", title, "ok").exchange_id
            ids[title] = store.create_strand(session, title, [[exchange, exchange]])["strand_id"]
    return store, ids


def topic(key, name, description, chapter_ids):
    return {"key": key, "name": name, "family": "Inne", "description": description, "chapter_ids": chapter_ids,
            "reason": "Comes back."}


def radio_map(ids):
    return {"reason": "One subject.", "topics": [
        topic("radio_job", "Radio", "Praca w radiu: grafik, przerwy, mobbing",
              [ids["radio grafik dyżurów"], ids["radio przerwy nocne"], ids["radio mobbing kierownik"]])]}


def bread(ids):
    return topic("bread", "Chleb", "Pieczenie: zakwas, dokarmianie, temperatura, żytnia",
                 [ids["zakwas dokarmianie"], ids["zakwas temperatura"], ids["zakwas mąka żytnia"]])


def bike(ids):
    return topic("bike", "Rower", "Rower: przerzutka, łańcuch, opony",
                 [ids["rower przerzutka"], ids["rower łańcuch"], ids["rower opony"]])


def with_map(store, ids, *finds, sort=True, **settings):
    settings.setdefault("auto_enabled", True)
    """A runtime whose active map holds only the radio topic; the rest waits in Unsorted, never seen by Find."""
    codex = ScriptedCodex(*finds)
    rt = TopicRuntime(store, key_reader=lambda: "sk", jev_factory=lambda key, model: WordJev(), codex=codex)
    store.create_topic_version(
        [{k: v for k, v in t.items() if k not in ("chapter_ids", "reason")} for t in radio_map(ids)["topics"]],
        origin="manual", actor="owner",
    )
    if sort:
        async def go():
            await rt.start_classify({"mode": "unclassified"}, actor="owner")
            await rt.wait()
        asyncio.run(go())
    store.set_topic_settings(settings)
    return rt, codex


def settle(rt, coro):
    async def go():
        await coro
        await rt.wait()
        while rt._follow_task is not None and not rt._follow_task.done():
            await rt._follow_task
            await rt.wait()
    asyncio.run(go())


def journal(store):
    with store._connect() as conn:
        rows = conn.execute("SELECT actor, action, data_json FROM topic_journal ORDER BY event_id").fetchall()
    return [(row["actor"], row["action"], row["data_json"] or "") for row in rows]


def test_settings_default_to_a_reminder_and_validate() -> None:
    settings = validate_topic_settings({})
    assert settings["auto_enabled"] is False and settings["auto_find"] == "remind"
    assert validate_topic_settings({"auto_sort": True})["auto_enabled"] is True  # stored by the first beta
    with pytest.raises(TopicError):
        validate_topic_settings({"auto_find": "always"})
    with pytest.raises(TopicError):
        validate_topic_settings({"auto_enabled": "yes"})


def test_propose_and_accept_wait_while_automatic_topics_are_off(lab) -> None:
    store, ids = lab
    rt, codex = with_map(store, ids, auto_enabled=False, auto_find="accept", pool_hint=1)
    settle(rt, rt.maybe_find(cause="sorting"))
    assert codex.prompts == []


def test_full_unsorted_is_found_and_both_clean_topics_are_accepted_together(lab) -> None:
    store, ids = lab
    rt, codex = with_map(store, ids, {"new_topics": [bread(ids), bike(ids)], "waiting": []},
                         auto_find="accept", pool_hint=6)
    assert len(rt.pool_new()) == 6

    settle(rt, rt.maybe_find(cause="sorting"))

    keys = {t["key"] for t in store.active_topic_version()["topics"]}
    assert {"bread", "bike"} <= keys and rt.pool_chapters() == []
    statuses = sorted(p["status"] for p in store.list_topic_proposals())
    assert statuses.count("accepted") == 3  # the joined pair and both of its parts
    entries = journal(store)
    assert ("system", "find.start", '{"trigger": "auto", "cause": "sorting"}') in entries
    assert any(actor == "system" and action == "proposal.accept" and '"trigger": "auto"' in data
               for actor, action, data in entries)


def test_propose_mode_leaves_tested_proposals_for_the_owner(lab) -> None:
    store, ids = lab
    rt, _ = with_map(store, ids, {"new_topics": [bread(ids)], "waiting": []}, auto_find="propose", pool_hint=3)
    settle(rt, rt.maybe_find(cause="sorting"))
    [proposal] = store.list_topic_proposals(("pending",))
    assert proposal["test"]["verdict"] == "ok"
    assert "bread" not in {t["key"] for t in store.active_topic_version()["topics"]}


def test_below_the_threshold_or_in_remind_mode_nothing_starts(lab) -> None:
    store, ids = lab
    rt, codex = with_map(store, ids, auto_find="accept", pool_hint=7)
    settle(rt, rt.maybe_find(cause="sorting"))
    store.set_topic_settings({"auto_find": "remind", "pool_hint": 1})
    settle(rt, rt.maybe_find(cause="sorting"))
    assert codex.prompts == []


def test_chapters_the_last_find_left_waiting_do_not_start_it_again(lab) -> None:
    store, ids = lab
    rt, codex = with_map(store, ids, {"new_topics": [], "waiting": []}, auto_find="accept", pool_hint=3)
    settle(rt, rt.maybe_find(cause="sorting"))
    assert len(codex.prompts) == 1 and len(rt.pool_chapters()) == 6 and rt.pool_new() == []
    settle(rt, rt.maybe_find(cause="sorting"))
    assert len(codex.prompts) == 1


def test_after_a_chapter_fill_new_chapters_are_sorted_then_found(lab) -> None:
    store, ids = lab
    rt, codex = with_map(store, ids, {"new_topics": [bread(ids)], "waiting": []}, sort=False,
                         auto_enabled=True, auto_find="accept", pool_hint=3)

    settle(rt, rt.after_chapters())

    runs = store.list_topic_runs()
    assert [r["kind"] for r in runs[:3]] == ["test", "find", "classify"]
    assert "bread" in {t["key"] for t in store.active_topic_version()["topics"]}


def test_automation_off_or_without_a_map_does_nothing(lab) -> None:
    store, ids = lab
    rt = TopicRuntime(store, key_reader=lambda: "sk", jev_factory=lambda key, model: WordJev(),
                      codex=ScriptedCodex())
    store.set_topic_settings({"auto_enabled": True, "auto_find": "accept", "pool_hint": 1})
    settle(rt, rt.after_chapters())
    settle(rt, rt.maybe_find(cause="sorting"))
    assert store.list_topic_runs() == []
