from __future__ import annotations

import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.context_packs import ContextPackStore
from app.oauth import OAuthHandlers
from app.security import hash_secret
from app.session_package import render_session_package, render_session_transcript
from app.settings import load_settings
from app.storage import Store

settings = load_settings()
store = Store(settings.db_path)
context_packs = ContextPackStore(settings.context_packs_dir)


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


@mcp.tool()
def list_context_packs() -> dict[str, Any]:
    """List available context packs stored on the VPS."""
    return {"ok": True, "context_packs": context_packs.list_packs()}


@mcp.tool()
def create_session(context_pack_id: str = "", title: str = "") -> dict[str, Any]:
    """Create a new brainstorming session. Title is optional and may be inferred after the first exchange."""
    resolved_context_pack_id = context_pack_id.strip() or settings.default_context_pack_id
    context_pack = context_packs.load_pack(resolved_context_pack_id)
    resolved_title = title.strip()
    title_is_auto = not resolved_title
    if title_is_auto:
        resolved_title = _auto_title()
    session_id = _new_session_id(resolved_title, title_is_auto=title_is_auto)
    session = store.create_session(
        session_id=session_id,
        title=resolved_title,
        context_pack_id=context_pack.pack_id,
        title_is_auto=title_is_auto,
    )
    return {
        "ok": True,
        "session_id": session.session_id,
        "title": session.title,
        "context_pack_id": session.context_pack_id,
        "context_pack_name": context_pack.name,
        "title_is_auto": session.title_is_auto,
        "created_at": session.created_at,
    }


@mcp.tool()
def list_sessions() -> dict[str, Any]:
    """List saved brainstorming sessions."""
    return {"ok": True, "sessions": store.list_sessions()}


@mcp.tool()
def get_session_package(session_id: str) -> dict[str, Any]:
    """Return the full context pack and transcript for a session as one Markdown package."""
    session = store.get_session(session_id)
    if session is None:
        return {"ok": False, "error": f"Unknown session_id: {session_id}"}
    context_pack = context_packs.load_pack(session.context_pack_id)
    exchanges = store.list_exchanges(session.session_id)
    return {"ok": True, **render_session_package(session, context_pack, exchanges)}


@mcp.tool()
def get_session_transcript(session_id: str) -> dict[str, Any]:
    """Return only the saved conversation transcript for audit, without context pack files."""
    session = store.get_session(session_id)
    if session is None:
        return {"ok": False, "error": f"Unknown session_id: {session_id}"}
    exchanges = store.list_exchanges(session.session_id)
    return {"ok": True, **render_session_transcript(session, exchanges)}


@mcp.tool()
def save_exchange(
    session_id: str,
    model_name: str,
    user_message: str,
    assistant_response: str,
) -> dict[str, Any]:
    """Save one full Wojtek/model exchange in the shared session transcript."""
    exchange = store.save_exchange(
        session_id=session_id,
        model_name=model_name.strip() or "Unknown model",
        user_message=user_message.strip(),
        assistant_response=assistant_response.strip(),
    )
    return {
        "ok": True,
        "exchange_id": exchange.exchange_id,
        "session_id": exchange.session_id,
        "model_name": exchange.model_name,
        "user_message_chars": len(exchange.user_message),
        "assistant_response_chars": len(exchange.assistant_response),
        "created_at": exchange.created_at,
    }


@mcp.tool()
def export_session_markdown(session_id: str) -> dict[str, Any]:
    """Export the current full session package as Markdown."""
    package = get_session_package(session_id)
    if not package.get("ok"):
        return package
    return {
        "ok": True,
        "session_id": package["session_id"],
        "char_count": package["char_count"],
        "sha256": package["sha256"],
        "markdown": package["package_markdown"],
    }


def _new_session_id(title: str, title_is_auto: bool = False) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug_source = "session" if title_is_auto else title
    slug = re.sub(r"[^a-z0-9]+", "-", slug_source.lower()).strip("-")[:36]
    if not slug:
        slug = "session"
    return f"{stamp}-{slug}-{secrets.token_hex(3)}"


def _auto_title() -> str:
    return "Sesja " + datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


app = mcp.streamable_http_app()
