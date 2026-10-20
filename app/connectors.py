"""Who may connect over OAuth: redirect URI rules and the connection lock.

A dynamically registered client is known only by the redirect URI it declared,
because that is where the authorization code is delivered. Bridge therefore
judges a client by that URI:

* ``web``   -- https on an internet host. A phishing link can only steal a code
               this way, so the optional lock applies here.
* ``local`` -- loopback http(s) on the owner's own computer (Claude Code, Codex).
* ``app``   -- a custom scheme handled by an app on the owner's device.

The lock is off by default. When the owner turns it on, a ``web`` client that has
never been approved can sign in for the first time only while a connection window
is open, the way a reserved national ID number is released for a few minutes.
"""

from __future__ import annotations

import ipaddress
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from app.storage import ClientRecord, Store

LOCK_SETTING_KEY = "connectors.lock_enabled"
WINDOW_SETTING_KEY = "connectors.window_until"
WINDOW_MINUTES = (10, 30, 60)

MAX_REDIRECT_URIS = 10
MAX_REDIRECT_URI_CHARS = 2000
MAX_CLIENT_NAME_CHARS = 120

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
BLOCKED_SCHEMES = {
    "about", "blob", "data", "file", "filesystem", "ftp", "javascript", "mailto",
    "tel", "vbscript", "ws", "wss",
}
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class RedirectUriError(ValueError):
    pass


def redirect_kind(uri: str) -> str:
    """Classify a redirect URI as ``web``, ``local`` or ``app``; raise when it is unusable."""
    if not isinstance(uri, str) or not uri or len(uri) > MAX_REDIRECT_URI_CHARS:
        raise RedirectUriError("redirect_uri must be a non-empty string of reasonable length")
    if _CONTROL.search(uri) or " " in uri:
        raise RedirectUriError("redirect_uri must not contain spaces or control characters")
    try:
        parts = urlsplit(uri)
        host = (parts.hostname or "").lower()
    except ValueError as exc:
        raise RedirectUriError("redirect_uri is not a valid URI") from exc
    scheme = parts.scheme.lower()
    if not scheme or not _SCHEME.fullmatch(scheme):
        raise RedirectUriError("redirect_uri must be an absolute URI")
    if parts.fragment or "#" in uri:
        raise RedirectUriError("redirect_uri must not contain a fragment")
    if scheme in {"http", "https"}:
        if not host:
            raise RedirectUriError("redirect_uri must name a host")
        if _is_loopback(host):
            return "local"
        if scheme == "http":
            raise RedirectUriError("redirect_uri must use https unless it points to this computer")
        return "web"
    if scheme in BLOCKED_SCHEMES:
        raise RedirectUriError(f"redirect_uri scheme {scheme}: is not allowed")
    return "app"


def redirect_label(uri: str) -> str:
    """What the consent screen shows as the destination of the code."""
    parts = urlsplit(uri)
    kind = redirect_kind(uri)
    if kind == "app":
        return f"{parts.scheme.lower()}://"
    host = (parts.hostname or "").lower()
    display = f"[{host}]" if ":" in host else host
    port = parts.port
    if port and not (kind == "web" and port == 443):
        display = f"{display}:{port}"
    return display


def client_kind(client: ClientRecord) -> str:
    """The riskiest kind among a client's redirect URIs."""
    kinds = {redirect_kind(uri) for uri in client.redirect_uris}
    for kind in ("web", "app", "local"):
        if kind in kinds:
            return kind
    return "local"


def clean_client_name(value: Any) -> str | None:
    """A display name the client chose for itself; never trusted, only shown."""
    if not isinstance(value, str):
        return None
    name = " ".join(_CONTROL.sub(" ", value).split())
    if not name:
        return None
    if len(name) > MAX_CLIENT_NAME_CHARS:
        name = name[: MAX_CLIENT_NAME_CHARS - 1].rstrip() + "…"
    return name


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ConnectionLock:
    def __init__(self, store: Store, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    def enabled(self) -> bool:
        return self.store.get_app_setting(LOCK_SETTING_KEY) == "true"

    def set_enabled(self, enabled: bool) -> None:
        self.store.set_app_setting(LOCK_SETTING_KEY, "true" if enabled else "false")
        if not enabled:
            self.close_window()

    def window_until(self) -> int | None:
        raw = self.store.get_app_setting(WINDOW_SETTING_KEY)
        try:
            until = int(raw) if raw else 0
        except ValueError:
            return None
        return until if until > self.clock() else None

    def open_window(self, minutes: int) -> int:
        if minutes not in WINDOW_MINUTES:
            raise ValueError(f"minutes must be one of {', '.join(map(str, WINDOW_MINUTES))}")
        until = int(self.clock()) + minutes * 60
        self.store.set_app_setting(WINDOW_SETTING_KEY, str(until))
        return until

    def close_window(self) -> None:
        self.store.delete_app_setting(WINDOW_SETTING_KEY)

    def blocks(self, client: ClientRecord, redirect_uri: str) -> bool:
        """True when this sign-in needs an open window and none is open."""
        if not self.enabled() or client.approved_at is not None:
            return False
        if redirect_kind(redirect_uri) != "web":
            return False
        return self.window_until() is None

    def state(self) -> dict[str, Any]:
        until = self.window_until() if self.enabled() else None
        return {
            "lock_enabled": self.enabled(),
            "window_open": until is not None,
            "window_until": until,
            "window_remaining_seconds": max(0, until - int(self.clock())) if until else 0,
            "window_minutes": list(WINDOW_MINUTES),
        }
