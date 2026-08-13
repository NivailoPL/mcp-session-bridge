from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.storage import Store
from bridge_cli.config import update_env_file
from bridge_cli.database_probe import verify_writable_database
from bridge_cli.migrations import CURRENT_SCHEMA_VERSION, migrate_database
from bridge_cli.__main__ import build_parser
from bridge_cli.status import CheckResult, StatusReport, UpdateStatus


def test_status_report_has_stable_machine_readable_contract() -> None:
    report = StatusReport(
        overall="attention",
        version={"current": "0.4.0"},
        update=UpdateStatus(state="available", current="0.4.0", latest="0.4.1"),
        checks=[CheckResult("service", "Bridge service", "pass", "active")],
        installation={"mode": "managed"},
        last_operation=None,
    )

    payload = report.to_dict()

    assert payload["format_version"] == 1
    assert "schema_version" not in payload
    assert payload["overall"] == "attention"
    assert payload["update"]["state"] == "available"
    assert payload["checks"] == [
        {
            "id": "service",
            "label": "Bridge service",
            "state": "pass",
            "message": "active",
        }
    ]
    assert json.loads(report.to_json()) == payload


def test_status_contract_rejects_unknown_states() -> None:
    with pytest.raises(ValueError, match="check state"):
        CheckResult("service", "Service", "maybe", "unknown")
    with pytest.raises(ValueError, match="update state"):
        UpdateStatus(state="newer", current="0.4.0")


def test_update_env_file_preserves_comments_and_unknown_keys(tmp_path: Path) -> None:
    env_path = tmp_path / "bridge.env"
    env_path.write_text(
        "# operator note\nCUSTOM_SETTING=keep-me\nBRIDGE_PUBLIC_BASE_URL=https://old.test\n",
        encoding="utf-8",
    )

    update_env_file(
        env_path,
        {
            "BRIDGE_PUBLIC_BASE_URL": "https://bridge.test",
            "BRIDGE_OWNER_USERNAME": "owner",
        },
    )

    text = env_path.read_text(encoding="utf-8")
    assert "# operator note" in text
    assert "CUSTOM_SETTING=keep-me" in text
    assert "BRIDGE_PUBLIC_BASE_URL=https://bridge.test" in text
    assert "BRIDGE_OWNER_USERNAME=owner" in text
    assert text.count("BRIDGE_PUBLIC_BASE_URL=") == 1
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_managed_store_requires_explicit_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge.sqlite3"

    with pytest.raises(RuntimeError, match="mcp-bridge migrate"):
        Store(db_path, allow_startup_migrations=False)

    result = migrate_database(db_path)
    store = Store(db_path, allow_startup_migrations=False)

    assert result["schema_version"] == CURRENT_SCHEMA_VERSION
    assert store.schema_version() == CURRENT_SCHEMA_VERSION


def test_managed_store_accepts_existing_schema_two_after_feature_removal(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    Store(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(1, 1), (2, 2)],
        )

    managed = Store(db_path, allow_startup_migrations=False)

    assert managed.schema_version() == 2


def test_migration_is_idempotent_and_preserves_data(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    legacy = Store(db_path)
    legacy.create_session("keep-me", "Existing session", "manual-context")

    first = migrate_database(db_path)
    second = migrate_database(db_path)
    managed = Store(db_path, allow_startup_migrations=False)

    assert first["schema_version"] == second["schema_version"] == CURRENT_SCHEMA_VERSION
    assert managed.get_session("keep-me") is not None


def test_database_probe_enables_wal_and_verifies_write_access(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    migrate_database(db_path)

    verify_writable_database(db_path)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_database_command_contract_parses_explicit_replace_and_yes() -> None:
    args = build_parser().parse_args(
        ["database", "import", "/tmp/portable.sqlite3", "--replace", "--yes", "--json"]
    )

    assert args.command == "database"
    assert args.database_action == "import"
    assert args.replace is True
    assert args.yes is True
    assert args.as_json is True


def test_deploy_command_contract_supports_confirmation_and_json() -> None:
    args = build_parser().parse_args(
        ["deploy", "--yes", "--allow-downgrade", "--json"]
    )

    assert args.command == "deploy"
    assert args.yes is True
    assert args.allow_downgrade is True
    assert args.as_json is True
