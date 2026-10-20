from __future__ import annotations

import hmac
import html
import time
from base64 import b64decode
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.connectors import (
    MAX_REDIRECT_URIS,
    ConnectionLock,
    RedirectUriError,
    clean_client_name,
    redirect_kind,
    redirect_label,
)
from app.consent_session import ConsentSessions
from app.login_page import ARROW_ICON, alert_html, render_login_page
from app.rate_limits import LoginGuard, SlidingWindowLimiter, client_ip, retry_message
from app.security import hash_secret, pkce_s256, token_urlsafe
from app.settings import Settings
from app.storage import ChallengeRecord, ClientRecord, CodeRecord, Store, TokenRecord

ALLOWED_AUTH_METHODS = {"none", "client_secret_post", "client_secret_basic"}
DEFAULT_GRANT_TYPES = ["authorization_code", "refresh_token"]
DEFAULT_RESPONSE_TYPES = ["code"]
OPTIONAL_SCOPES = {"offline_access"}
# Anyone may call these two endpoints, so both are bounded per address and in total.
REGISTRATIONS_PER_IP = 20
AUTHORIZATIONS_PER_IP = 60
UNAUTHENTICATED_WINDOW_SECONDS = 10 * 60
MAX_PENDING_CHALLENGES = 500
MAX_UNAPPROVED_CLIENTS = 200
CONNECTORS_SETTINGS_PATH = "/admin/sessions?settings=connectors"


