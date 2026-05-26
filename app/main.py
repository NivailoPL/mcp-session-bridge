from __future__ import annotations

import time
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.oauth import OAuthHandlers
from app.security import hash_secret
from app.settings import load_settings
from app.storage import Store

settings = load_settings()
store = Store(settings.db_path)


class BridgeTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        token_hash = hash_secret(token, settings.secret_key)
        record = store.get_access_token(token_hash)
        if record is None:
            return None
        if record.revoked_at is not None:
            return None
        if record.expires_at is not None and record.expires_at < time.time():
            return None
        return AccessToken(
            token=token,
            client_id=record.client_id,
            scopes=record.scopes,
            expires_at=record.expires_at,
            resource=record.resource,
        )


mcp = FastMCP(
    name="MCP Session Bridge",
    instructions="Auth-first spike for testing Claude and ChatGPT remote MCP connectors. Do not store sensitive session content yet.",
    token_verifier=BridgeTokenVerifier(),
    auth=AuthSettings(
        issuer_url=settings.issuer_url,
        resource_server_url=settings.resource_url,
        required_scopes=[settings.scope],
    ),
    streamable_http_path=settings.resource_path,
    json_response=True,
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "mcp.panchmurka.wtf",
            "mcp.panchmurka.wtf:443",
            "127.0.0.1:8787",
            "localhost:8787",
        ],
        allowed_origins=[
            "https://mcp.panchmurka.wtf",
            "https://claude.ai",
            "https://chatgpt.com",
            "https://chat.openai.com",
        ],
    ),
)

oauth = OAuthHandlers(settings, store)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True, "service": "mcp-session-bridge"})


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET", "OPTIONS"])
async def oauth_metadata(request: Request) -> Response:
    return await oauth.metadata(request)


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
async def protected_resource_metadata(request: Request) -> Response:
    return await oauth.protected_resource_metadata(request)


@mcp.custom_route("/oauth/register", methods=["POST", "OPTIONS"])
async def oauth_register(request: Request) -> Response:
    return await oauth.register(request)


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def oauth_authorize(request: Request) -> Response:
    return await oauth.authorize(request)


@mcp.custom_route("/oauth/login", methods=["GET"])
async def oauth_login_get(request: Request) -> Response:
    return await oauth.login_get(request)


@mcp.custom_route("/oauth/login", methods=["POST"])
async def oauth_login_post(request: Request) -> Response:
    return await oauth.login_post(request)


@mcp.custom_route("/oauth/token", methods=["POST", "OPTIONS"])
async def oauth_token(request: Request) -> Response:
    return await oauth.token(request)


@mcp.tool()
def bridge_ping() -> dict[str, Any]:
    """Return a minimal health response proving the authenticated MCP tool path works."""
    return {"ok": True, "service": "mcp-session-bridge", "scope": settings.scope}


@mcp.tool()
def auth_whoami() -> dict[str, Any]:
    """Return the OAuth client identity attached to the current tool call."""
    token = get_access_token()
    if token is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "client_id": token.client_id,
        "scopes": token.scopes,
        "resource": token.resource,
        "expires_at": token.expires_at,
    }


@mcp.tool()
def save_probe(key: str, value: str) -> dict[str, Any]:
    """Save a non-sensitive probe string for cross-client testing only."""
    token = get_access_token()
    updated_by = token.client_id if token else "unknown"
    return store.save_probe(key=key, value=value, updated_by=updated_by)


@mcp.tool()
def read_probe(key: str) -> dict[str, Any]:
    """Read a non-sensitive probe string saved during connector testing."""
    value = store.read_probe(key)
    if value is None:
        return {"found": False, "key": key}
    return {"found": True, **value}


app = mcp.streamable_http_app()
