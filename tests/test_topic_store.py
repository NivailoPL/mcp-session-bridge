import pytest

from app.storage import Store
from app.topics import POOL_KEY, TopicError, jev_criteria, validate_scope, validate_topic_map

SESSION = "20261017-100000-radio-and-health-a1b2c3"
PRIVATE = "20261017-110000-gut-d4e5f6"

MAP = [
    {"key": "radio_job", "name": "Radio", "description": "Praca w Radiu Kraków.", "family": "Praca", "color": "#f97316"},
    {"key": "health_gut", "name": "Jelita", "description": "Zdrowie: jelita.", "family": "Zdrowie", "color": "#34d399"},
]


@pytest.fixture()
def store(tmp_path) -> Store:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session(SESSION, "Radio i zdrowie")
    store.create_session(PRIVATE, "Jelita")
    group = store.create_session_group("Health", "#34d399", "folder")
    store.update_session_group(group.group_id, is_sensitive=True)
    store.set_session_group(PRIVATE, group.group_id)
    return store


def chapter(store: Store, session: str, title: str) -> int:
    exchange = store.save_exchange(session, "Claude", f"Pytanie: {title}", "Odpowiedź").exchange_id
    return store.create_strand(session, title, [[exchange, exchange]])["strand_id"]


def test_map_accepts_experiment_shape_and_keeps_pool_last() -> None:
    topics = validate_topic_map({"radio_job": "Radio.", "unsorted": "ignored", "car": "Samochód."})
    assert [topic["key"] for topic in topics] == ["radio_job", "car", POOL_KEY]
    assert topics[0]["name"] == "Radio job"
    assert list(jev_criteria(topics))[-1] == POOL_KEY


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [{"key": "Bad Key", "description": "x"}],
        [{"key": "radio", "description": ""}],
        [{"key": "radio", "description": "x"}, {"key": "radio", "description": "y"}],
        [{"key": "radio", "description": "x", "color": "orange"}],
    ],
)
def test_map_rejects_invalid_topics(payload) -> None:
    with pytest.raises(TopicError):
        validate_topic_map(payload)


def test_scope_validation() -> None:
    assert validate_scope(None, {"radio_job"}) == {"mode": "unclassified"}
    assert validate_scope({"mode": "topic", "topic_key": "radio_job"}, {"radio_job"})["topic_key"] == "radio_job"
    with pytest.raises(TopicError):
        validate_scope({"mode": "topic", "topic_key": "car"}, {"radio_job"})


def test_versions_are_immutable_and_one_is_active(store: Store) -> None:
    first = store.create_topic_version(MAP, origin="import", actor="owner", note="v6")
    edited = [dict(MAP[0], description="Radio: warunki pracy."), MAP[1]]
    second = store.create_topic_version(edited, origin="manual", actor="owner", parent_id=first["version_id"])
    assert store.active_topic_version()["version_id"] == second["version_id"]
    assert store.get_topic_version(first["version_id"])["topics"][0]["description"] == "Praca w Radiu Kraków."
    assert [v["is_active"] for v in store.list_topic_versions()] == [True, False]
    store.activate_topic_version(first["version_id"])
    assert store.active_topic_version()["version_id"] == first["version_id"]
    with pytest.raises(TopicError):
        store.create_topic_version(MAP, origin="manual", actor="owner", parent_id=999)


def test_chapters_carry_session_group_and_sensitivity(store: Store) -> None:
    chapter(store, SESSION, "Grafik dyżurów")
    chapter(store, PRIVATE, "Kolonoskopia")
    chapters = {c["title"]: c for c in store.topic_chapters()}
    assert chapters["Grafik dyżurów"]["sensitive"] is False
    assert chapters["Grafik dyżurów"]["group_name"] == "Uncategorized"
    assert chapters["Kolonoskopia"]["sensitive"] is True
    assert chapters["Kolonoskopia"]["group_name"] == "Health"


def test_run_items_become_current_assignments_and_follow_chapters(store: Store) -> None:
    version = store.create_topic_version(MAP, origin="import", actor="owner")
    a = chapter(store, SESSION, "Grafik dyżurów")
    b = chapter(store, SESSION, "Nowa sesja")
    chapters = store.topic_chapters()
    run = store.create_topic_run(
        kind="classify", version_id=version["version_id"], scope={"mode": "all"}, settings={},
        chapters=chapters, skipped=[], input_data={}, actor="owner",
    )
    with pytest.raises(TopicError):
        store.create_topic_run(
            kind="classify", version_id=version["version_id"], scope={"mode": "all"}, settings={},
            chapters=chapters, skipped=[], input_data={}, actor="owner",
        )
    store.record_topic_item(run["run_id"], a, request={"state": 1}, response={"answers": {}}, topic_key="radio_job",
                            confidence=0.9, probabilities={"radio_job": 0.9}, cost_usd=0.0001)
    store.record_topic_item(run["run_id"], b, request={"state": 2}, response=None, error="HTTP 500")
    finished = store.get_topic_run(run["run_id"])
    assert (finished["done_count"], finished["failed_count"], finished["cost_usd"]) == (1, 1, 0.0001)
    current = store.topic_current_assignments(version["version_id"])
    assert set(current) == {a} and current[a]["topic_key"] == "radio_job"
    assert store.reset_failed_topic_items(run["run_id"]) == 1
    assert store.pending_topic_items(run["run_id"]) == [b]

    counts = store.topic_overview_counts(version["version_id"], include_sensitive=False)
    assert counts["classified"] == 1 and counts["unclassified"] == 1
    store.update_strand(a, title="Grafik i przerwy")
    assert store.topic_overview_counts(version["version_id"], include_sensitive=False)["changed"] == 1
    store.delete_strand(a)
    assert store.topic_current_assignments(version["version_id"]) == {}


def test_recover_pauses_interrupted_runs(store: Store) -> None:
    version = store.create_topic_version(MAP, origin="import", actor="owner")
    chapter(store, SESSION, "Grafik dyżurów")
    run = store.create_topic_run(
        kind="classify", version_id=version["version_id"], scope={"mode": "all"}, settings={},
        chapters=store.topic_chapters(), skipped=[], input_data={}, actor="owner",
    )
    store.set_topic_run_status(run["run_id"], "running")
    assert store.recover_topic_runs() == [run["run_id"]]
    recovered = store.get_topic_run(run["run_id"])
    assert recovered["status"] == "paused" and recovered["error_code"] == "interrupted"


def test_journal_and_settings(store: Store) -> None:
    store.add_topic_journal("owner", "map.import", "Imported v6.", version_id=1, data={"topics": 23})
    entry = store.list_topic_journal()[0]
    assert entry["actor"] == "owner" and entry["data"] == {"topics": 23}
    with pytest.raises(ValueError):
        store.add_topic_journal("someone", "x", "y")
    assert store.set_topic_settings({"include_sensitive": True})["include_sensitive"] is True
    assert store.get_topic_settings()["jev_model"] == "typesafe/jev-1.14"
    with pytest.raises(TopicError):
        store.set_topic_settings({"weak_threshold": 2})
