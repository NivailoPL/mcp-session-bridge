from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.storage import Store
from bridge_cli.database import DatabaseManager
from bridge_cli.layout import Layout


class Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Runner:
    def __init__(self, *, active: bool = False, fail_start: bool = False):
        self.active = active
        self.fail_start = fail_start
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, check: bool = True):
        call = tuple(args)
        self.calls.append(call)
        if call[:2] == ("systemctl", "is-active"):
            result = Result(0, "active\n") if self.active else Result(3, "inactive\n")
        elif call[:2] == ("systemctl", "stop"):
            self.active = False
            result = Result()
        elif call[:2] == ("systemctl", "start"):
            if self.fail_start:
                result = Result(1, stderr="forced start failure")
            else:
                self.active = True
                result = Result()
        else:
            result = Result()
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result


def create_database(path: Path, *session_ids: str) -> None:
    store = Store(path)
    for session_id in session_ids:
        store.create_session(session_id, session_id, "manual-context")


def test_database_inspection_and_verified_backup(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    create_database(layout.db_path, "one", "two")
    manager = DatabaseManager(layout, Runner())

    info = manager.inspect()
    destination = tmp_path / "portable.sqlite3"
    result = manager.backup(destination)

    assert info.healthy is True
    assert info.sessions == 2
    assert info.schema_version == 1
    assert result["state"] == "complete"
    assert result["artifact"]["sha256"]
    assert DatabaseManager(layout, Runner()).inspect(destination).sessions == 2
    assert destination.stat().st_mode & 0o777 == 0o600

    with pytest.raises(ValueError, match="already exists"):
        manager.backup(destination)


def test_inspection_accepts_legacy_exchange_timestamp_shape(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    create_database(layout.db_path, "legacy")
    with sqlite3.connect(layout.db_path) as connection:
        connection.execute("ALTER TABLE exchanges RENAME TO exchanges_current")
        connection.execute(
            "CREATE TABLE exchanges (exchange_id INTEGER PRIMARY KEY, session_id TEXT, "
            "model_name TEXT, user_message TEXT, assistant_response TEXT, created_at INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO exchanges(session_id, model_name, user_message, assistant_response, created_at) "
            "VALUES ('legacy', 'model', 'hello', 'world', 123)"
        )
        connection.execute("DROP TABLE exchanges_current")

    info = DatabaseManager(layout, Runner()).inspect()

    assert info.healthy is True
    assert info.latest_conversation_at == 123


def test_import_replaces_complete_database_and_preserves_source(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    create_database(layout.db_path, "old")
    source = tmp_path / "incoming.sqlite3"
    create_database(source, "new-a", "new-b")
    before = source.read_bytes()
    manager = DatabaseManager(layout, Runner(active=True))

    result = manager.import_replace(source)

    assert result["state"] == "complete"
    assert result["before"]["sessions"] == 1
    assert result["after"]["sessions"] == 2
    assert source.read_bytes() == before
    with sqlite3.connect(layout.db_path) as connection:
        ids = {row[0] for row in connection.execute("SELECT session_id FROM sessions")}
    assert ids == {"new-a", "new-b"}
    assert Path(result["backup"]).exists()


def test_import_failure_restores_previous_database_and_service_state(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    create_database(layout.db_path, "keep")
    source = tmp_path / "incoming.sqlite3"
    create_database(source, "replace")
    runner = Runner(active=True, fail_start=True)

    with pytest.raises(RuntimeError, match="previous database.*restored"):
        DatabaseManager(layout, runner).import_replace(source)

    with sqlite3.connect(layout.db_path) as connection:
        ids = {row[0] for row in connection.execute("SELECT session_id FROM sessions")}
    assert ids == {"keep"}


def test_import_rejects_current_database_as_source(tmp_path: Path) -> None:
    layout = Layout.for_root(tmp_path / "root")
    create_database(layout.db_path, "same")

    with pytest.raises(ValueError, match="same file"):
        DatabaseManager(layout, Runner()).import_replace(layout.db_path)


def test_failed_fresh_import_restores_absent_database(tmp_path: Path, monkeypatch) -> None:
    layout = Layout.for_root(tmp_path / "root")
    source = tmp_path / "incoming.sqlite3"
    create_database(source, "incoming")
    manager = DatabaseManager(layout, Runner())
    monkeypatch.setattr(
        manager,
        "_prepare_runtime_database",
        lambda: (_ for _ in ()).throw(RuntimeError("forced runtime probe failure")),
    )

    with pytest.raises(RuntimeError, match="previous database state was restored"):
        manager.import_replace(source)

    assert not layout.db_path.exists()
    assert not Path(f"{layout.db_path}-wal").exists()
    assert not Path(f"{layout.db_path}-shm").exists()


def test_replacement_preparation_failure_leaves_current_database_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    layout = Layout.for_root(tmp_path / "root")
    create_database(layout.db_path, "keep")
    source = tmp_path / "incoming.sqlite3"
    create_database(source, "incoming")
    from bridge_cli import database as database_module

    original_backup = database_module.sqlite_backup

    def fail_replacement(source_path: Path, destination: Path) -> None:
        if destination.name == ".bridge.sqlite3.replacement":
            raise OSError("disk full")
        original_backup(source_path, destination)

    monkeypatch.setattr(database_module, "sqlite_backup", fail_replacement)

    with pytest.raises(OSError, match="disk full"):
        DatabaseManager(layout, Runner()).import_replace(source)

    with sqlite3.connect(layout.db_path) as connection:
        ids = {row[0] for row in connection.execute("SELECT session_id FROM sessions")}
    assert ids == {"keep"}
