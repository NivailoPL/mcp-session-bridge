import asyncio

import pytest

from app.jev_client import JevError
from app.storage import Store
from app.strands import TOKEN_FEATURES_MASTER_KEY
from app.topic_runtime import TopicRuntime
from app.topics import POOL_KEY, TopicError

SESSION = "20261017-100000-radio-a1b2c3"
PRIVATE = "20261017-110000-gut-d4e5f6"
MAP = [
    {"key": "radio_job", "description": "Praca w Radiu Kraków."},
    {"key": "health_gut", "description": "Zdrowie: jelita."},
]


class FakeJev:
    """Answers by title: 'radio' -> radio_job, 'fail' -> error, 'auth' -> bad key, else the pool."""

    def __init__(self, log: list):
        self.log = log
        self.closed = False

    async def classify(self, state, criteria):
        self.log.append(state)
        title = state["chapter_title"].lower()
        if "auth" in title:
            raise JevError("OpenRouter refused the API key.", code="jev_auth")
        if "fail" in title:
            raise JevError("OpenRouter kept failing (HTTP 503).", code="jev_unavailable")
        choice = "radio_job" if "radio" in title else POOL_KEY
        raw = {"answers": {"topic": {"choice": choice, "confidence": 0.8}}, "usage": {"cost": 0.0001}}
        return {"choice": choice, "confidence": 0.8, "probabilities": {choice: 0.8}, "cost_usd": 0.0001, "raw": raw}

    async def close(self):
        self.closed = True


@pytest.fixture()
def setup(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session(SESSION, "Radio")
    store.create_session(PRIVATE, "Jelita")
    group = store.create_session_group("Health", "#34d399", "folder")
    store.update_session_group(group.group_id, is_sensitive=True)
    store.set_session_group(PRIVATE, group.group_id)
    store.create_topic_version(MAP, origin="import", actor="owner")
    log: list = []
    key = {"value": "sk-test"}
    runtime = TopicRuntime(store, key_reader=lambda: key["value"], jev_factory=lambda k, model: FakeJev(log))
    return store, runtime, log, key


def chapters(store: Store, session: str, *titles: str) -> list[int]:
    ids = []
    for title in titles:
        exchange = store.save_exchange(session, "Claude", title, "ok").exchange_id
        ids.append(store.create_strand(session, title, [[exchange, exchange]])["strand_id"])
    return ids


async def classify(runtime: TopicRuntime, scope=None):
    run = await runtime.start_classify(scope, actor="owner")
    await runtime.wait()
    return runtime.store.get_topic_run(run["run_id"])


def test_estimate_and_classify_skip_sensitive_chapters(setup) -> None:
    store, runtime, log, _ = setup
    chapters(store, SESSION, "Radio grafik", "Nowa sesja", "Radio przerwy")
    chapters(store, PRIVATE, "Kolonoskopia")
    estimate = runtime.estimate(None)
    assert (estimate["send_count"], estimate["skipped_sensitive"]) == (3, 1)
    assert estimate["estimated_cost_usd"] == pytest.approx(0.0002, abs=1e-4)

    run = asyncio.run(classify(runtime))
    assert run["status"] == "completed"
    assert (run["done_count"], run["skipped_count"]) == (3, 1)
    assert run["metrics"]["per_topic"] == {"radio_job": 2, POOL_KEY: 1}
    assert all(state["chapter_title"] != "Kolonoskopia" for state in log)
    assert set(log[0]) == {"chapter_title", "session_title", "archive_group"}
    journal = [entry["action"] for entry in store.list_topic_journal()]
    assert journal[:2] == ["classify.finish", "classify.start"]

    items = store.topic_run_items(run["run_id"])
    assert items[0]["request"]["state"]["session_title"] == "Radio"
    assert items[0]["response"]["usage"]["cost"] == 0.0001
    # Measured cost replaces the default estimate.
    assert runtime.estimate({"mode": "all"})["measured"] is True


def test_unclassified_scope_picks_new_and_renamed_chapters(setup) -> None:
    store, runtime, log, _ = setup
    first, _second = chapters(store, SESSION, "Radio grafik", "Nowa sesja")
    asyncio.run(classify(runtime))
    with pytest.raises(TopicError) as nothing:
        asyncio.run(runtime.start_classify(None, actor="owner"))
    assert nothing.value.code == "nothing_to_do"
    chapters(store, SESSION, "Radio projekt")
    store.update_strand(first, title="Radio grafik i przerwy")
    assert runtime.estimate(None)["send_count"] == 2
    assert runtime.estimate({"mode": "topic", "topic_key": POOL_KEY})["send_count"] == 1


def test_sensitive_chapters_are_sent_when_settings_allow(setup) -> None:
    store, runtime, log, _ = setup
    chapters(store, PRIVATE, "Kolonoskopia")
    store.set_topic_settings({"include_sensitive": True})
    run = asyncio.run(classify(runtime))
    assert run["done_count"] == 1 and log[0]["archive_group"] == "Health"


def test_repeated_provider_errors_pause_and_resume_retries(setup) -> None:
    store, runtime, log, _ = setup
    chapters(store, SESSION, "Radio a", "fail 1", "fail 2", "fail 3", "Radio b")
    run = asyncio.run(classify(runtime))
    assert run["status"] == "paused" and run["error_code"] == "provider_errors"
    assert run["failed_count"] == 3

    async def resume():
        await runtime.resume(run["run_id"], actor="owner")
        await runtime.wait()

    asyncio.run(resume())
    again = store.get_topic_run(run["run_id"])
    assert again["status"] == "paused" and again["done_count"] == 2
    closed = runtime.close_run(run["run_id"])
    assert closed["status"] == "completed"
    assert store.topic_overview_counts(run["version_id"], include_sensitive=False)["classified"] == 2


def test_bad_key_pauses_at_once(setup) -> None:
    store, runtime, _, _ = setup
    chapters(store, SESSION, "auth problem", "Radio a")
    run = asyncio.run(classify(runtime))
    assert run["status"] == "paused" and run["error_code"] == "jev_auth"


def test_refusals_before_any_call(setup) -> None:
    store, runtime, log, key = setup
    chapters(store, SESSION, "Radio a")
    key["value"] = ""
    with pytest.raises(TopicError) as missing:
        asyncio.run(runtime.start_classify(None, actor="owner"))
    assert missing.value.code == "key_missing"
    key["value"] = "sk-test"
    store.set_app_setting(TOKEN_FEATURES_MASTER_KEY, "false")
    with pytest.raises(TopicError) as off:
        asyncio.run(runtime.start_classify(None, actor="owner"))
    assert off.value.code == "feature_off"
    assert log == []


def test_resume_refuses_after_map_change(setup) -> None:
    store, runtime, _, _ = setup
    chapters(store, SESSION, "fail 1", "fail 2", "fail 3")
    run = asyncio.run(classify(runtime))
    store.create_topic_version(MAP + [{"key": "car", "description": "Samochód."}], origin="manual", actor="owner")
    with pytest.raises(TopicError) as changed:
        asyncio.run(runtime.resume(run["run_id"], actor="owner"))
    assert changed.value.code == "map_changed"
