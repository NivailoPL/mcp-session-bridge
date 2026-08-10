from __future__ import annotations

import sqlite3

import pytest

from app.graph_config import DEFAULT_GRAPH_PROMPT, GraphConfigError
from app.storage import SCHEMA_VERSION, Store


def test_graph_config_seeds_locked_default_profile(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")

    config = store.get_graph_config()

    assert SCHEMA_VERSION >= 2
    assert config["enabled"] is False
    assert config["locked"] is True
    assert config["draft"] is None
    assert config["active_profile"] == {
        "profile_id": 1,
        "version": 1,
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "effort": "medium",
        "prompt": DEFAULT_GRAPH_PROMPT,
        "max_concepts": 25,
        "inactivity_hours": 24,
        "include_sensitive": False,
        "schema_version": "concept-extraction/v1",
        "created_by": "system",
        "created_at": config["active_profile"]["created_at"],
    }


def test_unlock_edit_and_activate_never_mutates_active_profile(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    original = store.get_graph_config()["active_profile"]

    draft = store.unlock_graph_profile("owner")
    assert draft["base_profile_id"] == original["profile_id"]
    assert store.get_graph_config()["locked"] is False

    updated = store.update_graph_draft(
        {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "prompt": "Extract only durable project concepts and cite literal evidence.",
            "max_concepts": 40,
            "inactivity_hours": 3,
            "include_sensitive": True,
        },
        "owner",
    )
    assert updated["effort"] == "high"
    assert store.get_graph_config()["active_profile"] == original

    activated = store.activate_graph_draft("owner")
    config = store.get_graph_config()
    assert activated["version"] == 2
    assert activated["effort"] == "high"
    assert config["active_profile"] == activated
    assert config["draft"] is None
    assert config["locked"] is True

    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            "SELECT version, effort FROM graph_extractor_profiles ORDER BY version"
        ).fetchall()
    assert rows == [(1, "medium"), (2, "high")]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("provider", "api", "API provider is not available yet"),
        ("effort", "maximum", "effort"),
        ("prompt", "", "prompt"),
        ("max_concepts", 0, "max_concepts"),
        ("inactivity_hours", 2, "inactivity_hours"),
    ],
)
def test_invalid_graph_draft_is_rejected_without_partial_write(
    tmp_path, field, value, message
) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    before = store.unlock_graph_profile("owner")
    payload = {
        "provider": before["provider"],
        "model": before["model"],
        "effort": before["effort"],
        "prompt": before["prompt"],
        "max_concepts": before["max_concepts"],
        "inactivity_hours": before["inactivity_hours"],
        "include_sensitive": before["include_sensitive"],
    }
    payload[field] = value

    with pytest.raises(GraphConfigError, match=message):
        store.update_graph_draft(payload, "owner")

    assert store.get_graph_config()["draft"] == before


def test_graph_master_state_persists_independently_of_profile_lock(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = Store(db_path)
    store.set_graph_enabled(True)
    store.unlock_graph_profile("owner")

    reopened = Store(db_path)

    assert reopened.get_graph_config()["enabled"] is True
    assert reopened.get_graph_config()["locked"] is False

