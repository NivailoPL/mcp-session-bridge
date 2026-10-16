import pytest

from app.strands import (
    StrandError,
    build_strand_prompt,
    normalize_ranges,
    validate_strand_result,
    validate_strand_settings,
)
from app.storage import Store

SESSION = "20261015-200000-organic-synthetic-a1b2c3"
OTHER = "20261013-090000-caddy-limity-d4e5f6"


@pytest.fixture()
def store(tmp_path) -> Store:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session(SESSION, "Organic vs synthetic")
    store.create_session(OTHER, "Caddy")
    return store


def exchanges(store: Store, count: int, session: str = SESSION) -> list[int]:
    return [
        store.save_exchange(session, "Claude", f"Pytanie {index}", f"Odpowiedź {index}").exchange_id
        for index in range(count)
    ]


def spans(strand: dict) -> list[tuple[int, int]]:
    return [(item["start_exchange_id"], item["end_exchange_id"]) for item in strand["ranges"]]


def completed_run(store: Store, session: str, proposals: list[dict]) -> int:
    run = store.create_strand_run(
        session,
        source_exchange_id=1,
        exchange_count=1,
        settings={"model": "m", "effort": "low", "prompt": "p"},
        actor="owner",
    )
    store.start_strand_run(run["run_id"])
    store.complete_strand_run(run["run_id"], proposals)
    return run["run_id"]


def test_manual_strand_with_two_ranges_is_accepted(store):
    ids = exchanges(store, 6)
    strand = store.create_strand(SESSION, "  Highlighty   i źródła ", [[ids[0], ids[1]], [ids[4], ids[4]]])
    assert strand["title"] == "Highlighty i źródła"
    assert strand["origin"] == "manual" and strand["status"] == "accepted" and strand["run_id"] is None
    assert spans(strand) == [(ids[0], ids[1]), (ids[4], ids[4])]


def test_overlapping_and_touching_ranges_are_merged(store):
    ids = exchanges(store, 6)
    positions = store.strand_positions(SESSION)
    merged = normalize_ranges([[ids[3], ids[4]], [ids[0], ids[2]], [ids[1], ids[1]]], positions)
    assert merged == [(ids[0], ids[4])]


def test_invalid_ranges_are_rejected(store):
    ids = exchanges(store, 3)
    other = exchanges(store, 1, session=OTHER)
    positions = store.strand_positions(SESSION)
    with pytest.raises(StrandError, match="start before"):
        normalize_ranges([[ids[2], ids[0]]], positions)
    with pytest.raises(StrandError) as error:
        normalize_ranges([[other[0], other[0]]], positions)
    assert error.value.code == "range_unknown"
    with pytest.raises(StrandError):
        normalize_ranges([], positions)
    with pytest.raises(StrandError):
        store.create_strand(SESSION, "", [[ids[0], ids[0]]])


def test_accept_and_discard_only_touch_pending_rows_of_the_session(store):
    ids = exchanges(store, 4)
    other_ids = exchanges(store, 1, session=OTHER)
    completed_run(store, SESSION, [
        {"title": "A", "ranges": [(ids[0], ids[1])]},
        {"title": "B", "ranges": [(ids[2], ids[3])]},
    ])
    completed_run(store, OTHER, [{"title": "C", "ranges": [(other_ids[0], other_ids[0])]}])
    first = store.list_strands(SESSION)[0]
    assert store.accept_strands(SESSION, first["strand_id"]) == 1
    assert store.discard_pending_strands(SESSION) == 1
    assert [item["status"] for item in store.list_strands(SESSION)] == ["accepted"]
    assert store.has_pending_strands(OTHER) is True


def test_revert_removes_the_whole_run_and_keeps_manual_strands(store):
    ids = exchanges(store, 6)
    manual = store.create_strand(SESSION, "Ręczny", [[ids[5], ids[5]]])
    run_id = completed_run(store, SESSION, [
        {"title": f"S{index}", "ranges": [(ids[index], ids[index])]} for index in range(5)
    ])
    for strand in store.list_strands(SESSION)[:3]:
        store.accept_strands(SESSION, strand["strand_id"])
    assert store.revert_strand_run(run_id) == 5
    remaining = store.list_strands(SESSION)
    assert [item["strand_id"] for item in remaining] == [manual["strand_id"]]
    assert store.get_strand_run(run_id)["reverted_at"] is not None


