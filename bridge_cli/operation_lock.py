from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


@contextmanager
def operation_lock(lock_path: Path, compatibility_path: Path | None = None) -> Iterator[None]:
    """Serialize Bridge mutations without locking interactive browsing."""
    paths = [lock_path]
    if compatibility_path is not None and compatibility_path != lock_path:
        paths.append(compatibility_path)
    paths.sort(key=str)
    handles: list[IO[str]] = []
    try:
        for path in paths:
            path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            handle = path.open("a+", encoding="utf-8")
            handles.append(handle)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another Bridge operation is running.") from exc
        yield
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
