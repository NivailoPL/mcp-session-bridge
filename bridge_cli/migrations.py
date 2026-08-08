from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app.storage import SCHEMA_VERSION, Store


CURRENT_SCHEMA_VERSION = SCHEMA_VERSION


def migrate_database(db_path: Path) -> dict[str, Any]:
    """Apply current Bridge migrations explicitly and verify SQLite integrity."""
    store = Store(db_path, allow_startup_migrations=True)
    with sqlite3.connect(db_path) as conn:
        result = conn.execute("PRAGMA quick_check").fetchone()
    if result is None or result[0] != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {result[0] if result else 'no result'}")
    return {
        "ok": True,
        "schema_version": store.schema_version(),
        "db_path": str(db_path),
    }
