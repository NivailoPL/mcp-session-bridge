"""In-memory limits for the unauthenticated OAuth and login endpoints.

Bridge runs as one process, so a per-process sliding window is enough. State is
lost on restart, which only ever loosens a limit for a moment.
"""

from __future__ import annotations

import asyncio
import hmac
import math
import time
from collections import deque
from collections.abc import Callable

from app.security import verify_password

LOGIN_FAILURES_PER_IP = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
LOGIN_FAILURES_GLOBAL = 30
LOGIN_GLOBAL_WINDOW_SECONDS = 60
# PBKDF2 runs in worker threads; two at a time keeps a burst from starving the server.
PASSWORD_CHECK_CONCURRENCY = 2
MAX_TRACKED_KEYS = 10_000


class SlidingWindowLimiter:
    """Allow ``limit`` hits per key within ``window_seconds``."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` may try again; 0 when it is allowed now."""
        hits = self._fresh(key)
        if len(hits) < self.limit:
            return 0
        return max(1, math.ceil(hits[0] + self.window_seconds - self.clock()))

    def hit(self, key: str) -> None:
        hits = self._fresh(key)
        hits.append(self.clock())
        self._hits[key] = hits
        if len(self._hits) > self.max_keys:
            self._prune()

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def _fresh(self, key: str) -> deque[float]:
        hits = self._hits.get(key) or deque()
        cutoff = self.clock() - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
        return hits

    def _prune(self) -> None:
        for key in list(self._hits):
            self._fresh(key)
        while len(self._hits) > self.max_keys:
            self._hits.pop(next(iter(self._hits)))


class LoginGuard:
    """Shared by the OAuth and admin login forms, which check the same password."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_ip = SlidingWindowLimiter(LOGIN_FAILURES_PER_IP, LOGIN_FAILURE_WINDOW_SECONDS, clock=clock)
        self.everyone = SlidingWindowLimiter(LOGIN_FAILURES_GLOBAL, LOGIN_GLOBAL_WINDOW_SECONDS, clock=clock)
        self._password_checks: asyncio.Semaphore | None = None

    def retry_after(self, ip: str) -> int:
        return max(self.per_ip.retry_after(ip), self.everyone.retry_after("*"))

    async def check(self, ip: str, username: str, password: str, owner_username: str, password_hash: str) -> bool:
        """Verify owner credentials off the event loop and record the outcome."""
        if self._password_checks is None:
            self._password_checks = asyncio.Semaphore(PASSWORD_CHECK_CONCURRENCY)
        async with self._password_checks:
            password_ok = await asyncio.to_thread(verify_password, password, password_hash)
        ok = hmac.compare_digest(username.encode("utf-8"), owner_username.encode("utf-8")) and password_ok
        if ok:
            self.per_ip.reset(ip)
        else:
            self.per_ip.hit(ip)
            self.everyone.hit("*")
        return ok


def client_ip(request) -> str:
    """The caller's address; uvicorn already resolved it from the trusted proxy."""
    client = getattr(request, "client", None)
    return client.host if client and client.host else "unknown"


def retry_message(seconds: int) -> str:
    minutes = math.ceil(seconds / 60)
    if minutes <= 1:
        return "Too many sign-in attempts. Try again in a minute."
    return f"Too many sign-in attempts. Try again in {minutes} minutes."
