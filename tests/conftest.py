"""Shared fixtures for the Bridge test-suite.

Every test that needs a running Bridge goes through :func:`load_main`, so the
environment a test runs against is described in one place instead of being
re-declared in each test module.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator

import pytest
from starlette.testclient import TestClient

from app import settings as app_settings
from app.security import password_hash

OWNER_USERNAME = "owner"
OWNER_PASSWORD = "secret-admin-password"
ADMIN_BASE_URL = "http://127.0.0.1:8787"


@pytest.fixture
def load_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., ModuleType]:
    """Import a freshly configured ``app.main`` bound to this test's tmp_path.

    ``app.main`` reads its settings at import time, so each test needs its own
    import. The module is dropped from ``sys.modules`` first to force that.
    """

    def _load(
        *,
        transcript_max_lines: int = 180,
        transcript_max_chars: int = 12000,
        graph_experimental: bool | None = False,
        owner_username: str = OWNER_USERNAME,
        owner_password: str = OWNER_PASSWORD,
        env: dict[str, str] | None = None,
    ) -> ModuleType:
        _clear_bridge_env(monkeypatch)
        monkeypatch.setenv("BRIDGE_PUBLIC_BASE_URL", "https://example.test")
        monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "bridge.sqlite3"))
        monkeypatch.setenv("BRIDGE_OWNER_USERNAME", owner_username)
        monkeypatch.setenv("BRIDGE_OWNER_PASSWORD_HASH", password_hash(owner_password))
        monkeypatch.setenv("BRIDGE_SECRET_KEY", "test-secret-for-isolated-fixtures-only")
        monkeypatch.setenv(
            "BRIDGE_TRANSCRIPT_CHUNK_MAX_LINES", str(transcript_max_lines)
        )
        monkeypatch.setenv(
            "BRIDGE_TRANSCRIPT_CHUNK_MAX_CHARS", str(transcript_max_chars)
        )
        # None leaves the variable unset so a test can assert the shipped default.
        if graph_experimental is not None:
            monkeypatch.setenv(
                "BRIDGE_GRAPH_EXPERIMENTAL", "true" if graph_experimental else "false"
            )
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)

        sys.modules.pop("app.main", None)
        return importlib.import_module("app.main")

    return _load


@pytest.fixture
def admin_client(
    load_main: Callable[..., ModuleType],
) -> Callable[..., tuple[TestClient, str]]:
    """Return a logged-in admin TestClient together with its CSRF token."""

    def _client(
        main: ModuleType | None = None,
        *,
        next_path: str = "/admin/sessions",
        **load_kwargs: Any,
    ) -> tuple[TestClient, str]:
        if main is None:
            main = load_main(**load_kwargs)
        client = TestClient(main.app, base_url=ADMIN_BASE_URL)
        response = client.post(
            "/admin/login",
            data={
                "username": OWNER_USERNAME,
                "password": OWNER_PASSWORD,
                "next": next_path,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        return client, client.get("/admin/api/me").json()["csrf_token"]

    return _client


@pytest.fixture
def short_tmp_path() -> Iterator[Path]:
    """A temporary directory short enough to hold a Unix socket path.

    ``tmp_path`` lives under ``/private/var/folders/...`` on macOS, which alone
    is longer than the 104-byte ``sun_path`` limit, so any test that binds an
    AF_UNIX socket has to build it somewhere shorter.
    """
    root = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
    path = Path(tempfile.mkdtemp(prefix="br-", dir=root))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _clear_bridge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make settings depend only on what this fixture sets.

    Two things would otherwise leak the developer's machine into the run: any
    BRIDGE_* variable exported in their shell, and the repository ``.env`` that
    ``load_settings`` reads through ``load_dotenv``. Clearing the variables is
    not enough on its own -- ``load_dotenv`` fills in names that are *absent*,
    so clearing them without also disabling it would widen the leak instead of
    closing it.
    """
    monkeypatch.setattr(app_settings, "load_dotenv", lambda *_args, **_kwargs: False)
    for name in [key for key in os.environ if key.startswith("BRIDGE_")]:
        monkeypatch.delenv(name, raising=False)
