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

from app.admin import AdminHandlers
from app.oauth import OAuthHandlers
from app.security import hash_secret
from app.session_package import render_session_overview, render_session_transcript_chunk
from app.session_summaries import SessionSummaryStore
from app.settings import ROOT, load_settings
from app.storage import Store
from app.time_format import (
    DEFAULT_DISPLAY_TIMEZONE_NAME,
    DISPLAY_TIMEZONE_SETTING_KEY,
    format_response_timestamp,
    resolve_timezone_name,
)

MANUAL_CONTEXT_ID = "manual-context"
SERVER_INSTRUCTIONS = (
    "MCP Session Bridge is a shared transcript bridge for multi-model conversations. "
    "User context files are supplied manually outside MCP. If a session_id is known, "
    "call get_session_overview, then fetch every get_session_transcript_chunk before "
    "answering. Before showing a final answer for an active session, call save_exchange "
    "with the full user message and full assistant response. Use list_session_groups before "
    "create_session; use list_sessions to find an existing session."
)

settings = load_settings()
store = Store(settings.db_path)
summary_store = SessionSummaryStore(settings.summaries_dir)


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
    instructions=SERVER_INSTRUCTIONS,
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
        allowed_hosts=settings.transport_allowed_hosts,
        allowed_origins=settings.transport_allowed_origins,
    ),
)

oauth = OAuthHandlers(settings, store)
admin = AdminHandlers(settings, store, ROOT / "admin-viewer.html")


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


@mcp.custom_route("/admin", methods=["GET"])
async def admin_index(request: Request) -> Response:
    return await admin.index(request)


@mcp.custom_route("/admin/sessions", methods=["GET"])
async def admin_sessions_page(request: Request) -> Response:
    return await admin.sessions_page(request)


@mcp.custom_route("/admin/login", methods=["GET"])
async def admin_login_get(request: Request) -> Response:
    return await admin.login_get(request)


@mcp.custom_route("/admin/login", methods=["POST"])
async def admin_login_post(request: Request) -> Response:
    return await admin.login_post(request)


@mcp.custom_route("/admin/logout", methods=["POST"])
async def admin_logout(request: Request) -> Response:
    return await admin.logout(request)


@mcp.custom_route("/admin/api/me", methods=["GET"])
async def admin_api_me(request: Request) -> Response:
    return await admin.api_me(request)


@mcp.custom_route("/admin/api/timezone", methods=["POST", "PUT"])
async def admin_api_update_timezone(request: Request) -> Response:
    return await admin.api_update_timezone(request)


@mcp.custom_route("/admin/api/sessions", methods=["GET"])
async def admin_api_sessions(request: Request) -> Response:
    return await admin.api_sessions(request)


@mcp.custom_route("/admin/api/sessions/{session_id}", methods=["GET"])
async def admin_api_session(request: Request) -> Response:
    return await admin.api_session(request)


@mcp.custom_route("/admin/api/sessions/{session_id}", methods=["PATCH"])
async def admin_api_update_session(request: Request) -> Response:
    return await admin.api_update_session(request)


@mcp.custom_route("/admin/api/files/{file_id}", methods=["GET"])
async def admin_api_file(request: Request) -> Response:
    return await admin.api_file(request)


@mcp.custom_route("/admin/api/session-groups", methods=["GET"])
async def admin_api_session_groups(request: Request) -> Response:
    return await admin.api_session_groups(request)


@mcp.custom_route("/admin/api/session-groups", methods=["POST"])
async def admin_api_create_session_group(request: Request) -> Response:
    return await admin.api_create_session_group(request)


@mcp.custom_route("/admin/api/session-groups/{group_id}", methods=["PATCH"])
async def admin_api_update_session_group(request: Request) -> Response:
    return await admin.api_update_session_group(request)


@mcp.custom_route("/admin/api/session-groups/{group_id}", methods=["DELETE"])
async def admin_api_delete_session_group(request: Request) -> Response:
    return await admin.api_delete_session_group(request)


@mcp.custom_route("/admin/api/exchanges/{exchange_id}", methods=["PATCH"])
async def admin_api_update_exchange(request: Request) -> Response:
    return await admin.api_update_exchange(request)


@mcp.custom_route("/admin/api/exchanges/{exchange_id}", methods=["DELETE"])
async def admin_api_delete_exchange(request: Request) -> Response:
    return await admin.api_delete_exchange(request)


@mcp.custom_route("/admin/api/exchanges/{exchange_id}/restore", methods=["POST"])
async def admin_api_restore_exchange(request: Request) -> Response:
    return await admin.api_restore_exchange(request)


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
def list_session_groups() -> dict[str, Any]:
    """List available local session groups so a model can choose a valid group_id before creating a session."""
    return {"ok": True, "groups": store.list_session_groups()}