def test_new_run_is_refused_while_proposals_are_pending_or_a_run_is_active(store):
    ids = exchanges(store, 2)
    completed_run(store, SESSION, [{"title": "A", "ranges": [(ids[0], ids[0])]}])
    with pytest.raises(StrandError) as error:
        completed_run(store, SESSION, [])
    assert error.value.code == "pending_exists"
    store.discard_pending_strands(SESSION)
    store.create_strand_run(SESSION, source_exchange_id=ids[1], exchange_count=2, settings={}, actor="owner")
    with pytest.raises(StrandError) as error:
        store.create_strand_run(SESSION, source_exchange_id=ids[1], exchange_count=2, settings={}, actor="owner")
    assert error.value.code == "run_active"


def test_stale_running_run_is_marked_interrupted(store):
    ids = exchanges(store, 1)
    stale = store.create_strand_run(
        SESSION, source_exchange_id=ids[0], exchange_count=1, settings={}, actor="owner", now=1_000
    )
    fresh = store.create_strand_run(
        SESSION, source_exchange_id=ids[0], exchange_count=1, settings={}, actor="owner", now=1_000 + 16 * 60
    )
    assert store.get_strand_run(stale["run_id"])["error_code"] == "interrupted"
    assert fresh["status"] == "queued"


def test_update_and_delete_strand(store):
    ids = exchanges(store, 3)
    strand = store.create_strand(SESSION, "Stary", [[ids[0], ids[0]]])
    updated = store.update_strand(strand["strand_id"], title="Nowy", ranges=[[ids[1], ids[2]]])
    assert updated["title"] == "Nowy"
    assert spans(updated) == [(ids[1], ids[2])]
    assert store.delete_strand(strand["strand_id"]) is True
    assert store.list_strands(SESSION) == []


def test_prompt_lists_every_exchange_without_title_and_hides_masked_text(store):
    ids = exchanges(store, 2)
    store.mask_exchange_response(ids[1])
    prompt = build_strand_prompt("Podziel.", store.list_exchanges(SESSION))
    assert f"Exchange {ids[0]}" in prompt and f"Exchange {ids[1]}" in prompt
    assert "Organic vs synthetic" not in prompt
    assert "Odpowiedź 1" not in prompt and "Odpowiedź 0" in prompt


def test_oversized_session_is_refused_not_truncated(store):
    store.save_exchange(SESSION, "Claude", "x" * 470_000, "y")
    with pytest.raises(StrandError) as error:
        build_strand_prompt("Podziel.", store.list_exchanges(SESSION))
    assert error.value.code == "source_too_large"


def test_result_with_unknown_exchange_fails_whole_result(store):
    ids = exchanges(store, 2)
    other = exchanges(store, 1, session=OTHER)
    positions = store.strand_positions(SESSION)
    good = {"title": "A", "ranges": [{"start": ids[0], "end": ids[1]}]}
    assert validate_strand_result({"strands": [good]}, positions)[0]["ranges"] == [(ids[0], ids[1])]
    bad = {"title": "B", "ranges": [{"start": other[0], "end": other[0]}]}
    with pytest.raises(StrandError):
        validate_strand_result({"strands": [good, bad]}, positions)


def test_settings_validation_and_defaults(store):
    assert store.get_strand_settings()["effort"] == "medium"
    saved = store.set_strand_settings({"model": "gpt-5.6-sol", "effort": "high", "prompt": " Podziel. "})
    assert saved["prompt"] == "Podziel."
    assert store.get_strand_settings() == saved
    for payload in ({"model": "bad model", "effort": "low", "prompt": "p"},
                    {"model": "m", "effort": "max", "prompt": "p"},
                    {"model": "m", "effort": "low", "prompt": ""},
                    {"model": "m", "effort": "low", "prompt": "p", "extra": 1}):
        with pytest.raises(StrandError):
            validate_strand_settings(payload)