class OAuthHandlers:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        login_guard: LoginGuard | None = None,
        lock: ConnectionLock | None = None,
    ):
        self.settings = settings
        self.store = store
        self.login_guard = login_guard or LoginGuard()
        self.lock = lock or ConnectionLock(store)
        self.consent = ConsentSessions(settings.secret_key)
        self.registrations = SlidingWindowLimiter(REGISTRATIONS_PER_IP, UNAUTHENTICATED_WINDOW_SECONDS)
        self.authorizations = SlidingWindowLimiter(AUTHORIZATIONS_PER_IP, UNAUTHENTICATED_WINDOW_SECONDS)

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

        ip = client_ip(request)
        wait = self.registrations.retry_after(ip)
        if wait:
            return self._busy("too many client registrations from this address", wait)
        self.registrations.hit(ip)

        try:
            payload = await request.json()
        except Exception:
            return self._oauth_error("invalid_request", "registration body must be JSON", status_code=400)
        if not isinstance(payload, dict):
            return self._oauth_error("invalid_request", "registration body must be a JSON object", status_code=400)

        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris or not all(isinstance(v, str) for v in redirect_uris):
            return self._oauth_error("invalid_redirect_uri", "redirect_uris must be a non-empty string list", 400)
        if len(redirect_uris) > MAX_REDIRECT_URIS:
            return self._oauth_error("invalid_redirect_uri", f"at most {MAX_REDIRECT_URIS} redirect_uris are allowed", 400)
        for uri in redirect_uris:
            try:
                redirect_kind(uri)
            except RedirectUriError as exc:
                return self._oauth_error("invalid_redirect_uri", str(exc), 400)

        auth_method = payload.get("token_endpoint_auth_method") or "none"
        if auth_method not in ALLOWED_AUTH_METHODS:
            return self._oauth_error("invalid_client_metadata", "unsupported token_endpoint_auth_method", 400)

        grant_types = payload.get("grant_types") or DEFAULT_GRANT_TYPES
        response_types = payload.get("response_types") or DEFAULT_RESPONSE_TYPES
        if not _string_list(grant_types) or not _string_list(response_types):
            return self._oauth_error("invalid_client_metadata", "grant_types and response_types must be string lists", 400)
        if "authorization_code" not in grant_types or "code" not in response_types:
            return self._oauth_error("invalid_client_metadata", "authorization_code/code flow is required", 400)

        scopes = self._normalize_scopes(payload.get("scope"))
        if scopes is None:
            return self._oauth_error("invalid_scope", "unsupported scope", 400)

        if self.store.count_pending_oauth()["unapproved_clients"] >= MAX_UNAPPROVED_CLIENTS:
            return self._busy("too many clients are waiting for their first sign-in", 600)

        client_name = clean_client_name(payload.get("client_name"))
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
            client_name=client_name,
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
        if client_name:
            response["client_name"] = client_name
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
        try:
            redirect_kind(redirect_uri)
        except RedirectUriError as exc:
            return self._oauth_error("invalid_request", str(exc), 400)

        if params.get("code_challenge_method") != "S256":
            return self._oauth_error("invalid_request", "PKCE S256 is required", 400)
        code_challenge = params.get("code_challenge")
        if not code_challenge:
            return self._oauth_error("invalid_request", "code_challenge is required", 400)

        resource = self._canonical_resource(params.get("resource"))
        if resource is None:
            return self._oauth_error("invalid_target", "unsupported resource", 400)

        scopes = self._normalize_scopes(params.get("scope") or client.scope)
        if scopes is None:
            return self._oauth_error("invalid_scope", "unsupported scope", 400)

        if self.lock.blocks(client, redirect_uri):
            return self._locked_page(request, client, redirect_uri)

        ip = client_ip(request)
        wait = self.authorizations.retry_after(ip)
        if wait:
            return self._busy("too many sign-in attempts from this address", wait)
        if self.store.count_pending_oauth()["challenges"] >= MAX_PENDING_CHALLENGES:
            return self._busy("too many sign-ins are pending", 60)
        self.authorizations.hit(ip)

        challenge = token_urlsafe(24)
        self.store.create_challenge(
            ChallengeRecord(
                challenge=challenge,
                client_id=client.client_id,
                redirect_uri=redirect_uri,
                scope=" ".join(scopes),
                state=params.get("state"),
                code_challenge=code_challenge,
                resource=resource,
                expires_at=int(time.time()) + self.settings.auth_challenge_seconds,
            )
        )
        return RedirectResponse(f"/oauth/login?challenge={challenge}", status_code=302)

    async def login_get(self, request: Request) -> Response:
        pending = self._pending(request.query_params.get("challenge", ""))
        if pending is None:
            return self._expired_page()
        return self._consent_page(request, *pending)

    async def login_post(self, request: Request) -> Response:
        form = await request.form()
        pending = self._pending(str(form.get("challenge", "")))
        if pending is None:
            return self._expired_page()
        challenge, client = pending

        if form.get("action") == "deny":
            return self._deny(challenge)

        if "approval" in form:
            # One-click Allow from a browser that is signed in to Bridge.
            if not self.consent.approval_is_valid(request, challenge.challenge, str(form.get("approval", ""))):
                return self._consent_page(
                    request, challenge, client,
                    "Your Bridge sign-in in this browser has ended. Sign in to continue.", 401,
                    password=True,
                )
            return self._grant(challenge, client)

        ip = client_ip(request)
        wait = self.login_guard.retry_after(ip)
        if wait:
            return self._consent_page(
                request, challenge, client, retry_message(wait), 429,
                headers={"Retry-After": str(wait)}, password=True,
            )
        if not await self.login_guard.check(
            ip,
            str(form.get("username", "")),
            str(form.get("password", "")),
            self.settings.owner_username,
            self.settings.owner_password_hash,
        ):
            return self._consent_page(request, challenge, client, "Wrong username or password.", 401, password=True)
        response = self._grant(challenge, client)
        self.consent.set_cookie(response, request)
        return response

    def _grant(self, challenge: ChallengeRecord, client: ClientRecord) -> Response:
        self.store.approve_client(client.client_id)
        code = token_urlsafe(32)
        self.store.create_code(
            CodeRecord(
                code_hash=hash_secret(code, self.settings.secret_key),
                client_id=challenge.client_id,
                redirect_uri=challenge.redirect_uri,
                scopes=challenge.scope.split(),
                code_challenge=challenge.code_challenge,
                resource=challenge.resource,
                expires_at=int(time.time()) + self.settings.auth_code_seconds,
                used_at=None,
            )
        )
        self.store.consume_challenge(challenge.challenge)
        redirect_url = _with_query(challenge.redirect_uri, {"code": code, "state": challenge.state})
        return RedirectResponse(redirect_url, status_code=302, headers={"Referrer-Policy": "no-referrer"})

    def _deny(self, challenge: ChallengeRecord) -> Response:
        self.store.consume_challenge(challenge.challenge)
        redirect_url = _with_query(
            challenge.redirect_uri,
            {"error": "access_denied", "error_description": "The owner denied access.", "state": challenge.state},
        )
        return RedirectResponse(redirect_url, status_code=302, headers={"Referrer-Policy": "no-referrer"})

    def _pending(self, challenge_id: str) -> tuple[ChallengeRecord, ClientRecord] | None:
        if not challenge_id:
            return None
        challenge = self.store.get_challenge(challenge_id)
        if challenge is None or challenge.expires_at < time.time():
            return None
        client = self.store.get_client(challenge.client_id)
        if client is None:
            return None
        return challenge, client

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
        if not hmac.compare_digest(provided_hash, client.client_secret_hash):
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

    def _canonical_resource(self, resource: Any) -> str | None:
        if not resource:
            return self.settings.resource_url

        requested = str(resource).rstrip("/")
        allowed = {
            self.settings.resource_url.rstrip("/"),
            self.settings.public_base_url.rstrip("/"),
            self.settings.issuer_url.rstrip("/"),
        }
        if requested not in allowed:
            return None
        return self.settings.resource_url

    def _destination(self, client: ClientRecord, redirect_uri: str) -> str:
        """The consent card: where the code goes, how Bridge knows it, and the self-declared name."""
        kind = redirect_kind(redirect_uri)
        label = redirect_label(redirect_uri)
        if kind == "local":
            badge = ("local", "This computer")
        elif kind == "app":
            badge = ("local", "App on this device")
        elif client.approved_at is not None:
            badge = ("known", "Connected before")
        elif label in self._known_web_hosts():
            badge = ("known", "Known address")
        else:
            badge = ("new", "First time")
        name = (
            f"Calls itself \u201c{html.escape(client.client_name)}\u201d. Names are not verified; the address is."
            if client.client_name
            else "It gave no name. Judge it by the address."
        )
        return f"""        <div class="consent">
          <span class="consent-label">Access will be sent to</span>
          <div class="consent-host"><strong>{html.escape(label)}</strong><span class="consent-badge" data-tone="{badge[0]}">{badge[1]}</span></div>
          <p class="consent-name">{name}</p>
        </div>"""

    def _known_web_hosts(self) -> set[str]:
        hosts: set[str] = set()
        for connected in self.store.list_connected_clients():
            for uri in connected["redirect_uris"]:
                try:
                    if redirect_kind(uri) == "web":
                        hosts.add(redirect_label(uri))
                except RedirectUriError:
                    continue
        return hosts

    def _consent_page(
        self,
        request: Request,
        challenge: ChallengeRecord,
        client: ClientRecord,
        error: str | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        *,
        password: bool = False,
    ) -> HTMLResponse:
        escaped_challenge = html.escape(challenge.challenge, quote=True)
        session = None if password else self.consent.read(request)
        deny = (
            '<button class="login-secondary" type="submit" name="action" value="deny" formnovalidate>Deny</button>'
        )
        if session:
            approval = html.escape(self.consent.approval_token(session, challenge.challenge), quote=True)
            owner = html.escape(self.settings.owner_username)
            intro = f"Signed in as {owner}. Allow it to read and save the sessions you share with it?"
            form = f"""        <form class="login-form" method="post" action="/oauth/login">
          <input type="hidden" name="challenge" value="{escaped_challenge}">
          <input type="hidden" name="approval" value="{approval}">
          <div class="login-actions">
            <button class="login-submit" type="submit" name="action" value="allow"><span>Allow</span>{ARROW_ICON}</button>
            {deny}
          </div>
        </form>
        <p class="login-footer"><span class="status-dot is-warn" aria-hidden="true"></span><span>Not you? Choose Deny or close this page.</span></p>"""
        else:
            intro = "Sign in to let it read and save the sessions you share with it."
            form = f"""        <form class="login-form" method="post" action="/oauth/login">
          <input type="hidden" name="challenge" value="{escaped_challenge}">
          <div class="field">
            <label for="username">Username</label>
            <input id="username" name="username" autocomplete="username" placeholder="Enter your username" required>
          </div>
          <div class="field">
            <label for="password">Password</label>
            <input id="password" name="password" type="password" autocomplete="current-password" placeholder="Enter your password" required>
          </div>
          <div class="login-actions">
            <button class="login-submit" type="submit" name="action" value="allow"><span>Authorize connector</span>{ARROW_ICON}</button>
            {deny}
          </div>
        </form>
        <p class="login-footer"><span class="status-dot is-warn" aria-hidden="true"></span><span>Not you? Choose Deny or close this page. Nothing is shared until you sign in.</span></p>"""
        panel = f"""        <div class="form-header">
          <p class="eyebrow">Authorize connector</p>
          <h2 id="login-title">A harness wants in</h2>
          <p>{intro}</p>
        </div>
{self._destination(client, challenge.redirect_uri)}
        {alert_html(error)}
{form}"""
        return self._oauth_page(panel, status_code, headers)

    def _locked_page(self, request: Request, client: ClientRecord, redirect_uri: str) -> HTMLResponse:
        retry = html.escape(f"/oauth/authorize?{request.url.query}", quote=True)
        panel = f"""        <div class="form-header">
          <p class="eyebrow">Connections locked</p>
          <h2 id="login-title">Open a connection window</h2>
          <p>New connections to this Bridge are locked.</p>
        </div>
{self._destination(client, redirect_uri)}
        <p class="login-note">If you started this, open <strong>Settings \u2192 Connectors</strong> in Bridge admin, open a connection window, then try again. If you did not, close this page.</p>
        <div class="login-actions">
          <a class="login-submit" href="{retry}"><span>Try again</span>{ARROW_ICON}</a>
          <a class="login-secondary" href="{CONNECTORS_SETTINGS_PATH}" target="_blank" rel="noopener noreferrer">Open Bridge admin</a>
        </div>"""
        return self._oauth_page(panel, 403)

    def _expired_page(self) -> HTMLResponse:
        minutes = max(1, self.settings.auth_challenge_seconds // 60)
        panel = f"""        <div class="form-header">
          <p class="eyebrow">Authorize connector</p>
          <h2 id="login-title">This link expired</h2>
          <p>Authorization request expired. Start connector login again from your harness; each sign-in link lasts {minutes} minutes.</p>
        </div>"""
        return self._oauth_page(panel, 400)

    def _oauth_page(self, panel: str, status_code: int, headers: dict[str, str] | None = None) -> HTMLResponse:
        return render_login_page(
            page_title="Authorize connector \u00b7 MCP Session Bridge",
            visual_eyebrow="Connector access",
            visual_title="The conversation continues.",
            visual_copy=(
                "A harness that signs in here can read and add to every session whose ID it is "
                "given, and to session groups."
            ),
            meta_label="OAUTH / CONNECT",
            meta_text="Owner sign-in for a connector",
            panel_html=panel,
            status_code=status_code,
            headers={"Referrer-Policy": "no-referrer", **(headers or {})},
            compact_on_mobile=True,
        )

    def _busy(self, description: str, retry_after: int) -> Response:
        response = self._oauth_error("temporarily_unavailable", description, 429)
        response.headers["Retry-After"] = str(retry_after)
        return response

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


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


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
