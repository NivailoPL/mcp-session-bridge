"""One-click connector approval for a browser that is signed in to Bridge.

A password sign-in, on the admin login or on the connector sign-in page, leaves
a signed cookie scoped to ``/oauth``. While it is valid the connector page asks
only Allow or Deny. It lasts as long as an admin session and is cleared on
sign-out.

The cookie is ``SameSite=Lax`` so it arrives on the top-level redirect a harness
starts from its own site. Allow is a POST, which Lax keeps same-site, and it must
also carry a token bound to this cookie and to the one pending sign-in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

CONSENT_COOKIE = "mcp_bridge_consent"
CONSENT_COOKIE_PATH = "/oauth"
# The same lifetime as an admin session (app.admin.ADMIN_SESSION_SECONDS).
CONSENT_SECONDS = 12 * 60 * 60


class ConsentSessions:
    def __init__(self, secret_key: str, *, clock: Callable[[], float] = time.time) -> None:
        # A key of its own, so this cookie can never be confused with another Bridge signature.
        self._key = hmac.new(secret_key.encode("utf-8"), b"bridge-consent-session", hashlib.sha256).digest()
        self.clock = clock

    def set_cookie(self, response: Response, request: Request) -> None:
        payload = {"nonce": secrets.token_urlsafe(18), "exp": int(self.clock()) + CONSENT_SECONDS}
        data = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
        response.set_cookie(
            CONSENT_COOKIE,
            f"{data.rstrip('=')}.{self._sign(data.rstrip('='))}",
            max_age=CONSENT_SECONDS,
            path=CONSENT_COOKIE_PATH,
            httponly=True,
            secure=request_is_secure(request),
            samesite="lax",
        )

    @staticmethod
    def clear_cookie(response: Response) -> None:
        response.delete_cookie(CONSENT_COOKIE, path=CONSENT_COOKIE_PATH)

    def read(self, request: Request) -> dict[str, Any] | None:
        cookie = request.cookies.get(CONSENT_COOKIE, "")
        data, _, signature = cookie.rpartition(".")
        if not data or not hmac.compare_digest(signature, self._sign(data)):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
            expires_at = int(payload["exp"])
            nonce = str(payload["nonce"])
        except (ValueError, KeyError, TypeError):
            return None
        if expires_at < self.clock() or not nonce:
            return None
        return {"nonce": nonce, "exp": expires_at}

    def approval_token(self, session: dict[str, Any], challenge: str) -> str:
        """Ties an Allow form to this browser's sign-in and to one pending authorization."""
        return self._sign(f"allow|{session['nonce']}|{challenge}")

    def approval_is_valid(self, request: Request, challenge: str, token: str) -> bool:
        session = self.read(request)
        return bool(session and token and hmac.compare_digest(token, self.approval_token(session, challenge)))

    def _sign(self, value: str) -> str:
        return hmac.new(self._key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def request_is_secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"