@mcp.tool()
def create_session(title: str = "", group_id: str = "") -> dict[str, Any]:
    """Create a new model-to-model conversation session. Title is optional and group_id defaults to uncategorized."""
    resolved_title = title.strip()
    title_is_auto = not resolved_title
    if title_is_auto:
        resolved_title = _auto_title()
    session_id = _new_session_id(resolved_title, title_is_auto=title_is_auto)
    try:
        session = store.create_session(
            session_id=session_id,
            title=resolved_title,
            context_pack_id=MANUAL_CONTEXT_ID,
            title_is_auto=title_is_auto,
            group_id=group_id,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    group = store.get_session_group(session.group_id)
    return {
        "ok": True,
        "session_id": session.session_id,
        "title": session.title,
        "group_id": session.group_id,
        "group": _group_payload(group),
        "context_source": "manual",
        "title_is_auto": session.title_is_auto,
        "created_at": session.created_at,
    }


@mcp.tool()
def list_sessions() -> dict[str, Any]:
    """List saved brainstorming sessions."""
    sessions = []
    for session in store.list_sessions():
        sessions.append(
            {
                "session_id": session["session_id"],
                "title": session["title"],
                "group_id": session["group_id"],
                "group": session["group"],
                "title_is_auto": session["title_is_auto"],
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
                "exchange_count": session["exchange_count"],
            }
        )
    return {"ok": True, "sessions": sessions}


@mcp.tool()
def get_session_overview(session_id: str) -> dict[str, Any]:
    """Return lightweight session metadata and transcript chunking information, without transcript content."""
    session = store.get_session(session_id)
    if session is None:
        return {"ok": False, "error": f"Unknown session_id: {session_id}"}
    exchanges = store.list_exchanges(session.session_id)
    summaries = summary_store.list_summaries(session.session_id)
    display_timezone = _display_timezone_name()
    group = store.get_session_group(session.group_id)
    files = {
        "session": store.list_session_files(session_id=session.session_id),
        "group": store.list_session_files(group_id=session.group_id),
    }
    return {
        "ok": True,
        "context_source": "manual",
        "summary_count": len(summaries),
        "group": _group_payload(group),
        "files": files,
        **render_session_overview(
            session,
            exchanges,
            max_lines=settings.transcript_chunk_max_lines,
            max_chars=settings.transcript_chunk_max_chars,
            timezone_name=display_timezone,
        ),
    }


@mcp.tool()
def get_session_transcript_chunk(session_id: str, chunk_index: int = 1) -> dict[str, Any]:
    """Return one bounded chunk of the saved conversation transcript."""
    session = store.get_session(session_id)
    if session is None:
        return {"ok": False, "error": f"Unknown session_id: {session_id}"}
    exchanges = store.list_exchanges(session.session_id)
    display_timezone = _display_timezone_name()
    try:
        chunk = render_session_transcript_chunk(
            session,
            exchanges,
            chunk_index=chunk_index,
            max_lines=settings.transcript_chunk_max_lines,
            max_chars=settings.transcript_chunk_max_chars,
            timezone_name=display_timezone,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **chunk}


@mcp.tool()
def save_exchange(
    session_id: str,
    model_name: str,
    user_message: str,
    assistant_response: str,
) -> dict[str, Any]:
    """Save one full user/model exchange in the shared session transcript."""
    exchange = store.save_exchange(
        session_id=session_id,
        model_name=model_name.strip() or "Unknown model",
        user_message=user_message.strip(),
        assistant_response=assistant_response.strip(),
    )
    display_timezone = _display_timezone_name()
    return {
        "ok": True,
        "exchange_id": exchange.exchange_id,
        "session_id": exchange.session_id,
        "model_name": exchange.model_name,
        "user_message_chars": len(exchange.user_message),
        "assistant_response_chars": len(exchange.assistant_response),
        "assistant_created_at": exchange.assistant_created_at,
        "assistant_created_at_display": format_response_timestamp(
            exchange.assistant_created_at,
            timezone_name=display_timezone,
        ),
        "assistant_created_at_timezone": display_timezone,
        "created_at": exchange.created_at,
    }


@mcp.tool()
def save_session_summary(
    session_id: str,
    model_name: str,
    summary_markdown: str,
    title: str = "",
) -> dict[str, Any]:
    """Save a Markdown summary file for the current conversation session."""
    resolved_session_id = session_id.strip()
    if not resolved_session_id:
        return {"ok": False, "error": "session_id must not be empty"}

    content = summary_markdown.strip()
    if not content:
        return {"ok": False, "error": "summary_markdown must not be empty"}

    session = store.get_session(resolved_session_id)
    if session is None:
        return {"ok": False, "error": f"Unknown session_id: {resolved_session_id}"}

    try:
        saved = summary_store.save_summary(
            session_id=session.session_id,
            model_name=model_name.strip() or "Unknown model",
            summary_markdown=content,
            title=title,
        )
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "session_id": saved.session_id,
        "title": saved.title,
        "model_name": saved.model_name,
        "path": saved.path,
        "file_path": saved.file_path,
        "chars": saved.chars,
        "sha256": saved.sha256,
        "created_at": saved.created_at,
    }


