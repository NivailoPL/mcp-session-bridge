from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import html
import json
import re
import time
import urllib.error
import urllib.request
import urllib.parse
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from bridge_cli.version import BRIDGE_VERSION

from app.graph_config import GraphConfigError
from app.graph_runtime import GraphRuntime
from app.codex_app_server import (
    MAX_CHAT_MESSAGE_CHARS,
    CodexAppServerClient,
    CodexPolicyViolationError,
    CodexProtocolError,
    CodexTimeoutError,
    CodexUnavailableError,
)

from app.search import (
    COHERE_KEY_SETTING,
    OPENAI_KEY_SETTING,
    RENAME_MODEL_SETTING,
    SearchConfig,
    SearchService,
)
from app.pdf_files import (
    MAX_ADMIN_PDF_BYTES,
    PdfWorkerBusyError,
    decode_pdf_base64,
    extract_pdf_text_isolated,
    run_pdf_worker,
)
from app.output_probe import (
    MAX_PROBE_TARGET_CHARS,
    MAX_TRANSCRIPT_CHUNK_CHARS,
    MAX_TRANSCRIPT_CHUNK_LINES,
    MIN_PROBE_TARGET_CHARS,
    MIN_TRANSCRIPT_CHUNK_CHARS,
    MIN_TRANSCRIPT_CHUNK_LINES,
    PROBE_SAFETY_MARGIN,
    TRANSCRIPT_CHUNK_MAX_CHARS_SETTING,
    TRANSCRIPT_CHUNK_MAX_LINES_SETTING,
    probe_recommendations,
    public_probe_run,
    transcript_chunk_limits,
    validate_transcript_chunk_limits,
)
from app.security import token_urlsafe, verify_password
from app.settings import Settings
from app.storage import (
    MAX_SESSION_FILE_BYTES,
    ExchangeRecord,
    PdfStorageQuotaError,
    SessionFileConflictError,
    SessionFileRecord,
    SessionGroupRecord,
    SessionRecord,
    Store,
    session_file_payload,
)
from app.time_format import (
    DEFAULT_DISPLAY_TIMEZONE_NAME,
    DISPLAY_TIMEZONE_SETTING_KEY,
    format_response_timestamp,
    format_timestamp_iso,
    resolve_timezone_name,
)
from app.tool_output import (
    DEFAULT_TOOL_OUTPUT_MODE,
    TOOL_OUTPUT_RESTART_PENDING_SETTING,
    configured_tool_output_mode,
    validate_tool_output_mode,
)

ADMIN_COOKIE = "mcp_bridge_admin"
ADMIN_SESSION_SECONDS = 12 * 60 * 60
TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)
AI_RENAME_API_KEY_SETTING = "ai_rename.api_key"
AI_RENAME_MODEL_SETTING = "ai_rename.model"
AI_RENAME_DEFAULT_MODEL = "gpt-5.4-nano"
AI_RENAME_ENDPOINT = "https://api.openai.com/v1/chat/completions"
SESSION_TITLE_MAX_CHARS = 72
ADMIN_FILE_UPLOAD_MAX_BODY_BYTES = ((MAX_ADMIN_PDF_BYTES + 2) // 3 * 4) + 16_384
ADMIN_FILE_EDIT_MAX_BODY_BYTES = (MAX_SESSION_FILE_BYTES * 6) + 16_384
CODEX_CHAT_MAX_BODY_BYTES = MAX_CHAT_MESSAGE_CHARS * 4 + 1_024
CodexAppServerErrorTypes = (
    CodexUnavailableError,
    CodexProtocolError,
    CodexTimeoutError,
    CodexPolicyViolationError,
)
ADMIN_FILE_EXTENSIONS = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".pdf": "application/pdf",
}
BRAND_ASSET_MEDIA_TYPES = {
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
}


