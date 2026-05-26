from __future__ import annotations

import html
import time
from base64 import b64decode
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.security import hash_secret, pkce_s256, token_urlsafe, verify_password
from app.settings import Settings
from app.storage import ChallengeRecord, ClientRecord, CodeRecord, Store, TokenRecord

ALLOWED_AUTH_METHODS = {"none", "client_secret_post", "client_secret_basic"}
DEFAULT_GRANT_TYPES = ["authorization_code", "refresh_token"]
DEFAULT_RESPONSE_TYPES = ["code"]
OPTIONAL_SCOPES = {"offline_access"}


class OAuthHandlers:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    async def metadata(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return self._cors(Response(status_code=204))
        return self._cors(
            JSONResponse(
                {
                    "issuer": self.settings.issuer_url,
                    "authorization_endpoint": self.settings.authz_endpoint,
                    "token_endpoint": self.settings.token_endpoint,
                    "registration_endpoint": self.settings.registration_endpoint,
                    "response_types_supported": DEFAULT_RESPONSE_TYPES,
                    "grant_types_supported": DEFAULT_GRANT_TYPES,
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": sorted(ALLOWED_AUTH_METHODS),
                    "scopes_supported": [self.settings.scope, *sorted(OPTIONAL_SCOPES)],
                },
                headers=self._no_store_headers(),
            )
        )

    async def protected_resource_metadata(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return self._cors(Response(status_code=204))
        return self._cors(
            JSONResponse(
                {
                    "resource": self.settings.resource_url,
                    "authorization_servers": [self.settings.issuer_url],
                    "scopes_supported": [self.settings.scope],
                    "bearer_methods_supported": ["header"],
                    "resource_name": "MCP Session Bridge",
                },
                headers=self._no_store_headers(),
            )
        )

    async def register(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return self._cors(Response(status_code=204))

        try:
            payload = await request.json()
        except Exception:
            return self._oauth_error("invalid_request", "registration body must be JSON", status_code=400)

        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris or not all(isinstance(v, str) for v in redirect_uris):
            return self._oauth_error("invalid_redirect_uri", "redirect_uris must be a non-empty string list", 400)

        auth_method = payload.get("token_endpoint_auth_method") or "none"
        if auth_method not in ALLOWED_AUTH_METHODS:
            return self._oauth_error("invalid_client_metadata", "unsupported token_endpoint_auth_method", 400)

        grant_types = payload.get("grant_types") or DEFAULT_GRANT_TYPES
        response_types = payload.get("response_types") or DEFAULT_RESPONSE_TYPES
        if "authorization_code" not in grant_types or "code" not in response_types:
            return self._oauth_error("invalid_client_metadata", "authorization_code/code flow is required", 400)

        scopes = self._normalize_scopes(payload.get("scope"))
        if scopes is None:
            return self._oauth_error("invalid_scope", "unsupported scope", 400)

        client_id = f"mcp_{token_urlsafe(24)}"
        client_secret = None
        client_secret_hash = None
        secret_expires_at = None
        if auth_method != "none":
            client_secret = token_urlsafe(32)
            client_secret_hash = hash_secret(client_secret, self.settings.secret_key)
            secret_expires_at = 0

        record = ClientRecord(
            client_id=client_id,
            client_secret_hash=client_secret_hash,
            auth_method=auth_method,
            redirect_uris=redirect_uris,
            grant_types=list(grant_types),
            response_types=list(response_types),
            scope=" ".join(scopes),
            client_name=payload.get("client_name"),
            secret_expires_at=secret_expires_at,
        )
        self.store.register_client(record)

        response: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": redirect_uris,
            "grant_types": list(grant_types),
            "response_types": list(response_types),
            "scope": " ".join(scopes),
            "token_endpoint_auth_method": auth_method,
        }
        if payload.get("client_name"):
            response["client_name"] = payload["client_name"]
        if client_secret:
            response["client_secret"] = client_secret
            response["client_secret_expires_at"] = secret_expires_at

        return self._cors(JSONResponse(response, status_code=201, headers=self._no_store_headers()))

    async def authorize(self, request: Request) -> Response:
        params = request.query_params
        if params.get("response_type") != "code":
            return self._oauth_error("unsupported_response_type", "response_type must be code", 400)

        client_id = params.get("client_id")
        if not client_id:
            return self._oauth_error("invalid_request", "client_id is required", 400)
        client = self.store.get_client(client_id)
        if client is None:
            return self._oauth_error("unauthorized_client", "unknown client", 400)

        redirect_uri = params.get("redirect_uri")
        if not redirect_uri and len(client.redirect_uris) == 1:
            redirect_uri = client.redirect_uris[0]
        if not redirect_uri or redirect_uri not in client.redirect_uris:
            return self._oauth_error("invalid_request", "redirect_uri is not registered", 400)

        if params.get("code_challenge_method") != "S256":
            return self._oauth_error("invalid_request", "PKCE S256 is required", 400)
        code_challenge = params.get("code_challenge")
        if not code_challenge:
            return self._oauth_error("invalid_request", "code_challenge is required", 400)

        resource = params.get("resource")
        if resource and resource.rstrip("/") != self.settings.resource_url.rstrip("/"):
            return self._oauth_error("invalid_target", "unsupported resource", 400)

        scopes = self._normalize_scopes(params.get("scope") or client.scope)
        if scopes is None:
            return self._oauth_error("invalid_scope", "unsupported scope", 400)

        challenge = token_urlsafe(24)
        self.store.create_challenge(
            ChallengeRecord(
                challenge=challenge,
                client_id=client.client_id,
                redirect_uri=redirect_uri,
                scope=" ".join(scopes),
                state=params.get("state"),
                code_challenge=code_challenge,
                resource=resource or self.settings.resource_url,
                expires_at=int(time.time()) + self.settings.auth_challenge_seconds,
            )
        )
        return RedirectResponse(f"/oauth/login?challenge={challenge}", status_code=302)

    async def login_get(self, request: Request) -> Response:
        challenge = request.query_params.get("challenge", "")
        return self._login_form(challenge)

    async def login_post(self, request: Request) -> Response:
        form = await request.form()
        challenge_id = str(form.get("challenge", ""))
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))

        challenge = self.store.get_challenge(challenge_id)
        if challenge is None or challenge.expires_at < time.time():
            return self._login_form(challenge_id, "Authorization request expired. Start connector login again.", 400)

        if username != self.settings.owner_username or not verify_password(password, self.settings.owner_password_hash):
            return self._login_form(challenge_id, "Wrong username or password.", 401)

        code = token_urlsafe(32)
        code_hash = hash_secret(code, self.settings.secret_key)
        scopes = challenge.scope.split()
        self.store.create_code(
            CodeRecord(
                code_hash=code_hash,
                client_id=challenge.client_id,
                redirect_uri=challenge.redirect_uri,
                scopes=scopes,
                code_challenge=challenge.code_challenge,
                resource=challenge.resource,
                expires_at=int(time.time()) + self.settings.auth_code_seconds,
                used_at=None,
            )
        )
        self.store.consume_challenge(challenge.challenge)
        redirect_url = _with_query(challenge.redirect_uri, {"code": code, "state": challenge.state})
        return RedirectResponse(redirect_url, status_code=302)

    async def token(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return self._cors(Response(status_code=204))

        form = await request.form()
        grant_type = str(form.get("grant_type", ""))
        client = self._authenticate_client(request, dict(form))
        if client is None:
            return self._token_error("invalid_client", "client authentication failed", 401)

        if grant_type == "authorization_code":
            response = self._exchange_code(client, dict(form))
        elif grant_type == "refresh_token":
            response = self._exchange_refresh(client, dict(form))
        else:
            response = self._token_error("unsupported_grant_type", "unsupported grant_type", 400)
        return self._cors(response)

    def _exchange_code(self, client: ClientRecord, form: dict[str, Any]) -> Response:
        code = str(form.get("code", ""))
        code_verifier = str(form.get("code_verifier", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        code_hash = hash_secret(code, self.settings.secret_key)
        record = self.store.get_code(code_hash)

        if record is None or record.client_id != client.client_id:
            return self._token_error("invalid_grant", "authorization code does not exist", 400)
        if record.used_at is not None or record.expires_at < time.time():
            return self._token_error("invalid_grant", "authorization code is expired or already used", 400)
        if redirect_uri and redirect_uri != record.redirect_uri:
            return self._token_error("invalid_grant", "redirect_uri mismatch", 400)
        if not code_verifier or pkce_s256(code_verifier) != record.code_challenge:
            return self._token_error("invalid_grant", "PKCE verification failed", 400)

        self.store.mark_code_used(code_hash)
        return self._issue_tokens(client.client_id, record.scopes, record.resource)

    def _exchange_refresh(self, client: ClientRecord, form: dict[str, Any]) -> Response:
        refresh_token = str(form.get("refresh_token", ""))
        token_hash = hash_secret(refresh_token, self.settings.secret_key)
        record = self.store.get_refresh_token(token_hash)
        if record is None or record.client_id != client.client_id:
            return self._token_error("invalid_grant", "refresh token does not exist", 400)
        if record.revoked_at is not None or (record.expires_at is not None and record.expires_at < time.time()):
            return self._token_error("invalid_grant", "refresh token is expired or revoked", 400)

        requested_scopes = self._normalize_scopes(form.get("scope"))
        scopes = record.scopes if requested_scopes is None else requested_scopes
        if not set(scopes).issubset(set(record.scopes)):
            return self._token_error("invalid_scope", "requested scope exceeds refresh token scope", 400)

        self.store.revoke_refresh_token(token_hash)
        return self._issue_tokens(client.client_id, scopes, record.resource)

    def _issue_tokens(self, client_id: str, scopes: list[str], resource: str | None) -> Response:
        access_token = token_urlsafe(32)
        refresh_token = token_urlsafe(32)
        now = int(time.time())
        access_expires_at = now + self.settings.access_token_seconds
        refresh_expires_at = now + self.settings.refresh_token_seconds

        self.store.save_access_token(
            TokenRecord(
                token_hash=hash_secret(access_token, self.settings.secret_key),
                client_id=client_id,
                scopes=scopes,
                resource=resource,
                expires_at=access_expires_at,
                revoked_at=None,
            )
        )
        self.store.save_refresh_token(
            TokenRecord(
                token_hash=hash_secret(refresh_token, self.settings.secret_key),
                client_id=client_id,
                scopes=scopes,
                resource=resource,
                expires_at=refresh_expires_at,
                revoked_at=None,
            )
        )
        return JSONResponse(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": self.settings.access_token_seconds,
                "scope": " ".join(scopes),
                "refresh_token": refresh_token,
            },
            headers=self._no_store_headers(),
        )

    def _authenticate_client(self, request: Request, form: dict[str, Any]) -> ClientRecord | None:
        basic_client_id, basic_secret = _basic_auth(request.headers.get("authorization"))
        client_id = basic_client_id or str(form.get("client_id", ""))
        if not client_id:
            return None
        client = self.store.get_client(client_id)
        if client is None:
            return None
        if client.auth_method == "none":
            return client

        provided_secret = basic_secret if client.auth_method == "client_secret_basic" else str(form.get("client_secret", ""))
        if not provided_secret or not client.client_secret_hash:
            return None
        provided_hash = hash_secret(provided_secret, self.settings.secret_key)
        if provided_hash != client.client_secret_hash:
            return None
        return client

    def _normalize_scopes(self, scope_value: Any) -> list[str] | None:
        if not scope_value:
            return [self.settings.scope]
        scopes = str(scope_value).split()
        allowed = {self.settings.scope, *OPTIONAL_SCOPES}
        if not scopes or any(scope not in allowed for scope in scopes):
            return None
        if self.settings.scope not in scopes:
            return None
        return scopes

    def _login_form(self, challenge: str, error: str | None = None, status_code: int = 200) -> HTMLResponse:
        escaped_challenge = html.escape(challenge, quote=True)
        error_html = f"<p class=\"error\">{html.escape(error)}</p>" if error else ""
        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MCP Session Bridge Login</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 3rem auto; max-width: 32rem; line-height: 1.45; }}
    label {{ display: block; margin-top: 1rem; }}
    input {{ box-sizing: border-box; font: inherit; padding: .6rem; width: 100%; }}
    button {{ font: inherit; margin-top: 1.25rem; padding: .65rem 1rem; }}
    .error {{ color: #a40000; }}
  </style>
</head>
<body>
  <h1>MCP Session Bridge</h1>
  {error_html}
  <form method="post" action="/oauth/login">
    <input type="hidden" name="challenge" value="{escaped_challenge}">
    <label>Username <input name="username" autocomplete="username" required></label>
    <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
    <button type="submit">Authorize connector</button>
  </form>
</body>
</html>"""
        return HTMLResponse(body, status_code=status_code, headers=self._no_store_headers())

    def _oauth_error(self, error: str, description: str, status_code: int) -> Response:
        return self._cors(
            JSONResponse(
                {"error": error, "error_description": description},
                status_code=status_code,
                headers=self._no_store_headers(),
            )
        )

    def _token_error(self, error: str, description: str, status_code: int) -> Response:
        return JSONResponse(
            {"error": error, "error_description": description},
            status_code=status_code,
            headers=self._no_store_headers(),
        )

    def _cors(self, response: Response) -> Response:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, MCP-Protocol-Version"
        return response

    @staticmethod
    def _no_store_headers() -> dict[str, str]:
        return {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _basic_auth(header: str | None) -> tuple[str | None, str | None]:
    if not header or not header.lower().startswith("basic "):
        return None, None
    try:
        decoded = b64decode(header.split(" ", 1)[1]).decode("utf-8")
        client_id, secret = decoded.split(":", 1)
        return client_id, secret
    except Exception:
        return None, None


def _with_query(url: str, additions: dict[str, str | None]) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in additions.items():
        if value is not None:
            query[key] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
