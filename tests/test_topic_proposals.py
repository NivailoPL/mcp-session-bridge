"""Find topics, Refine topics, tests and decisions, with fake Codex and fake Jev."""
from __future__ import annotations

import asyncio

import pytest

from app.codex_app_server import CodexUnavailableError
from app.storage import Store
from app.topic_runtime import TopicRuntime
from app.topics import POOL_KEY, TopicError


class WordJev:
    """Picks the first topic whose description shares a word (5+ letters) with the chapter title."""

    async def classify(self, state, criteria):
        title = state["chapter_title"].lower()
        choice = POOL_KEY
        for key, description in criteria.items():
            if key == POOL_KEY:
                continue
            if any(len(word) >= 5 and word in title for word in description.lower().replace(",", " ").split()):
                choice = key
                break
        probabilities = {choice: 0.9, POOL_KEY if choice != POOL_KEY else next(iter(criteria)): 0.1}
        return {"choice": choice, "confidence": 0.9, "probabilities": probabilities, "cost_usd": 0.0001,
                "raw": {"answers": {"topic": {"choice": choice}}, "usage": {"cost": 0.0001}}}

    async def close(self):
        return None


class ScriptedCodex:
    def __init__(self, *results):
        self.results = list(results)
        self.prompts: list[str] = []

    async def extract_structured(self, message, *, model, effort, output_schema):
        self.prompts.append(message)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


