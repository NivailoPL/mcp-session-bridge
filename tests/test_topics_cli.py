import asyncio
import json
import sqlite3

import pytest

from app.storage import Store
from app.topic_runtime import TopicRuntime
from app.topics import TOPIC_OPENROUTER_KEY_SETTING
from bridge_cli.__main__ import build_parser
from bridge_cli.topics import run_command
from tests.test_topic_runtime import FakeJev

SESSION = "20261017-130000-radio-a1b2c3"


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    store = Store(path)
    store.create_session(SESSION, "Radio")
    for title in ("Radio grafik", "fail once"):
        exchange = store.save_exchange(SESSION, "Claude", title, "ok").exchange_id
        store.create_strand(SESSION, title, [[exchange, exchange]])
    store.set_app_setting(TOPIC_OPENROUTER_KEY_SETTING, "encrypted-sk-or-secret")
    store.create_topic_version({"radio_job": "Radio."}, origin="import", actor="owner", note="v6")
    runtime = TopicRuntime(store, key_reader=lambda: "sk", jev_factory=lambda key, model: FakeJev([]))

    async def classify():
        await runtime.start_classify(None, actor="owner")
        await runtime.wait()

    asyncio.run(classify())
    return path


def cli(db, capsys, *argv):
    args = build_parser().parse_args(["topics", *argv])
    assert run_command(args, db) == 0
    return capsys.readouterr().out


def test_status_journal_and_run(db, capsys) -> None:
    status = cli(db, capsys, "status")
    assert "Active map: v1" in status and "Radio job (radio_job)" in status
    assert "#1 classify paused 1/2" in status

    journal = cli(db, capsys, "journal")
    assert "classify.pause" in journal and "classify.start" in journal

    run = json.loads(cli(db, capsys, "run", "1", "--json"))
    assert run["run"]["failed_count"] == 1
    assert run["items"][0]["request"]["state"]["chapter_title"] == "Radio grafik"
    assert "failed: #2 fail once" in cli(db, capsys, "run", "1")


def test_export_and_versions_never_include_the_key(db, capsys, tmp_path) -> None:
    out = tmp_path / "lab.json"
    cli(db, capsys, "export", "--out", str(out))
    text = out.read_text()
    assert "sk-or-secret" not in text and json.loads(text)["versions"][0]["note"] == "v6"
    assert "* v1 import 1 topics" in cli(db, capsys, "versions")
    assert "- radio_job | Radio job" in cli(db, capsys, "version", "1")


def test_cli_is_read_only(db) -> None:
    before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM topic_journal").fetchone()[0]
    args = build_parser().parse_args(["topics", "status"])
    run_command(args, db)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM topic_journal").fetchone()[0] == before


def test_cli_explains_missing_tables(tmp_path) -> None:
    path = tmp_path / "old.sqlite3"
    sqlite3.connect(path).execute("CREATE TABLE app_settings (key TEXT)").connection.commit()
    with pytest.raises(RuntimeError, match="no Topics lab tables"):
        run_command(build_parser().parse_args(["topics", "status"]), path)