@mcp.tool()
def list_session_summaries(session_id: str) -> dict[str, Any]:
    """List Markdown summaries saved for a conversation session."""
    resolved_session_id = session_id.strip()
    if not resolved_session_id:
        return {"ok": False, "error": "session_id must not be empty"}
    session = store.get_session(resolved_session_id)
    if session is None:
        return {"ok": False, "error": f"Unknown session_id: {resolved_session_id}"}
    try:
        summaries = summary_store.list_summaries(session.session_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "session_id": session.session_id, "summary_count": len(summaries), "summaries": summaries}


@mcp.tool()
def upload_session_file(
    session_id: str,
    filename: str,
    content: str,
    mime_type: str = "text/markdown",
) -> dict[str, Any]:
    """Save a text file that belongs only to one conversation session."""
    token = get_access_token()
    created_by = token.client_id if token else "unknown"
    try:
        saved = store.save_session_file(
            session_id=session_id,
            filename=filename,
            content=content,
            mime_type=mime_type,
            created_by=created_by,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "file": _file_payload(saved)}


@mcp.tool()
def upload_group_file(
    group_id: str,
    filename: str,
    content: str,
    mime_type: str = "text/markdown",
) -> dict[str, Any]:
    """Save a text file as durable context for an entire session group."""
    token = get_access_token()
    created_by = token.client_id if token else "unknown"
    try:
        saved = store.save_group_file(
            group_id=group_id,
            filename=filename,
            content=content,
            mime_type=mime_type,
            created_by=created_by,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "file": _file_payload(saved)}


@mcp.tool()
def list_session_files(session_id: str = "", group_id: str = "") -> dict[str, Any]:
    """List uploaded text files, optionally filtered by session_id and/or group_id."""
    return {
        "ok": True,
        "files": store.list_session_files(
            session_id=session_id.strip() or None,
            group_id=group_id.strip() or None,
        ),
    }


@mcp.tool()
def download_session_file(file_id: int) -> dict[str, Any]:
    """Download one uploaded text file by file_id."""
    saved = store.get_session_file(file_id)
    if saved is None:
        return {"ok": False, "error": f"Unknown file_id: {file_id}"}
    return {"ok": True, "file": _file_payload(saved, include_content=True)}


def _new_session_id(title: str, title_is_auto: bool = False) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug_source = "session" if title_is_auto else title
    slug = re.sub(r"[^a-z0-9]+", "-", slug_source.lower()).strip("-")[:36]
    if not slug:
        slug = "session"
    return f"{stamp}-{slug}-{secrets.token_hex(3)}"


def _auto_title() -> str:
    return "Session " + datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _display_timezone_name() -> str:
    try:
        return resolve_timezone_name(store.get_app_setting(DISPLAY_TIMEZONE_SETTING_KEY))
    except ValueError:
        store.set_app_setting(DISPLAY_TIMEZONE_SETTING_KEY, DEFAULT_DISPLAY_TIMEZONE_NAME)
        return DEFAULT_DISPLAY_TIMEZONE_NAME


def _group_payload(group: Any) -> dict[str, Any] | None:
    if group is None:
        return None
    return {
        "group_id": group.group_id,
        "name": group.name,
        "color": group.color,
        "icon_key": group.icon_key,
        "sort_order": group.sort_order,
        "is_system": group.is_system,
    }


def _file_payload(file: Any, include_content: bool = False) -> dict[str, Any]:
    payload = {
        "file_id": file.file_id,
        "scope_type": file.scope_type,
        "session_id": file.session_id,
        "group_id": file.group_id,
        "filename": file.filename,
        "mime_type": file.mime_type,
        "sha256": file.sha256,
        "size_bytes": file.size_bytes,
        "created_by": file.created_by,
        "created_at": file.created_at,
    }
    if include_content:
        payload["content"] = file.content
    return payload


app = mcp.streamable_http_app()