SESSIONS = {
    "20261018-100000-radio-a": ["radio grafik dyżurów", "radio przerwy nocne"],
    "20261018-110000-radio-b": ["radio mobbing kierownik", "jelita kolonoskopia wyniki"],
    "20261018-120000-health-c": ["jelita dieta pylera", "jelita gastroskopia", "zakwas chleb żytni"],
    "20261018-130000-misc-d": ["zakwas dokarmianie", "zakwas temperatura"],
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


def runtime_with(store, codex) -> TopicRuntime:
    return TopicRuntime(store, key_reader=lambda: "sk", jev_factory=lambda key, model: WordJev(), codex=codex)


def run(coro):
    return asyncio.run(coro)


def first_map(ids):
    return {
        "reason": "Two recurring subjects.",
        "topics": [
            {"key": "radio_job", "name": "Radio", "family": "Praca", "description": "Praca w radiu: grafik, przerwy, mobbing",
             "chapter_ids": [ids["radio grafik dyżurów"], ids["radio przerwy nocne"], ids["radio mobbing kierownik"]]},
            {"key": "health_gut", "name": "Jelita", "family": "Zdrowie", "description": "Zdrowie: jelita, kolonoskopia, pylera",
             "chapter_ids": [ids["jelita kolonoskopia wyniki"], ids["jelita dieta pylera"], ids["jelita gastroskopia"]]},
            {"key": "bread", "name": "Chleb", "family": "Inne", "description": "Zakwas i pieczenie",
             "chapter_ids": [ids["zakwas chleb żytni"]]},
        ],
    }


def find_and_wait(rt, payload=None):
    async def go():
        await rt.start_find(payload or {}, actor="owner")
        await rt.wait()
    run(go())


def test_first_map_is_proposed_tested_and_accepted_without_sorting_again(lab) -> None:
    store, ids = lab
    codex = ScriptedCodex(first_map(ids))
    rt = runtime_with(store, codex)
    find_and_wait(rt, {"detail": "fine", "note": "keep health together"})

    assert "every chapter is unsorted" in codex.prompts[0] and "keep health together" in codex.prompts[0]
    [proposal] = store.list_topic_proposals()
    assert proposal["op"] == "first_map" and proposal["status"] == "pending"
    assert [t["key"] for t in proposal["payload"]["topics"]] == ["radio_job", "health_gut"]
    assert proposal["payload"]["dropped"][0]["key"] == "bread"
    test = proposal["test"]
    assert test["per_topic"]["radio_job"]["landed"] == 3 and test["verdict"] == "ok"
    assert test["pool_after"] == 3  # the three sourdough chapters stay unsorted
    assert store.active_topic_version() is None and store.list_topic_versions() == []

    result = rt.accept(proposal["proposal_id"], actor="owner")
    version = result["version"]
    assert version["is_active"] and not version["is_candidate"]
    counts = store.topic_overview_counts(version["version_id"], include_sensitive=False)
    assert counts["classified"] == 9 and counts["pool"] == 3
    assert [v["version_id"] for v in store.list_topic_versions()] == [version["version_id"]]


def test_find_with_a_map_proposes_new_topics_from_unsorted(lab) -> None:
    store, ids = lab
    bread = {"new_topics": [{"key": "bread", "name": "Chleb", "family": "Kuchnia",
                             "description": "Pieczenie: zakwas, dokarmianie, temperatura",
                             "chapter_ids": [ids["zakwas chleb żytni"], ids["zakwas dokarmianie"], ids["zakwas temperatura"]],
                             "reason": "Comes back in two sessions."}],
             "waiting": []}
    codex = ScriptedCodex(first_map(ids), bread)
    rt = runtime_with(store, codex)
    find_and_wait(rt)
    rt.accept(store.list_topic_proposals()[0]["proposal_id"], actor="owner")
    assert len(rt.pool_chapters()) == 3

    find_and_wait(rt)
    assert "EXISTING TOPICS" in codex.prompts[1] and "zakwas dokarmianie" in codex.prompts[1]
    proposal = store.list_topic_proposals(("pending",))[0]
    assert proposal["op"] == "new_topic" and proposal["test"]["landed"] == 3
    rt.accept(proposal["proposal_id"], actor="owner")
    assert rt.pool_chapters() == []


def test_refine_split_reject_is_remembered_and_others_go_outdated(lab) -> None:
    store, ids = lab
    split = {"operations": [{
        "op": "split", "target": "radio_job", "new_description": "", "reason": "Two threads.",
        "parts": [
            {"key": "radio_shifts", "name": "Grafik", "description": "Radio grafik, przerwy nocne",
             "chapter_ids": [ids["radio grafik dyżurów"], ids["radio przerwy nocne"]]},
            {"key": "radio_people", "name": "Ludzie", "description": "Radio mobbing kierownik",
             "chapter_ids": [ids["radio mobbing kierownik"]]},
        ]}, {"op": "redescribe", "target": "health_gut", "new_description": "Jelita: kolonoskopia, pylera, gastroskopia, dieta",
             "parts": [], "reason": "Name the tests."}]}
    codex = ScriptedCodex(first_map(ids), split, {"operations": []})
    rt = runtime_with(store, codex)
    find_and_wait(rt)
    rt.accept(store.list_topic_proposals()[0]["proposal_id"], actor="owner")

    async def refine():
        await rt.start_refine({}, actor="owner")
        await rt.wait()

    run(refine())
    # a part needs 4 chapters from 2 sessions, so the split is dropped; the redescribe stays
    [proposal] = store.list_topic_proposals(("pending",))
    assert proposal["op"] == "redescribe"
    assert proposal["test"]["changed_keys"] == ["health_gut"]
    rt.reject(proposal["proposal_id"], actor="owner", note="names are fine")
    run(refine())
    assert "Clearer description for Jelita (owner's note: names are fine)" in codex.prompts[-1]


def test_accepting_outdates_other_proposals_and_retest_reopens_them(lab) -> None:
    store, ids = lab
    bread = {"new_topics": [
        {"key": "bread", "name": "Chleb", "family": "Kuchnia", "description": "Zakwas, dokarmianie, temperatura",
         "chapter_ids": [ids["zakwas chleb żytni"], ids["zakwas dokarmianie"], ids["zakwas temperatura"]], "reason": "x"},
    ], "waiting": []}
    codex = ScriptedCodex(first_map(ids), bread)
    rt = runtime_with(store, codex)
    find_and_wait(rt)
    v1 = rt.accept(store.list_topic_proposals()[0]["proposal_id"], actor="owner")["version"]
    find_and_wait(rt)
    bread_proposal = store.list_topic_proposals(("pending",))[0]

    topics = [t for t in v1["topics"] if t["key"] != POOL_KEY]
    topics[0] = dict(topics[0], description="Praca w radiu: grafik dyżurów, przerwy nocne, mobbing")

    async def edit_then_retest():
        edit = await rt.start_test_edit(topics, note="clearer radio", actor="owner")
        await rt.wait()
        rt.accept(edit["proposal_id"], actor="owner")
        assert store.get_topic_proposal(bread_proposal["proposal_id"])["status"] == "outdated"
        with pytest.raises(TopicError):
            rt.accept(bread_proposal["proposal_id"], actor="owner")
        await rt.retest(bread_proposal["proposal_id"], actor="owner")
        await rt.wait()

    run(edit_then_retest())
    retested = store.get_topic_proposal(bread_proposal["proposal_id"])
    assert retested["status"] == "pending"
    rt.accept(retested["proposal_id"], actor="owner")
    assert rt.pool_chapters() == []


def test_hand_edit_is_tested_then_saved_and_old_version_restores(lab) -> None:
    store, ids = lab
    codex = ScriptedCodex(first_map(ids))
    rt = runtime_with(store, codex)
    find_and_wait(rt)
    v1 = rt.accept(store.list_topic_proposals()[0]["proposal_id"], actor="owner")["version"]
    topics = [t for t in v1["topics"] if t["key"] != POOL_KEY]
    topics[1] = dict(topics[1], description="Jelita i zakwas: kolonoskopia, pylera, gastroskopia, zakwas")

    async def edit():
        proposal = await rt.start_test_edit(topics, note="bread into health", actor="owner")
        await rt.wait()
        return proposal

    proposal = run(edit())
    tested = store.get_topic_proposal(proposal["proposal_id"])
    assert tested["status"] == "pending" and tested["test"]["moves_count"] == 3
    v2 = rt.accept(tested["proposal_id"], actor="owner")["version"]
    assert store.topic_overview_counts(v2["version_id"], False)["pool"] == 0
    rt.restore(v1["version_id"], actor="owner")
    assert store.topic_overview_counts(v1["version_id"], False)["pool"] == 3


def test_revise_first_map_drops_a_topic_and_tests_again(lab) -> None:
    store, ids = lab
    rt = runtime_with(store, ScriptedCodex(first_map(ids)))
    find_and_wait(rt)
    proposal = store.list_topic_proposals()[0]
    topics = [dict(t, keep=t["key"] != "health_gut") for t in proposal["payload"]["topics"]]

    async def revise():
        await rt.revise(proposal["proposal_id"], topics, actor="owner")
        await rt.wait()

    run(revise())
    revised = store.get_topic_proposal(proposal["proposal_id"])
    assert revised["summary"] == "First map: 1 topics" and revised["status"] == "pending"
    assert revised["test"]["pool_after"] == 6


def test_codex_failure_leaves_no_proposal(lab) -> None:
    store, _ = lab
    rt = runtime_with(store, ScriptedCodex(CodexUnavailableError("down")))
    find_and_wait(rt)
    assert store.list_topic_proposals() == []
    run_row = store.list_topic_runs()[0]
    assert run_row["kind"] == "find" and run_row["status"] == "failed"
    assert store.active_topic_run() is None
