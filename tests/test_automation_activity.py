"""Codex activity: the per-day report behind the header panel."""
from __future__ import annotations

import time

from app.automation_activity import ACTIVITY_DAYS, activity_report, live_status
from app.session_titles import title_tokens
from tests.test_chapter_automation import ChapterCodex, build, run_batch

MAP = [
    {"key": "radio_job", "name": "Radio job", "description": "Praca w radiu."},
    {"key": "health", "name": "Health", "description": "Zdrowie."},
]


def test_empty_window_still_lists_every_day_newest_first(tmp_path):
    store, _, _ = build(tmp_path, ChapterCodex())
    report = activity_report(store, timezone_name="Europe/Warsaw")
    assert len(report["days"]) == ACTIVITY_DAYS
    assert report["days"][0]["date"] == report["today"]
    dates = [day["date"] for day in report["days"]]
    assert dates == sorted(dates, reverse=True)
    assert all(day["estimated_tokens"] == 0 for day in report["days"])


def test_automatic_titles_are_logged_with_the_old_title(tmp_path):
    store, _, sessions = build(tmp_path, ChapterCodex())
    session_id = next(iter(sessions.values()))[0]
    with store._lock, store._connect() as conn:
        conn.execute("UPDATE sessions SET title_source = 'derived' WHERE session_id = ?", (session_id,))
    old = store.get_session(session_id).title
    assert store.set_auto_session_title(session_id, "Nowy tytuł", provider="codex", estimated_tokens=300)
    # A title the owner already set is never overwritten, so nothing more is logged.
    assert not store.set_auto_session_title(session_id, "Drugi tytuł")

    today = activity_report(store, timezone_name="UTC")["days"][0]["titles"]
    assert today["count"] == 1 and today["estimated_tokens"] == 300
    assert today["items"][0]["old_title"] == old and today["items"][0]["new_title"] == "Nowy tytuł"


def test_title_estimate_counts_the_message_as_sent():
    assert title_tokens("x" * 40) < title_tokens("x" * 4000) == title_tokens("x" * 9000)


def test_chapter_fill_shows_sessions_chapters_and_tokens(tmp_path):
    store, automation, _ = build(tmp_path, ChapterCodex())
    run_batch(automation)
    # A chapter run started by hand is not automation and stays out.
    manual = store.list_sessions()[-1]["session_id"]
    with store._lock, store._connect() as conn:
        conn.execute("UPDATE strand_runs SET created_by = 'owner' WHERE session_id = ?", (manual,))

    chapters = activity_report(store, timezone_name="UTC")["days"][0]["chapters"]
    assert chapters["sessions"] in (2, 3) and chapters["chapters"] == chapters["sessions"]
    assert chapters["estimated_tokens"] > 0
    assert chapters["items"][0]["chapters"] == ["Całość"]
    assert all(item["session_id"] != manual for item in chapters["items"])


def test_automatic_sorting_and_new_topics_are_grouped_per_topic(tmp_path):
    store, automation, _ = build(tmp_path, ChapterCodex())
    run_batch(automation)
    with store._lock, store._connect() as conn:
        conn.execute("UPDATE strands SET status = 'accepted'")
    version = store.create_topic_version(MAP, origin="import", actor="owner")
    chapters = store.topic_chapters()
    run = store.create_topic_run(
        kind="classify", version_id=version["version_id"], scope={"mode": "all"}, settings={},
        chapters=chapters, skipped=[], input_data={}, actor="system",
    )
    for chapter in chapters:
        store.record_topic_item(run["run_id"], chapter["strand_id"], request={}, response={},
                                topic_key="radio_job", confidence=0.9, probabilities={}, cost_usd=0.001)
    proposal = store.create_topic_proposal(
        run_id=None, base_version_id=version["version_id"], op="new_topic", source="find",
        summary="1 new topic", payload={"parts": [{"key": "release", "name": "Release workflow"}]},
    )
    store.add_topic_journal("system", "proposal.accept", "Accepted automatically",
                            data={"proposal_id": proposal["proposal_id"], "trigger": "auto"})

    topics = activity_report(store, timezone_name="UTC")["days"][0]["topics"]
    assert topics["chapters"] == len(chapters) and topics["new_topics"] == 1
    assert topics["cost_usd"] == round(0.001 * len(chapters), 6)
    assert topics["items"][0]["name"] == "Release workflow" and topics["items"][0]["is_new"]
    radio = next(item for item in topics["items"] if item["key"] == "radio_job")
    assert radio["name"] == "Radio job" and radio["added"] == len(chapters)


def test_old_activity_falls_out_of_the_window(tmp_path):
    store, _, sessions = build(tmp_path, ChapterCodex())
    session_id = next(iter(sessions.values()))[0]
    with store._lock, store._connect() as conn:
        conn.execute("UPDATE sessions SET title_source = 'derived' WHERE session_id = ?", (session_id,))
    store.set_auto_session_title(session_id, "Dawno temu", estimated_tokens=10)
    later = int(time.time()) + (ACTIVITY_DAYS + 2) * 86400
    report = activity_report(store, timezone_name="UTC", now=later)
    assert sum(day["titles"]["count"] for day in report["days"]) == 0


def test_live_status_reports_a_running_fill(tmp_path):
    store, automation, _ = build(tmp_path, ChapterCodex())

    class Topics:
        job = None

    assert live_status(automation, Topics())["chapters"]["status"] == "idle"
    automation._write("chapters.batch", {"status": "running", "processed": 1, "current_title": "x"})
    live = live_status(automation, Topics())["chapters"]
    assert live["status"] == "running" and live["processed"] == 1 and live["total"] > 1
