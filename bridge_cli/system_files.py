from __future__ import annotations

import sqlite3
from pathlib import Path


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_conn:
        with sqlite3.connect(destination) as destination_conn:
            source_conn.backup(destination_conn)
    destination.chmod(0o600)


def remove_sqlite_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def switch_release(current_link: Path, release: Path, *, temporary_name: str) -> None:
    temporary = current_link.with_name(temporary_name)
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(release)
    temporary.replace(current_link)
