from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def verify_writable_database(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        connection.execute("SELECT 1").fetchone()
    if journal_mode is None or str(journal_mode[0]).lower() != "wal":
        raise RuntimeError(f"SQLite WAL mode is unavailable: {journal_mode}")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        raise SystemExit("Usage: database_probe.py DATABASE_PATH")
    verify_writable_database(Path(arguments[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