class AdminHandlers:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        html_path: Path,
        *,
        active_tool_output_mode: str = DEFAULT_TOOL_OUTPUT_MODE,
        restart_requester: Callable[[], Awaitable[None]] | None = None,
        codex_client: CodexAppServerClient | None = None,
        graph_runtime: GraphRuntime | None = None,
    ):
        self.settings = settings
        self.store = store
        self.html_path = html_path
        self.graph_html_path = html_path.parent / "graph-viewer.html"
        self.graph_asset_dir = html_path.parent
        self.brand_dir = html_path.parent / "brand"
        self.pdfjs_dir = html_path.parent / "vendor" / "pdfjs"
        self.search = SearchService(store)
        self.active_tool_output_mode = active_tool_output_mode
        self.restart_requester = restart_requester
        self.codex = codex_client
        self.graph_runtime = graph_runtime

    async def index(self, request: Request) -> Response:
        return RedirectResponse("/admin/sessions", status_code=303)

    async def sessions_page(self, request: Request) -> Response:
        session, error = self._require_admin(request)
        if error:
            return error
        try:
            body = self.html_path.read_text(encoding="utf-8")
        except OSError:
            return HTMLResponse("Admin viewer is not installed.", status_code=500, headers=self._no_store_headers())
        return HTMLResponse(body, headers=self._admin_headers())

    async def graph_page(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        try:
            body = self.graph_html_path.read_text(encoding="utf-8")
        except OSError:
            return HTMLResponse("Graph workspace is not installed.", status_code=500, headers=self._no_store_headers())
        return HTMLResponse(body, headers=self._admin_headers())

    async def graph_asset(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        asset_name = str(request.path_params.get("asset_name", ""))
        media_types = {
            "graph-viewer.css": "text/css",
            "graph-data.css": "text/css",
            "pearl-gradient-nav.js": "text/javascript",
            "pearl-gradient-nav.css": "text/css",
            "graph-viewer.js": "text/javascript",
        }
        media_type = media_types.get(asset_name)
        if media_type is None:
            return Response(status_code=404)
        asset_path = self.graph_asset_dir / asset_name
        if not asset_path.is_file():
            return Response(status_code=404)
        return FileResponse(
            asset_path,
            media_type=media_type,
            headers={**self._no_store_headers(), "X-Content-Type-Options": "nosniff"},
        )

    async def login_get(self, request: Request) -> Response:
        next_path = _safe_next(request.query_params.get("next"))
        return self._login_form(next_path)

    async def login_post(self, request: Request) -> Response:
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        next_path = _safe_next(str(form.get("next", "")))

        if username != self.settings.owner_username or not verify_password(password, self.settings.owner_password_hash):
            return self._login_form(next_path, "Invalid username or password.", status_code=401)

        cookie = self._make_cookie(username)
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            ADMIN_COOKIE,
            cookie,
            max_age=ADMIN_SESSION_SECONDS,
            httponly=True,
            secure=_request_is_secure(request),
            samesite="strict",
            path="/admin",
        )
        return response

    async def logout(self, request: Request) -> Response:
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(ADMIN_COOKIE, path="/admin")
        return response

    async def api_me(self, request: Request) -> Response:
        session, error = self._require_admin(request)
        if error:
            return error
        display_timezone = self._display_timezone_name()
        return JSONResponse(
            {
                "ok": True,
                "username": session["username"],
                "csrf_token": session["csrf"],
                "expires_at": session["exp"],
                "display_timezone": display_timezone,
            },
            headers=self._no_store_headers(),
        )

    async def api_update_timezone(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error

        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        timezone_value = payload.get("timezone")
        if not isinstance(timezone_value, str):
            return self._json_error("timezone must be a string.", status_code=400)

        try:
            display_timezone = resolve_timezone_name(timezone_value)
        except ValueError as exc:
            return self._json_error(str(exc), status_code=400)

        self.store.set_app_setting(DISPLAY_TIMEZONE_SETTING_KEY, display_timezone)
        return JSONResponse(
            {"ok": True, "display_timezone": display_timezone},
            headers=self._no_store_headers(),
        )

    async def api_ai_settings(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        return JSONResponse(
            {"ok": True, "settings": self._ai_settings_payload()},
            headers=self._no_store_headers(),
        )

    async def api_operational_status(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        return JSONResponse(
            {"ok": True, "status": self._operational_status_payload()},
            headers=self._no_store_headers(),
        )

    async def api_codex_status(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        if self.codex is None:
            return self._codex_unavailable()
        try:
            status = await self.codex.status()
        except CodexAppServerErrorTypes as exc:
            return self._codex_error(exc)
        return JSONResponse({"ok": True, "codex": status}, headers=self._no_store_headers())

    async def api_codex_device_login_start(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        if self.codex is None:
            return self._codex_unavailable()
        try:
            login = await self.codex.start_device_login()
        except CodexAppServerErrorTypes as exc:
            return self._codex_error(exc)
        return JSONResponse({"ok": True, "login": login}, headers=self._no_store_headers())

    async def api_codex_device_login_status(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        if self.codex is None:
            return self._codex_unavailable()
        try:
            status = await self.codex.device_login_status()
        except CodexAppServerErrorTypes as exc:
            return self._codex_error(exc)
        return JSONResponse({"ok": True, "codex": status}, headers=self._no_store_headers())

    async def api_codex_device_login_cancel(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        if self.codex is None:
            return self._codex_unavailable()
        try:
            await self.codex.cancel_device_login()
        except CodexAppServerErrorTypes as exc:
            return self._codex_error(exc)
        return JSONResponse({"ok": True}, headers=self._no_store_headers())

    async def api_codex_logout(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        if self.codex is None:
            return self._codex_unavailable()
        try:
            await self.codex.logout()
        except CodexAppServerErrorTypes as exc:
            return self._codex_error(exc)
        return JSONResponse({"ok": True}, headers=self._no_store_headers())

    async def api_codex_chat(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        if self.codex is None:
            return self._codex_unavailable()
        payload, parse_error = await _bounded_json_body(
            request,
            max_bytes=CODEX_CHAT_MAX_BODY_BYTES,
        )
        if parse_error:
            return parse_error
        if "message" not in payload or not set(payload) <= {"message", "thread_id"}:
            return self._json_error(
                "Chat requires message and accepts optional thread_id.",
                status_code=400,
            )
        message = payload.get("message")
        thread_id = payload.get("thread_id")
        if not isinstance(message, str):
            return self._json_error("message must be a string.", status_code=400)
        if thread_id is not None and not isinstance(thread_id, str):
            return self._json_error("thread_id must be a string or null.", status_code=400)
        try:
            chat = await self.codex.chat(message, thread_id=thread_id)
        except ValueError as exc:
            if str(exc) == "Unknown or expired Codex conversation.":
                return JSONResponse(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": "codex_conversation_expired",
                    },
                    status_code=400,
                    headers=self._no_store_headers(),
                )
            return self._json_error(str(exc), status_code=400)
        except CodexAppServerErrorTypes as exc:
            return self._codex_error(exc)
        return JSONResponse({"ok": True, "chat": chat}, headers=self._no_store_headers())

    async def api_graph_config(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        return JSONResponse(
            {"ok": True, "config": self.store.get_graph_config()},
            headers=self._no_store_headers(),
        )

    async def api_graph_jobs(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        status = request.query_params.get("status")
        jobs = self.store.list_graph_jobs(status=status or None)
        return JSONResponse({"ok": True, "jobs": jobs}, headers=self._no_store_headers())

    async def api_graph_rescan(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        if self.graph_runtime is None:
            return self._json_error("Graph runtime is unavailable.", status_code=503)
        if not await self._graph_provider_is_ready():
            return self._graph_provider_not_ready()
        try:
            result = await self.graph_runtime.reset_all()
        except ValueError as exc:
            return self._json_error(str(exc), status_code=409)
        return JSONResponse(
            {"ok": True, "reset": result},
            status_code=202,
            headers=self._no_store_headers(),
        )

    async def api_graph_analysis_list(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        return JSONResponse(
            {"ok": True, "sessions": self.store.list_graph_analysis_sessions()},
            headers=self._no_store_headers(),
        )

    async def api_graph_analysis(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        session_id = str(request.path_params.get("session_id", ""))
        analysis = self.store.get_graph_analysis(session_id)
        if analysis is None:
            return self._json_error("No production Graph analysis for this session.", status_code=404)
        return JSONResponse({"ok": True, "analysis": analysis}, headers=self._no_store_headers())

    async def api_graph_lab_runs(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        return JSONResponse(
            {"ok": True, "runs": self.store.list_graph_lab_runs()},
            headers=self._no_store_headers(),
        )

    async def api_graph_lab_start(self, request: Request) -> Response:
        session, error = self._require_admin_mutation(request)
        if error:
            return error
        if self.graph_runtime is None:
            return self._json_error("Graph runtime is unavailable.", status_code=503)
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        session_id = payload.get("session_id")
        overrides = payload.get("settings", {})
        if not isinstance(session_id, str) or not session_id.strip():
            return self._json_error("session_id must be a non-empty string.", status_code=400)
        if not isinstance(overrides, dict):
            return self._json_error("settings must be an object.", status_code=400)
        try:
            run = await self.graph_runtime.start_lab_run(
                session_id.strip(), overrides, actor=session["username"]
            )
        except (GraphConfigError, ValueError) as exc:
            return self._json_error(str(exc), status_code=400)
        return JSONResponse(
            {"ok": True, "run": run},
            status_code=202,
            headers=self._no_store_headers(),
        )

    async def api_graph_unlock(self, request: Request) -> Response:
        session, error = self._require_admin_mutation(request)
        if error:
            return error
        self.store.unlock_graph_profile(session["username"])
        return self._graph_config_response()

    async def api_graph_update_draft(self, request: Request) -> Response:
        session, error = self._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        try:
            self.store.update_graph_draft(payload, session["username"])
        except (GraphConfigError, ValueError) as exc:
            return self._json_error(str(exc), status_code=400)
        return self._graph_config_response()

    async def api_graph_activate(self, request: Request) -> Response:
        session, error = self._require_admin_mutation(request)
        if error:
            return error
        try:
            self.store.activate_graph_draft(session["username"])
        except ValueError as exc:
            return self._json_error(str(exc), status_code=400)
        return self._graph_config_response()

    async def api_graph_discard_draft(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        self.store.discard_graph_draft()
        return self._graph_config_response()

    async def api_graph_state(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return self._json_error("enabled must be a boolean.", status_code=400)
        if enabled:
            if not await self._graph_provider_is_ready():
                return self._graph_provider_not_ready()
        if self.graph_runtime is not None:
            await self.graph_runtime.set_enabled(enabled)
        else:
            self.store.set_graph_enabled(enabled)
        return self._graph_config_response()

    def _graph_config_response(self) -> JSONResponse:
        return JSONResponse(
            {"ok": True, "config": self.store.get_graph_config()},
            headers=self._no_store_headers(),
        )

    async def _graph_provider_is_ready(self) -> bool:
        if self.codex is None:
            return False
        try:
            status = await self.codex.status()
        except CodexAppServerErrorTypes:
            return False
        return status.get("authenticated") is True

    @classmethod
    def _graph_provider_not_ready(cls) -> JSONResponse:
        return JSONResponse(
            {
                "ok": False,
                "error": "Configure and sign in to Codex before enabling Graph.",
                "code": "graph_provider_not_ready",
            },
            status_code=409,
            headers=cls._no_store_headers(),
        )

    async def api_settings(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        return JSONResponse(
            {"ok": True, "settings": await asyncio.to_thread(self._settings_payload)},
            headers=self._no_store_headers(),
        )

    async def api_update_general_settings(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        model = str(payload.get("rename_model", "")).strip() or AI_RENAME_DEFAULT_MODEL
        if len(model) > 96:
            return self._json_error("rename_model must be 96 characters or fewer.", status_code=400)
        self.store.set_app_setting(RENAME_MODEL_SETTING, model)
        self.store.set_app_setting(AI_RENAME_MODEL_SETTING, model)
        return JSONResponse({"ok": True, "settings": await asyncio.to_thread(self._settings_payload)},
                            headers=self._no_store_headers())

    async def api_update_transcript_settings(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        try:
            chunk_max_chars, chunk_max_lines = validate_transcript_chunk_limits(
                payload.get("chunk_max_chars"),
                payload.get("chunk_max_lines"),
            )
        except ValueError as exc:
            return self._json_error(str(exc), status_code=400)
        self.store.set_app_setting(TRANSCRIPT_CHUNK_MAX_CHARS_SETTING, str(chunk_max_chars))
        self.store.set_app_setting(TRANSCRIPT_CHUNK_MAX_LINES_SETTING, str(chunk_max_lines))
        return JSONResponse(
            {"ok": True, "settings": await asyncio.to_thread(self._settings_payload)},
            headers=self._no_store_headers(),
        )

    async def api_update_tool_output_settings(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        try:
            mode = validate_tool_output_mode(payload.get("mode"))
        except ValueError as exc:
            return self._json_error(str(exc), status_code=400)
        if not self.store.set_tool_output_mode_if_restart_not_pending(mode):
            return self._json_error(
                "A Bridge restart is already in progress. Wait for it to finish before changing the mode.",
                status_code=409,
            )
        return JSONResponse(
            {"ok": True, "tool_output": self._tool_output_settings_payload()},
            headers=self._no_store_headers(),
        )

    async def api_service_status(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        return JSONResponse(
            {"ok": True, "tool_output": self._tool_output_settings_payload()},
            headers=self._no_store_headers(),
        )

    async def api_restart_service(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        reservation, _ = self.store.reserve_tool_output_restart(self.active_tool_output_mode)
        if reservation == "pending":
            return self._json_error("A Bridge restart is already in progress.", status_code=409)
        if reservation == "not_required":
            return self._json_error("No pending tool output change requires a restart.", status_code=409)
        if self.restart_requester is None:
            self.store.clear_tool_output_restart_pending()
            return self._json_error("Service restart is unavailable on this deployment.", status_code=503)
        try:
            await self.restart_requester()
        except (OSError, RuntimeError) as exc:
            self.store.clear_tool_output_restart_pending()
            return self._json_error(f"Could not request service restart: {exc}", status_code=503)
        return JSONResponse(
            {
                "ok": True,
                "restart_requested": True,
                "tool_output": self._tool_output_settings_payload(),
                "message": (
                    "Bridge restart requested. After it returns, refresh the tool list or reconnect "
                    "the connector in every harness."
                ),
            },
            status_code=202,
            headers=self._no_store_headers(),
        )

    async def api_update_search_settings(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        try:
            config = self._validated_search_config(payload)
            previous = self.search.get_config()
            index = self.search.index_status(sync=False)
            build_sensitive_change = (
                config.enabled != previous.enabled
                or config.index_signature() != previous.index_signature()
            )
            if index["status"] in {"queued", "building"} and build_sensitive_change:
                return self._json_error(
                    "Stop the active vector build before changing RAG sources or group consent.",
                    status_code=409,
                )
            self.search.set_config(config)
            index = await asyncio.to_thread(
                self.search.maybe_start_rebuild, self._read_provider_key("openai")
            )
        except (TypeError, ValueError) as exc:
            return self._json_error(str(exc), status_code=400)
        return JSONResponse(
            {
                "ok": True,
                "settings": await asyncio.to_thread(self._settings_payload),
                "index": index,
            },
            headers=self._no_store_headers(),
        )

    async def api_update_provider_settings(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        for provider, setting_key in (("openai", OPENAI_KEY_SETTING), ("cohere", COHERE_KEY_SETTING)):
            if provider not in payload:
                continue
            value = str(payload.get(provider, "")).strip()
            if not value:
                return self._json_error(f"{provider} key must not be empty.", status_code=400)
            self.store.set_app_setting(setting_key, self._encrypt_secret(value))
            if provider == "openai":
                self.store.set_app_setting(AI_RENAME_API_KEY_SETTING, self._encrypt_secret(value))
        return JSONResponse({"ok": True, "settings": await asyncio.to_thread(self._settings_payload)},
                            headers=self._no_store_headers())

    async def api_delete_provider_key(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        provider = request.path_params.get("provider", "")
        keys = {"openai": OPENAI_KEY_SETTING, "cohere": COHERE_KEY_SETTING}
        if provider not in keys:
            return self._json_error("Unknown provider.", status_code=404)
        self.store.delete_app_setting(keys[provider])
        if provider == "openai":
            self.store.delete_app_setting(AI_RENAME_API_KEY_SETTING)
            self.search.cancel_rebuild()
        return JSONResponse({"ok": True, "settings": await asyncio.to_thread(self._settings_payload)},
                            headers=self._no_store_headers())

    async def api_search(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        query = str(payload.get("query", "")).strip()
        mode = str(payload.get("mode", "basic")).strip().lower()
        if not query:
            return self._json_error("query must not be empty.", status_code=400)
        if len(query) > 1000:
            return self._json_error("query must be 1000 characters or fewer.", status_code=400)
        config = self.search.get_config()
        try:
            if mode == "basic":
                results = await asyncio.to_thread(
                    self.search.basic_search, query, limit=config.result_limit,
                    source_kinds=config.source_kinds(),
                )
                data = {"results": results, "local_only_results": [], "warning": None}
            elif mode == "hybrid":
                data = await asyncio.to_thread(
                    self.search.hybrid_search, query,
                    openai_api_key=self._read_provider_key("openai"),
                    cohere_api_key=self._read_provider_key("cohere"),
                    config=config,
                )
            else:
                return self._json_error("mode must be basic or hybrid.", status_code=400)
        except ValueError as exc:
            return self._json_error(str(exc), status_code=400)
        except Exception as exc:
            return self._json_error(f"Search failed: {exc}", status_code=502)
        group_map = {item["group_id"]: item for item in self.store.list_session_groups()}
        for lane in ("results", "local_only_results"):
            for result in data[lane]:
                result["group"] = group_map.get(result["group_id"])
        return JSONResponse({"ok": True, "mode": mode, **data}, headers=self._no_store_headers())

    async def api_search_index(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        index = await asyncio.to_thread(
            self.search.maybe_start_rebuild, self._read_provider_key("openai")
        )
        return JSONResponse({"ok": True, "index": index}, headers=self._no_store_headers())

    async def api_search_index_estimate(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        try:
            config = self._validated_search_config(payload)
            estimate = await asyncio.to_thread(self.search.estimate_index, config)
        except (TypeError, ValueError) as exc:
            return self._json_error(str(exc), status_code=400)
        return JSONResponse(
            {"ok": True, "estimate": estimate},
            headers=self._no_store_headers(),
        )

    async def api_rebuild_search_index(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        try:
            index = self.search.start_rebuild(
                self._read_provider_key("openai"), self.search.get_config()
            )
        except (ValueError, RuntimeError) as exc:
            return self._json_error(str(exc), status_code=400)
        return JSONResponse({"ok": True, "index": index}, status_code=202,
                            headers=self._no_store_headers())

    async def api_cancel_search_index(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        return JSONResponse({"ok": True, "index": self.search.cancel_rebuild()},
                            headers=self._no_store_headers())

    async def api_delete_search_index(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        status = self.search.index_status(sync=False)
        if status["status"] in {"queued", "building"}:
            return self._json_error("Stop the active build before deleting the index.", status_code=409)
        return JSONResponse({"ok": True, "index": self.search.delete_vector_index()},
                            headers=self._no_store_headers())

    async def api_update_ai_settings(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error

        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error

        if "api_key" in payload:
            api_key = str(payload.get("api_key", "")).strip()
            if not api_key:
                return self._json_error("api_key must not be empty.", status_code=400)
            encrypted = self._encrypt_secret(api_key)
            self.store.set_app_setting(AI_RENAME_API_KEY_SETTING, encrypted)
            self.store.set_app_setting(OPENAI_KEY_SETTING, encrypted)

        if "model" in payload:
            model = str(payload.get("model", "")).strip() or AI_RENAME_DEFAULT_MODEL
            if len(model) > 96:
                return self._json_error("model must be 96 characters or fewer.", status_code=400)
            self.store.set_app_setting(AI_RENAME_MODEL_SETTING, model)
            self.store.set_app_setting(RENAME_MODEL_SETTING, model)

        return JSONResponse(
            {"ok": True, "settings": self._ai_settings_payload()},
            headers=self._no_store_headers(),
        )

    async def api_delete_ai_key(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        self.store.delete_app_setting(AI_RENAME_API_KEY_SETTING)
        self.store.delete_app_setting(OPENAI_KEY_SETTING)
        self.search.cancel_rebuild()
        return JSONResponse(
            {"ok": True, "settings": self._ai_settings_payload()},
            headers=self._no_store_headers(),
        )

    async def api_sessions(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        return JSONResponse(
            {
                "ok": True,
                "sessions": self.store.list_sessions(),
            },
            headers=self._no_store_headers(),
        )

    async def api_session_groups(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        return JSONResponse(
            {"ok": True, "groups": self.store.list_session_groups()},
            headers=self._no_store_headers(),
        )

    async def api_create_session_group(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        try:
            group = self.store.create_session_group(
                name=str(payload.get("name", "")),
                color=str(payload.get("color", "")),
                icon_key=str(payload.get("icon_key", "")),
                group_id=str(payload.get("group_id", "")),
            )
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {"ok": True, "group": _session_group_payload(group)},
            headers=self._no_store_headers(),
        )

    async def api_update_session_group(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        fields: dict[str, Any] = {}
        for key in ("name", "color", "icon_key"):
            if key in payload:
                fields[key] = str(payload[key])
        if "is_sensitive" in payload:
            if not isinstance(payload["is_sensitive"], bool):
                return self._json_error("is_sensitive must be a boolean.", status_code=400)
            fields["is_sensitive"] = payload["is_sensitive"]
        if not fields:
            return self._json_error("No editable session group fields provided.", status_code=400)
        try:
            group_id = request.path_params["group_id"]
            with self.search.external_scope_mutation():
                current = self.store.get_session_group(group_id)
                if current is None:
                    raise ValueError(f"Unknown session group: {group_id}")
                if current.is_system:
                    raise ValueError("System session groups cannot be edited")
                enabling_sensitive = fields.get("is_sensitive") is True and not current.is_sensitive
                config = self.search.get_config()
                if enabling_sensitive and group_id in config.included_group_ids:
                    index = self.search.index_status(sync=False)
                    if index["status"] in {"queued", "building"}:
                        return self._json_error(
                            "Stop the active vector build before marking this group sensitive.",
                            status_code=409,
                        )
                    config_payload = config.to_dict()
                    config_payload["included_group_ids"] = [
                        item for item in config.included_group_ids if item != group_id
                    ]
                    self.search.set_config(SearchConfig.from_dict(config_payload))
                group = self.store.update_session_group(group_id, **fields)
                external_config = self.search.effective_external_config(self.search.get_config())
            if enabling_sensitive and external_config.included_group_ids:
                await asyncio.to_thread(
                    self.search.maybe_start_rebuild, self._read_provider_key("openai")
                )
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {"ok": True, "group": _session_group_payload(group)},
            headers=self._no_store_headers(),
        )

    async def api_delete_session_group(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request, allow_empty=True)
        if parse_error:
            return parse_error
        destination_group_id = str(payload.get("destination_group_id", "")) if payload else ""
        try:
            group = self.store.delete_session_group(
                request.path_params["group_id"],
                destination_group_id=destination_group_id,
            )
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {"ok": True, "group": _session_group_payload(group)},
            headers=self._no_store_headers(),
        )

    async def api_session(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error

        session_id = request.path_params["session_id"]
        session = self.store.get_session(session_id)
        if session is None:
            return self._json_error(f"Unknown session_id: {session_id}", status_code=404)

        exchanges = self.store.list_exchanges(session.session_id, include_deleted=True)
        display_timezone = self._display_timezone_name()
        exchange_payloads = [
            _exchange_payload(exchange, timezone_name=display_timezone)
            for exchange in exchanges
        ]
        session_payload = _session_payload(session)
        session_payload["token_count"] = sum(exchange["total_token_count"] for exchange in exchange_payloads)
        files = self.store.list_session_files(
            session_id=session.session_id,
            group_id=session.group_id,
        )
        return JSONResponse(
            {
                "ok": True,
                "display_timezone": display_timezone,
                "session": session_payload,
                "files": {
                    "session": [file for file in files if file["scope_type"] == "session"],
                    "group": [file for file in files if file["scope_type"] == "group"],
                },
                "exchanges": exchange_payloads,
            },
            headers=self._no_store_headers(),
        )

    async def api_update_session(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        if "group_id" not in payload and "title" not in payload:
            return self._json_error("group_id or title is required.", status_code=400)
        try:
            session_id = request.path_params["session_id"]
            session = self.store.get_session(session_id)
            if session is None:
                raise ValueError(f"Unknown session_id: {session_id}")
            if "group_id" in payload:
                session = self.store.set_session_group(session.session_id, str(payload.get("group_id", "")))
            if "title" in payload:
                session = self.store.set_session_title(session.session_id, str(payload.get("title", "")))
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {"ok": True, "session": _session_payload(session)},
            headers=self._no_store_headers(),
        )

    async def api_ai_rename_session(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error

        api_key = self._read_ai_api_key()
        if not api_key:
            return self._json_error("AI rename API key is not configured.", status_code=400)

        session_id = request.path_params["session_id"]
        session = self.store.get_session(session_id)
        if session is None:
            return self._json_error(f"Unknown session_id: {session_id}", status_code=404)

        exchanges = self.store.list_exchanges(session.session_id)
        if not exchanges:
            return self._json_error("Session has no user message to rename from.", status_code=400)

        first_user_message = exchanges[0].user_message
        model = self.store.get_app_setting(AI_RENAME_MODEL_SETTING) or AI_RENAME_DEFAULT_MODEL
        try:
            title = await asyncio.to_thread(_suggest_session_title, api_key, model, first_user_message)
            session = self.store.set_session_title(session.session_id, title)
        except ValueError as exc:
            return self._value_error(exc)
        except RuntimeError as exc:
            return self._json_error(str(exc), status_code=502)

        return JSONResponse(
            {"ok": True, "session": _session_payload(session), "title": session.title},
            headers=self._no_store_headers(),
        )

    async def api_file(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        file_id, path_error = _path_file_id(request)
        if path_error:
            return path_error
        saved = self.store.get_session_file(file_id)
        if saved is None:
            return self._json_error(f"Unknown file_id: {file_id}", status_code=404)
        return JSONResponse(
            {"ok": True, "file": session_file_payload(saved, include_content=True)},
            headers=self._no_store_headers(),
        )

    async def api_file_raw(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        file_id, path_error = _path_file_id(request)
        if path_error:
            return path_error
        binary_record = await asyncio.to_thread(self.store.get_session_file_binary, file_id)
        if binary_record is None:
            return self._json_error(f"Unknown file_id: {file_id}", status_code=404)
        filename, content_kind, binary_content = binary_record
        if content_kind != "pdf" or binary_content is None:
            return self._json_error("Raw binary content is available only for PDF files.", status_code=400)
        disposition = "attachment" if request.query_params.get("download") == "1" else "inline"
        encoded_filename = urllib.parse.quote(filename, safe="")
        return Response(
            binary_content,
            media_type="application/pdf",
            headers={
                **self._no_store_headers(),
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_filename}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def pdfjs_asset(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        asset_name = request.path_params.get("asset_name", "")
        if asset_name not in {"pdf.min.mjs", "pdf.worker.min.mjs"}:
            return Response(status_code=404)
        asset_path = self.pdfjs_dir / asset_name
        return FileResponse(
            asset_path,
            media_type="text/javascript",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def brand_asset(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        asset_name = str(request.path_params.get("asset_path", "")).strip("/")
        relative_path = Path(asset_name)
        if (
            not asset_name
            or relative_path.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            return Response(status_code=404)
        media_type = BRAND_ASSET_MEDIA_TYPES.get(relative_path.suffix.lower())
        if media_type is None:
            return Response(status_code=404)
        try:
            brand_root = self.brand_dir.resolve()
            asset_path = (self.brand_dir / relative_path).resolve()
            if not asset_path.is_relative_to(brand_root) or not asset_path.is_file():
                return Response(status_code=404)
        except OSError:
            return Response(status_code=404)
        return FileResponse(
            asset_path,
            media_type=media_type,
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def api_upload_file(self, request: Request) -> Response:
        admin_session, error = self._require_admin_mutation(request)
        if error:
            return error
        selected, selection_error = self._selected_session(request)
        if selection_error:
            return selection_error

        payload, parse_error = await _bounded_json_body(
            request,
            max_bytes=ADMIN_FILE_UPLOAD_MAX_BODY_BYTES,
        )
        if parse_error:
            return parse_error
        allowed_keys = {"scope_type", "filename", "content_base64"}
        if set(payload) != allowed_keys:
            return self._json_error(
                "Upload requires exactly scope_type, filename, and content_base64.",
                status_code=400,
            )

        scope_type = payload.get("scope_type")
        filename = payload.get("filename")
        encoded = payload.get("content_base64")
        if scope_type not in {"session", "group"}:
            return self._json_error("scope_type must be session or group.", status_code=400)
        if not isinstance(filename, str):
            return self._json_error("filename must be a string.", status_code=400)
        mime_type = _admin_upload_mime_type(filename)
        if mime_type is None:
            return self._json_error("Unsupported file extension.", status_code=400)
        if not isinstance(encoded, str):
            return self._json_error("content_base64 must be a string.", status_code=400)
        try:
            if mime_type == "application/pdf":
                saved = await run_pdf_worker(
                    self._ingest_admin_pdf,
                    selected.session_id,
                    scope_type,
                    filename,
                    encoded,
                    admin_session["username"],
                )
            else:
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    return self._json_error("content_base64 must be valid base64.", status_code=400)
                if len(raw) > MAX_SESSION_FILE_BYTES:
                    return self._json_error(
                        f"File content must be at most {MAX_SESSION_FILE_BYTES} UTF-8 bytes.",
                        status_code=400,
                    )
                try:
                    content = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    return self._json_error("File content must be valid UTF-8.", status_code=400)
                if scope_type == "session":
                    saved = self.store.save_session_file(
                        selected.session_id,
                        filename,
                        content,
                        mime_type=mime_type,
                        created_by=admin_session["username"],
                    )
                else:
                    saved = self.store.save_group_file_for_session(
                        selected.session_id,
                        filename,
                        content,
                        mime_type=mime_type,
                        created_by=admin_session["username"],
                    )
        except PdfWorkerBusyError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=503,
                headers={**self._no_store_headers(), "Retry-After": "2"},
            )
        except PdfStorageQuotaError as exc:
            return self._json_error(str(exc), status_code=507)
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {"ok": True, "file": session_file_payload(saved)},
            headers=self._no_store_headers(),
        )

    def _ingest_admin_pdf(
        self,
        session_id: str,
        scope_type: str,
        filename: str,
        encoded: str,
        created_by: str,
    ) -> SessionFileRecord:
        raw = decode_pdf_base64(encoded, max_bytes=MAX_ADMIN_PDF_BYTES)
        extraction = extract_pdf_text_isolated(raw)
        pdf_kwargs = {
            "extracted_text": extraction.content,
            "page_count": extraction.page_count,
            "extraction_status": extraction.extraction_status,
            "extracted_text_bytes": extraction.extracted_text_bytes,
            "created_by": created_by,
        }
        if scope_type == "session":
            return self.store.save_session_pdf(
                session_id,
                filename,
                raw,
                **pdf_kwargs,
            )
        return self.store.save_group_pdf_for_session(
            session_id,
            filename,
            raw,
            **pdf_kwargs,
        )

    async def api_mutate_file(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        selected, selection_error = self._selected_session(request)
        if selection_error:
            return selection_error
        file_id, path_error = _path_file_id(request)
        if path_error:
            return path_error
        saved = self.store.get_session_file(file_id)
        if saved is None or not _file_visible_to_session(saved, selected):
            return self._json_error(f"Unknown file_id: {file_id}", status_code=404)

        payload, parse_error = await _bounded_json_body(
            request,
            max_bytes=ADMIN_FILE_EDIT_MAX_BODY_BYTES,
        )
        if parse_error:
            return parse_error

        edit_keys = {"content", "expected_sha256"}
        move_keys = {"scope_type"}
        try:
            if set(payload) == edit_keys:
                if not isinstance(payload["content"], str):
                    return self._json_error("content must be a string.", status_code=400)
                if not isinstance(payload["expected_sha256"], str):
                    return self._json_error("expected_sha256 must be a string.", status_code=400)
                updated = self.store.update_session_file(
                    file_id,
                    payload["content"],
                    expected_sha256=payload["expected_sha256"],
                    visible_session_id=selected.session_id,
                    visible_group_id=selected.group_id,
                )
            elif set(payload) == move_keys:
                scope_type = payload["scope_type"]
                if scope_type == "session":
                    updated = self.store.move_session_file(
                        file_id,
                        scope_type="session",
                        session_id=selected.session_id,
                        visible_session_id=selected.session_id,
                        visible_group_id=selected.group_id,
                    )
                elif scope_type == "group":
                    updated = self.store.move_session_file(
                        file_id,
                        scope_type="group",
                        group_id=selected.group_id,
                        visible_session_id=selected.session_id,
                        visible_group_id=selected.group_id,
                    )
                else:
                    return self._json_error("scope_type must be session or group.", status_code=400)
            else:
                return self._json_error(
                    "Provide exactly one edit (content and expected_sha256) or one move (scope_type).",
                    status_code=400,
                )
        except SessionFileConflictError as exc:
            return self._json_error(str(exc), status_code=409)
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {"ok": True, "file": session_file_payload(updated)},
            headers=self._no_store_headers(),
        )

    async def api_delete_file(self, request: Request) -> Response:
        _, error = self._require_admin_mutation(request)
        if error:
            return error
        selected, selection_error = self._selected_session(request)
        if selection_error:
            return selection_error
        file_id, path_error = _path_file_id(request)
        if path_error:
            return path_error
        saved = self.store.get_session_file(file_id)
        if saved is None or not _file_visible_to_session(saved, selected):
            return self._json_error(f"Unknown file_id: {file_id}", status_code=404)
        try:
            deleted = self.store.delete_session_file(
                file_id,
                visible_session_id=selected.session_id,
                visible_group_id=selected.group_id,
            )
        except SessionFileConflictError as exc:
            return self._json_error(str(exc), status_code=409)
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {"ok": True, "file": session_file_payload(deleted)},
            headers=self._no_store_headers(),
        )

    def _selected_session(
        self,
        request: Request,
    ) -> tuple[SessionRecord | None, JSONResponse | None]:
        session_id = request.path_params.get("session_id", "")
        selected = self.store.get_session(session_id)
        if selected is None:
            return None, self._json_error(f"Unknown session_id: {session_id}", status_code=404)
        return selected, None

    async def api_update_exchange(self, request: Request) -> Response:
        session, error = self._require_admin_mutation(request)
        if error:
            return error
        exchange_id, error_response = _path_exchange_id(request)
        if error_response:
            return error_response

        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error

        fields = {
            key: payload[key]
            for key in ("model_name", "user_message", "assistant_response")
            if key in payload
        }
        if not fields:
            return self._json_error("No editable fields provided.", status_code=400)

        try:
            exchange = self.store.update_exchange(exchange_id, actor=session["username"], **fields)
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {"ok": True, "exchange": _exchange_payload(exchange, timezone_name=self._display_timezone_name())},
            headers=self._no_store_headers(),
        )

    async def api_delete_exchange(self, request: Request) -> Response:
        session, error = self._require_admin_mutation(request)
        if error:
            return error
        exchange_id, error_response = _path_exchange_id(request)
        if error_response:
            return error_response

        payload, parse_error = await _json_body(request, allow_empty=True)
        if parse_error:
            return parse_error
        reason = str(payload.get("reason", "")) if payload else ""

        try:
            exchange = self.store.delete_exchange(exchange_id, reason=reason, actor=session["username"])
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {"ok": True, "exchange": _exchange_payload(exchange, timezone_name=self._display_timezone_name())},
            headers=self._no_store_headers(),
        )

    async def api_restore_exchange(self, request: Request) -> Response:
        session, error = self._require_admin_mutation(request)
        if error:
            return error
        exchange_id, error_response = _path_exchange_id(request)
        if error_response:
            return error_response

        try:
            exchange = self.store.restore_exchange(exchange_id, actor=session["username"])
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {"ok": True, "exchange": _exchange_payload(exchange, timezone_name=self._display_timezone_name())},
            headers=self._no_store_headers(),
        )

    async def api_mask_exchange_response(self, request: Request) -> Response:
        return await self._api_set_exchange_response_masked(request, masked=True)

    async def api_unmask_exchange_response(self, request: Request) -> Response:
        return await self._api_set_exchange_response_masked(request, masked=False)

    async def _api_set_exchange_response_masked(self, request: Request, *, masked: bool) -> Response:
        session, error = self._require_admin_mutation(request)
        if error:
            return error
        exchange_id, error_response = _path_exchange_id(request)
        if error_response:
            return error_response

        try:
            if masked:
                exchange = self.store.mask_exchange_response(exchange_id, actor=session["username"])
            else:
                exchange = self.store.unmask_exchange_response(exchange_id, actor=session["username"])
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "exchange": _exchange_payload(
                    exchange,
                    timezone_name=self._display_timezone_name(),
                ),
            },
            headers=self._no_store_headers(),
        )

    def _require_admin(self, request: Request) -> tuple[dict[str, Any], Response | None]:
        session = self._read_cookie(request)
        if session:
            return session, None
        if request.url.path.startswith("/admin/api/"):
            return {}, self._json_error("Admin login required.", status_code=401)
        next_path = _safe_next(request.url.path)
        return {}, RedirectResponse(f"/admin/login?next={next_path}", status_code=303)

    def _require_admin_mutation(self, request: Request) -> tuple[dict[str, Any], Response | None]:
        session, error = self._require_admin(request)
        if error:
            return session, error
        if request.headers.get("x-csrf-token") != session["csrf"]:
            return session, self._json_error("Invalid CSRF token.", status_code=403)
        return session, None

    def _make_cookie(self, username: str) -> str:
        payload = {
            "username": username,
            "csrf": token_urlsafe(24),
            "exp": int(time.time()) + ADMIN_SESSION_SECONDS,
        }
        data = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        signature = self._signature(data)
        return f"{data}.{signature}"

    def _read_cookie(self, request: Request) -> dict[str, Any] | None:
        cookie = request.cookies.get(ADMIN_COOKIE)
        if not cookie or "." not in cookie:
            return None
        data, signature = cookie.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._signature(data)):
            return None
        try:
            payload = json.loads(_b64decode(data).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) < time.time():
            return None
        if not isinstance(payload.get("username"), str) or not isinstance(payload.get("csrf"), str):
            return None
        return payload

    def _signature(self, data: str) -> str:
        return hmac.new(self.settings.secret_key.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()

    def _display_timezone_name(self) -> str:
        try:
            return resolve_timezone_name(self.store.get_app_setting(DISPLAY_TIMEZONE_SETTING_KEY))
        except ValueError:
            self.store.set_app_setting(DISPLAY_TIMEZONE_SETTING_KEY, DEFAULT_DISPLAY_TIMEZONE_NAME)
            return DEFAULT_DISPLAY_TIMEZONE_NAME

    def _ai_settings_payload(self) -> dict[str, Any]:
        api_key = self._read_ai_api_key()
        model = self.store.get_app_setting(AI_RENAME_MODEL_SETTING) or AI_RENAME_DEFAULT_MODEL
        return {
            "configured": bool(api_key),
            "api_key_preview": _secret_preview(api_key) if api_key else "",
            "model": model,
            "title_max_chars": SESSION_TITLE_MAX_CHARS,
        }
    def _read_provider_key(self, provider: str) -> str:
        setting_key = OPENAI_KEY_SETTING if provider == "openai" else COHERE_KEY_SETTING
        encrypted = self.store.get_app_setting(setting_key)
        if not encrypted and provider == "openai":
            encrypted = self.store.get_app_setting(AI_RENAME_API_KEY_SETTING)
        if not encrypted:
            return ""
        try:
            return self._decrypt_secret(encrypted)
        except (InvalidToken, ValueError):
            return ""

    def maybe_schedule_search_rebuild(self) -> dict[str, Any] | None:
        """Refresh source state and queue a due vector rebuild without blocking callers."""
        config = self.search.get_config()
        api_key = self._read_provider_key("openai")
        if not config.enabled or not api_key:
            return None
        return self.search.maybe_start_rebuild(api_key)

    def _operational_status_payload(self) -> dict[str, Any]:
        cached: dict[str, Any] = {}
        path = self.settings.operational_status_file
        if path is not None:
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(candidate, dict):
                    cached = candidate
            except (OSError, json.JSONDecodeError):
                cached = {}

        update = _public_keys(
            cached.get("update"),
            {"state", "current", "latest", "last_checked", "release_url", "error"},
        )
        if update.get("state") not in {"current", "available", "unknown"}:
            update["state"] = "unknown"
        update.setdefault("current", BRIDGE_VERSION)

        checks = []
        raw_checks = cached.get("checks")
        if isinstance(raw_checks, list):
            for check in raw_checks:
                public = _public_keys(
                    check,
                    {"id", "label", "state", "message", "remediation"},
                )
                if public:
                    checks.append(public)

        version = _public_keys(cached.get("version"), {"current", "database_schema"})
        version.setdefault("current", BRIDGE_VERSION)
        version["database_schema"] = self.store.schema_version()
        installation = _public_keys(
            cached.get("installation"),
            {"mode", "public_base_url", "resource_path", "installed_at"},
        )
        installation.setdefault("mode", "managed" if path is not None else "unmanaged")
        installation.setdefault("public_base_url", self.settings.public_base_url)
        installation.setdefault("resource_path", self.settings.resource_path)

        refreshed_at = None
        if path is not None:
            try:
                refreshed_at = int(path.stat().st_mtime)
            except OSError:
                pass
        overall = cached.get("overall", "unknown")
        if overall not in {"healthy", "attention", "failed", "incomplete", "unknown"}:
            overall = "unknown"
        return {
            "schema_version": 1,
            "overall": overall,
            "version": version,
            "update": update,
            "checks": checks,
            "installation": installation,
            "last_operation": _public_keys(
                cached.get("last_operation"),
                {"operation", "state", "previous_version", "version", "finished_at"},
            ) or None,
            "refreshed_at": refreshed_at,
            "live": {"application": "pass", "database": "pass"},
        }

    def _settings_payload(self) -> dict[str, Any]:
        openai_key = self._read_provider_key("openai")
        cohere_key = self._read_provider_key("cohere")
        rename_model = (
            self.store.get_app_setting(RENAME_MODEL_SETTING)
            or self.store.get_app_setting(AI_RENAME_MODEL_SETTING)
            or AI_RENAME_DEFAULT_MODEL
        )
        chunk_max_chars, chunk_max_lines = transcript_chunk_limits(
            self.store,
            self.settings.transcript_chunk_max_chars,
            self.settings.transcript_chunk_max_lines,
        )
        output_probe_runs = self.store.list_output_probe_runs(limit=50)
        tool_output = self._tool_output_settings_payload()
        return {
            "general": {"rename_model": rename_model},
            "api": {
                "openai": {"configured": bool(openai_key), "preview": _secret_preview(openai_key) if openai_key else ""},
                "cohere": {"configured": bool(cohere_key), "preview": _secret_preview(cohere_key) if cohere_key else ""},
            },
            "search": self.search.get_config().to_dict(),
            "groups": self.store.list_session_groups(),
            "index": self.search.index_status(),
            "tool_output": tool_output,
            "transcript": {
                "chunk_max_chars": chunk_max_chars,
                "chunk_max_lines": chunk_max_lines,
                "default_chunk_max_chars": self.settings.transcript_chunk_max_chars,
                "default_chunk_max_lines": self.settings.transcript_chunk_max_lines,
                "min_chunk_max_chars": MIN_TRANSCRIPT_CHUNK_CHARS,
                "max_chunk_max_chars": MAX_TRANSCRIPT_CHUNK_CHARS,
                "min_chunk_max_lines": MIN_TRANSCRIPT_CHUNK_LINES,
                "max_chunk_max_lines": MAX_TRANSCRIPT_CHUNK_LINES,
                "min_probe_target_chars": MIN_PROBE_TARGET_CHARS,
                "max_probe_target_chars": MAX_PROBE_TARGET_CHARS,
                "safety_margin_percent": round(PROBE_SAFETY_MARGIN * 100),
                "runs": [public_probe_run(run) for run in output_probe_runs],
                "recommendations": probe_recommendations(
                    output_probe_runs,
                    tool_output_mode=self.active_tool_output_mode,
                ),
            },
        }

    def _tool_output_settings_payload(self) -> dict[str, Any]:
        configured_mode = configured_tool_output_mode(self.store)
        pending_mode = self.store.get_app_setting(TOOL_OUTPUT_RESTART_PENDING_SETTING)
        return {
            "active_mode": self.active_tool_output_mode,
            "configured_mode": configured_mode,
            "restart_required": configured_mode != self.active_tool_output_mode,
            "restart_pending": pending_mode is not None,
            "pending_mode": pending_mode,
        }

    def _read_ai_api_key(self) -> str:
        return self._read_provider_key("openai")

    def _encrypt_secret(self, value: str) -> str:
        return self._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt_secret(self, value: str) -> str:
        return self._fernet().decrypt(value.encode("ascii")).decode("utf-8")

    def _fernet(self) -> Fernet:
        key = base64.urlsafe_b64encode(hashlib.sha256(self.settings.secret_key.encode("utf-8")).digest())
        return Fernet(key)

    def _login_form(self, next_path: str, error: str | None = None, status_code: int = 200) -> HTMLResponse:
        error_html = (
            '<div class="login-alert" role="alert">'
            '<span class="alert-dot" aria-hidden="true"></span>'
            f'<span>{html.escape(error)}</span>'
            '</div>'
            if error
            else ""
        )
        escaped_next = html.escape(next_path, quote=True)
        try:
            brand_svg = (self.brand_dir / "svg" / "lockup-horizontal-dark.svg").read_text(encoding="utf-8")
        except OSError:
            brand_svg = '<span class="fallback-wordmark"><span>MCP</span> Session Bridge</span>'
        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MCP Session Bridge Admin</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {{
      color-scheme: dark;
      --bg-base: #0a0b0f;
      --panel: #15161d;
      --text: #ecedf2;
      --muted: #8e93a6;
      --faint: #6e7387;
      --stroke: rgba(255, 255, 255, .1);
      --stroke-soft: rgba(255, 255, 255, .065);
      --accent: #6c5cf2;
      --accent-hover: #7c70f5;
      --accent-text: #b3a9ff;
      --accent-soft: rgba(108, 92, 242, .16);
      --danger: #fb7185;
      --danger-soft: rgba(251, 113, 133, .12);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ min-height: 100%; margin: 0; }}
    body {{
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 1.5rem;
      overflow-x: hidden;
      background:
        radial-gradient(58rem 38rem at 8% -8%, rgba(108, 92, 242, .2), transparent 66%),
        radial-gradient(42rem 30rem at 105% 108%, rgba(74, 66, 196, .16), transparent 68%),
        var(--bg-base);
      color: var(--text);
      font-family: Manrope, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
      -webkit-font-smoothing: antialiased;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: .26;
      background-image: linear-gradient(rgba(255, 255, 255, .025) 1px, transparent 1px), linear-gradient(90deg, rgba(255, 255, 255, .025) 1px, transparent 1px);
      background-size: 48px 48px;
      mask-image: linear-gradient(to bottom, black, transparent 78%);
    }}
    button, input {{ font: inherit; color: inherit; }}
    button:focus-visible, input:focus-visible {{ outline: 2px solid var(--accent-hover); outline-offset: 3px; }}
    .login-shell {{
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: minmax(0, 1.08fr) minmax(20rem, .92fr);
      width: min(100%, 68rem);
      min-height: min(36rem, calc(100vh - 3rem));
      overflow: hidden;
      border: 1px solid var(--stroke);
      border-radius: 22px;
      background: rgba(21, 22, 29, .86);
      box-shadow: 0 32px 90px rgba(0, 0, 0, .55), 0 0 0 1px rgba(108, 92, 242, .035);
      backdrop-filter: blur(20px);
    }}
    .login-visual {{
      position: relative;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-width: 0;
      padding: clamp(2.2rem, 5vw, 4.5rem);
      overflow: hidden;
      border-right: 1px solid var(--stroke-soft);
      background:
        radial-gradient(28rem 22rem at 10% 6%, rgba(108, 92, 242, .17), transparent 72%),
        linear-gradient(145deg, rgba(255, 255, 255, .035), transparent 58%);
    }}
    .login-visual::after {{
      content: "";
      position: absolute;
      right: -9rem;
      bottom: -12rem;
      width: 26rem;
      height: 26rem;
      border: 1px solid rgba(179, 169, 255, .12);
      border-radius: 50%;
      box-shadow: 0 0 0 3.2rem rgba(179, 169, 255, .025), 0 0 0 6.4rem rgba(179, 169, 255, .018);
      pointer-events: none;
    }}
    .login-logo {{ position: relative; z-index: 1; width: min(100%, 27rem); margin-bottom: clamp(4rem, 11vh, 8rem); }}
    .login-logo svg {{ display: block; width: 100%; height: auto; }}
    .fallback-wordmark {{ display: block; color: var(--text); font-size: 1.5rem; font-weight: 700; letter-spacing: -.04em; }}
    .fallback-wordmark span {{ margin-right: .45rem; color: var(--accent-text); font-family: "Geist Mono", ui-monospace, monospace; font-size: .78em; letter-spacing: .12em; }}
    .eyebrow {{ margin: 0 0 1rem; color: var(--accent-text); font-family: "Geist Mono", ui-monospace, monospace; font-size: .68rem; font-weight: 500; letter-spacing: .18em; text-transform: uppercase; }}
    h1, h2 {{ letter-spacing: -.055em; }}
    h1 {{ max-width: 28rem; margin: 0 0 1rem; font-size: clamp(2.35rem, 5vw, 4rem); line-height: .98; }}
    .visual-copy {{ max-width: 30rem; margin: 0; color: var(--muted); font-size: 1rem; line-height: 1.75; }}
    .visual-bottom {{ position: relative; z-index: 1; display: flex; align-items: center; gap: .85rem; margin-top: 3rem; color: var(--faint); }}
    .signal-mark {{ display: grid; place-items: center; width: 2.5rem; height: 2.5rem; flex: 0 0 auto; border: 1px solid rgba(179, 169, 255, .3); border-radius: 10px; background: var(--accent); box-shadow: 0 10px 24px rgba(108, 92, 242, .23); }}
    .signal-mark::before {{ content: ""; width: 1.05rem; height: 1.05rem; border: 2px solid #ecedf2; border-radius: 50%; box-shadow: 0 0 0 4px rgba(201, 194, 255, .45); }}
    .mono {{ font-family: "Geist Mono", ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .visual-meta {{ display: grid; gap: .2rem; }}
    .visual-meta .mono {{ color: var(--accent-text); font-size: .65rem; letter-spacing: .13em; }}
    .visual-meta span:last-child {{ font-size: .74rem; }}
    .login-form-panel {{ display: grid; place-items: center; padding: clamp(2rem, 5vw, 4rem); background: rgba(10, 11, 15, .22); }}
    .login-form-wrap {{ width: min(100%, 22rem); }}
    .form-header {{ margin-bottom: 2rem; }}
    .form-header h2 {{ margin: 0 0 .65rem; font-size: clamp(1.8rem, 3vw, 2.25rem); line-height: 1; }}
    .form-header p:last-child {{ margin: 0; color: var(--muted); font-size: .9rem; line-height: 1.6; }}
    .login-alert {{ display: flex; align-items: flex-start; gap: .65rem; margin: 0 0 1.25rem; padding: .8rem .9rem; border: 1px solid rgba(251, 113, 133, .28); border-radius: 10px; background: var(--danger-soft); color: #fecdd3; font-size: .82rem; line-height: 1.45; }}
    .alert-dot {{ width: .5rem; height: .5rem; flex: 0 0 auto; margin-top: .32rem; border-radius: 50%; background: var(--danger); box-shadow: 0 0 0 4px rgba(251, 113, 133, .12); }}
    .login-form {{ display: grid; gap: 1rem; }}
    .field {{ display: grid; gap: .45rem; }}
    label {{ color: #d6d8e2; font-size: .78rem; font-weight: 600; letter-spacing: .01em; }}
    input {{ width: 100%; min-height: 3rem; padding: .75rem .85rem; border: 1px solid var(--stroke); border-radius: 10px; background: rgba(0, 0, 0, .28); transition: border-color .16s ease, box-shadow .16s ease, background .16s ease; }}
    input::placeholder {{ color: var(--faint); }}
    input:hover {{ border-color: rgba(255, 255, 255, .16); }}
    input:focus {{ outline: none; border-color: var(--accent); background: rgba(0, 0, 0, .38); box-shadow: 0 0 0 3px var(--accent-soft); }}
    .login-submit {{ display: flex; align-items: center; justify-content: space-between; width: 100%; min-height: 3.1rem; margin-top: .35rem; padding: .75rem .9rem .75rem 1rem; border: 1px solid rgba(179, 169, 255, .24); border-radius: 10px; background: var(--accent); color: #fff; font-size: .86rem; font-weight: 700; cursor: pointer; box-shadow: 0 12px 25px rgba(108, 92, 242, .2); transition: background .16s ease, transform .16s ease, box-shadow .16s ease; }}
    .login-submit:hover {{ background: var(--accent-hover); box-shadow: 0 15px 30px rgba(108, 92, 242, .3); transform: translateY(-1px); }}
    .login-submit:active {{ transform: translateY(0); }}
    .login-submit svg {{ width: 1rem; height: 1rem; }}
    .login-footer {{ display: flex; align-items: center; gap: .5rem; margin: 1.7rem 0 0; color: var(--faint); font-size: .7rem; line-height: 1.5; }}
    .status-dot {{ width: .42rem; height: .42rem; flex: 0 0 auto; border-radius: 50%; background: #34d399; box-shadow: 0 0 0 4px rgba(52, 211, 153, .1); }}
    @media (max-width: 760px) {{
      body {{ padding: .8rem; }}
      .login-shell {{ grid-template-columns: 1fr; min-height: auto; border-radius: 17px; }}
      .login-visual {{ min-height: 23rem; padding: 2.1rem; border-right: 0; border-bottom: 1px solid var(--stroke-soft); }}
      .login-logo {{ width: min(100%, 23rem); margin-bottom: 4rem; }}
      h1 {{ font-size: clamp(2.25rem, 12vw, 3.2rem); }}
      .login-form-panel {{ padding: 2.1rem; }}
    }}
    @media (max-width: 390px) {{
      .login-visual, .login-form-panel {{ padding: 1.5rem; }}
      .login-visual {{ min-height: 21rem; }}
    }}
  </style>
</head>
<body>
  <main class="login-shell">
    <section class="login-visual" aria-label="MCP Session Bridge">
      <div>
        <div class="login-logo">{brand_svg}</div>
        <p class="eyebrow">Private workspace</p>
        <h1>Keep the bridge in view.</h1>
        <p class="visual-copy">A focused space for conversations, context, and the work around them.</p>
      </div>
      <div class="visual-bottom">
        <span class="signal-mark" aria-hidden="true"></span>
        <span class="visual-meta"><span class="mono">ADMIN / ACCESS</span><span>Protected session workspace</span></span>
      </div>
    </section>
    <section class="login-form-panel" aria-labelledby="login-title">
      <div class="login-form-wrap">
        <div class="form-header">
          <p class="eyebrow">Secure access</p>
          <h2 id="login-title">Welcome back</h2>
          <p>Sign in to continue to your admin workspace.</p>
        </div>
        {error_html}
        <form class="login-form" method="post" action="/admin/login">
          <input type="hidden" name="next" value="{escaped_next}">
          <div class="field">
            <label for="username">Username</label>
            <input id="username" name="username" autocomplete="username" placeholder="Enter your username" required>
          </div>
          <div class="field">
            <label for="password">Password</label>
            <input id="password" name="password" type="password" autocomplete="current-password" placeholder="Enter your password" required>
          </div>
          <button class="login-submit" type="submit"><span>Log in</span><svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 8h9m-4-4 4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
        </form>
        <p class="login-footer"><span class="status-dot" aria-hidden="true"></span><span>Your session is private and protected.</span></p>
      </div>
    </section>
  </main>
</body>
</html>"""
        return HTMLResponse(body, status_code=status_code, headers=self._admin_headers())

    def _value_error(self, exc: ValueError) -> JSONResponse:
        message = str(exc)
        status_code = 404 if message.startswith("Unknown ") else 400
        return self._json_error(message, status_code=status_code)

    def _validated_search_config(self, payload: dict[str, Any]) -> SearchConfig:
        config = SearchConfig.from_dict(payload)
        groups = self.store.list_session_groups()
        known_groups = {item["group_id"] for item in groups}
        unknown = set(config.included_group_ids) - known_groups
        if unknown:
            raise ValueError(f"Unknown group ids: {', '.join(sorted(unknown))}")
        sensitive = {
            item["group_id"] for item in groups if item.get("is_sensitive")
        } & set(config.included_group_ids)
        if sensitive:
            raise ValueError(
                f"Sensitive groups cannot be enabled for external processing: {', '.join(sorted(sensitive))}"
            )
        return config

    @staticmethod
    def _json_error(message: str, status_code: int) -> JSONResponse:
        return JSONResponse({"ok": False, "error": message}, status_code=status_code, headers=AdminHandlers._no_store_headers())

    @classmethod
    def _codex_unavailable(cls) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": "Codex App Server is unavailable."},
            status_code=503,
            headers={**cls._no_store_headers(), "Retry-After": "2"},
        )

    @classmethod
    def _codex_error(cls, error: Exception) -> JSONResponse:
        if isinstance(error, CodexUnavailableError):
            return cls._codex_unavailable()
        if isinstance(error, CodexTimeoutError):
            return cls._json_error("Codex App Server request timed out.", status_code=504)
        return cls._json_error("Codex App Server request failed.", status_code=502)

    @staticmethod
    def _admin_headers() -> dict[str, str]:
        return {
            **AdminHandlers._no_store_headers(),
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin",
        }

    @staticmethod
    def _no_store_headers() -> dict[str, str]:
        return {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _session_payload(session: SessionRecord) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "title": session.title,
        "group_id": session.group_id,
        "context_pack_id": session.context_pack_id,
        "context_pack_version": session.context_pack_version,
        "title_is_auto": session.title_is_auto,
        "created_at": session.created_at,
        "created_at_iso": format_timestamp_iso(session.created_at),
        "updated_at": session.updated_at,
        "updated_at_iso": format_timestamp_iso(session.updated_at),
    }


def _public_keys(value: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in allowed if key in value}


def _session_group_payload(group: SessionGroupRecord) -> dict[str, Any]:
    return {
        "group_id": group.group_id,
        "name": group.name,
        "color": group.color,
        "icon_key": group.icon_key,
        "sort_order": group.sort_order,
        "is_system": group.is_system,
        "is_sensitive": group.is_sensitive,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
        "deleted_at": group.deleted_at,
    }


def _secret_preview(value: str) -> str:
    if len(value) <= 8:
        return "configured"
    return f"{value[:3]}...{value[-4:]}"


def _suggest_session_title(api_key: str, model: str, first_user_message: str) -> str:
    prompt = (
        "Rename this conversation session from the first user message only. "
        f"Return JSON only with one key: title. The title must be at most {SESSION_TITLE_MAX_CHARS} characters, "
        "clear, specific, and in the same language as the user message when possible. "
        "Do not add markdown, quotes around the whole response, trailing ellipses, or bracket noise."
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": first_user_message[:4000]},
        ],
        "temperature": 0.2,
        "max_completion_tokens": 80,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        AI_RENAME_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI rename request failed: HTTP {exc.code} {body[:240]}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"AI rename request failed: {exc}") from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("AI rename response did not include message content.") from exc

    title = _title_from_ai_content(content)
    if not title:
        raise RuntimeError("AI rename response did not include a title.")
    return title


def _title_from_ai_content(content: str) -> str:
    raw = content.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            raw = str(parsed.get("title", ""))
    except json.JSONDecodeError:
        pass
    title = " ".join(raw.strip().strip(chr(34) + chr(39)).split())
    title = title.rstrip(" .,-;:...")
    if len(title) > SESSION_TITLE_MAX_CHARS:
        title = title[:SESSION_TITLE_MAX_CHARS].rsplit(" ", 1)[0].rstrip(" .,-;:...")
    return title


def _exchange_payload(exchange: ExchangeRecord, timezone_name: str | None = None) -> dict[str, Any]:
    user_token_count = _estimate_token_count(exchange.user_message)
    assistant_token_count = _estimate_token_count(exchange.assistant_response)
    return {
        "exchange_id": exchange.exchange_id,
        "session_id": exchange.session_id,
        "model_name": exchange.model_name,
        "user_message": exchange.user_message,
        "assistant_response": exchange.assistant_response,
        "user_message_token_count": user_token_count,
        "assistant_response_token_count": assistant_token_count,
        "total_token_count": user_token_count + assistant_token_count,
        "assistant_created_at": exchange.assistant_created_at,
        "assistant_created_at_iso": format_timestamp_iso(exchange.assistant_created_at),
        "assistant_created_at_display": format_response_timestamp(
            exchange.assistant_created_at,
            timezone_name=timezone_name,
        ),
        "assistant_created_at_timezone": resolve_timezone_name(timezone_name),
        "created_at": exchange.created_at,
        "assistant_masked_at": exchange.assistant_masked_at,
        "assistant_masked_at_iso": format_timestamp_iso(exchange.assistant_masked_at) if exchange.assistant_masked_at else None,
        "is_masked": exchange.assistant_masked_at is not None,
        "created_at_iso": format_timestamp_iso(exchange.created_at),
        "deleted_at": exchange.deleted_at,
        "deleted_at_iso": format_timestamp_iso(exchange.deleted_at) if exchange.deleted_at else None,
        "deleted_reason": exchange.deleted_reason,
        "edited_at": exchange.edited_at,
        "edited_at_iso": format_timestamp_iso(exchange.edited_at) if exchange.edited_at else None,
        "is_deleted": exchange.deleted_at is not None,
        "is_excluded": exchange.deleted_at is not None,
    }


def _estimate_token_count(text: str) -> int:
    if not text:
        return 0
    lexical_count = len(TOKEN_PATTERN.findall(text))
    char_estimate = (len(text) + 3) // 4
    return max(lexical_count, char_estimate)


async def _bounded_json_body(
    request: Request,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any], JSONResponse | None]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                return {}, AdminHandlers._json_error("Request body is too large.", status_code=413)
        except ValueError:
            return {}, AdminHandlers._json_error("Invalid Content-Length.", status_code=400)

    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > max_bytes:
                return {}, AdminHandlers._json_error("Request body is too large.", status_code=413)
    except Exception:
        return {}, AdminHandlers._json_error("Request body must be JSON.", status_code=400)

    try:
        payload = await asyncio.to_thread(json.loads, body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, AdminHandlers._json_error("Request body must be JSON.", status_code=400)
    if not isinstance(payload, dict):
        return {}, AdminHandlers._json_error("Request body must be a JSON object.", status_code=400)
    return payload, None


async def _json_body(request: Request, allow_empty: bool = False) -> tuple[dict[str, Any], JSONResponse | None]:
    try:
        payload = await request.json()
    except Exception:
        if allow_empty:
            return {}, None
        return {}, AdminHandlers._json_error("Request body must be JSON.", status_code=400)
    if not isinstance(payload, dict):
        return {}, AdminHandlers._json_error("Request body must be a JSON object.", status_code=400)
    return payload, None


def _path_file_id(request: Request) -> tuple[int, JSONResponse | None]:
    try:
        return int(request.path_params["file_id"]), None
    except (KeyError, TypeError, ValueError):
        return 0, AdminHandlers._json_error("Invalid file_id.", status_code=400)


def _admin_upload_mime_type(filename: str) -> str | None:
    return ADMIN_FILE_EXTENSIONS.get(Path(filename).suffix.lower())


def _file_visible_to_session(file: SessionFileRecord, session: SessionRecord) -> bool:
    if file.scope_type == "session":
        return file.session_id == session.session_id
    return file.scope_type == "group" and file.group_id == session.group_id


def _path_exchange_id(request: Request) -> tuple[int, JSONResponse | None]:
    try:
        return int(request.path_params["exchange_id"]), None
    except (KeyError, TypeError, ValueError):
        return 0, AdminHandlers._json_error("Invalid exchange_id.", status_code=400)


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/admin/") and not value.startswith("//"):
        return value
    return "/admin/sessions"


def _request_is_secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
