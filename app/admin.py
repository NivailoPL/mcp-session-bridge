from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import time
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.security import token_urlsafe, verify_password
from app.settings import Settings
from app.storage import ExchangeRecord, SessionFileRecord, SessionGroupRecord, SessionRecord, Store
from app.time_format import (
    DEFAULT_DISPLAY_TIMEZONE_NAME,
    DISPLAY_TIMEZONE_SETTING_KEY,
    format_response_timestamp,
    format_timestamp_iso,
    resolve_timezone_name,
)

ADMIN_COOKIE = "mcp_bridge_admin"
ADMIN_SESSION_SECONDS = 12 * 60 * 60


class AdminHandlers:
    def __init__(self, settings: Settings, store: Store, html_path: Path):
        self.settings = settings
        self.store = store
        self.html_path = html_path

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
        fields: dict[str, str] = {}
        for key in ("name", "color", "icon_key"):
            if key in payload:
                fields[key] = str(payload[key])
        if not fields:
            return self._json_error("No editable session group fields provided.", status_code=400)
        try:
            group = self.store.update_session_group(request.path_params["group_id"], **fields)
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
        return JSONResponse(
            {
                "ok": True,
                "display_timezone": display_timezone,
                "session": _session_payload(session),
                "files": {
                    "session": self.store.list_session_files(session_id=session.session_id),
                    "group": self.store.list_session_files(group_id=session.group_id),
                },
                "exchanges": [
                    _exchange_payload(exchange, timezone_name=display_timezone)
                    for exchange in exchanges
                ],
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
        if "group_id" not in payload:
            return self._json_error("group_id is required.", status_code=400)
        try:
            session = self.store.set_session_group(
                request.path_params["session_id"],
                str(payload.get("group_id", "")),
            )
        except ValueError as exc:
            return self._value_error(exc)
        return JSONResponse(
            {"ok": True, "session": _session_payload(session)},
            headers=self._no_store_headers(),
        )

    async def api_file(self, request: Request) -> Response:
        _, error = self._require_admin(request)
        if error:
            return error
        try:
            file_id = int(request.path_params["file_id"])
        except (KeyError, TypeError, ValueError):
            return self._json_error("Invalid file_id.", status_code=400)
        saved = self.store.get_session_file(file_id)
        if saved is None:
            return self._json_error(f"Unknown file_id: {file_id}", status_code=404)
        return JSONResponse(
            {"ok": True, "file": _session_file_payload(saved, include_content=True)},
            headers=self._no_store_headers(),
        )

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

    def _login_form(self, next_path: str, error: str | None = None, status_code: int = 200) -> HTMLResponse:
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        escaped_next = html.escape(next_path, quote=True)
        body = f"""<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MCP Session Bridge Admin</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f4;
      --text: #1f2933;
      --muted: #68727d;
      --border: #d8ddd5;
      --accent: #176b5b;
      --danger: #a33a32;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(92vw, 28rem);
      padding: 2rem;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 18px 40px rgb(31 41 51 / 10%);
    }}
    h1 {{ margin: 0 0 .35rem; font-size: 1.45rem; }}
    p {{ margin: 0 0 1.25rem; color: var(--muted); }}
    label {{ display: block; margin-top: 1rem; font-weight: 650; }}
    input {{
      width: 100%;
      margin-top: .4rem;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: .7rem .75rem;
      font: inherit;
    }}
    button {{
      width: 100%;
      margin-top: 1.25rem;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      padding: .75rem 1rem;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }}
    .error {{ color: var(--danger); font-weight: 650; }}
  </style>
</head>
<body>
  <main>
    <h1>Admin panel</h1>
    <p>MCP Session Bridge</p>
    {error_html}
    <form method="post" action="/admin/login">
      <input type="hidden" name="next" value="{escaped_next}">
      <label>Login <input name="username" autocomplete="username" required></label>
      <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
      <button type="submit">Log in</button>
    </form>
  </main>
</body>
</html>"""
        return HTMLResponse(body, status_code=status_code, headers=self._admin_headers())

    def _value_error(self, exc: ValueError) -> JSONResponse:
        message = str(exc)
        status_code = 404 if message.startswith("Unknown ") else 400
        return self._json_error(message, status_code=status_code)

    @staticmethod
    def _json_error(message: str, status_code: int) -> JSONResponse:
        return JSONResponse({"ok": False, "error": message}, status_code=status_code, headers=AdminHandlers._no_store_headers())

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


def _session_group_payload(group: SessionGroupRecord) -> dict[str, Any]:
    return {
        "group_id": group.group_id,
        "name": group.name,
        "color": group.color,
        "icon_key": group.icon_key,
        "sort_order": group.sort_order,
        "is_system": group.is_system,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
        "deleted_at": group.deleted_at,
    }


def _session_file_payload(file: SessionFileRecord, include_content: bool = False) -> dict[str, Any]:
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


def _exchange_payload(exchange: ExchangeRecord, timezone_name: str | None = None) -> dict[str, Any]:
    return {
        "exchange_id": exchange.exchange_id,
        "session_id": exchange.session_id,
        "model_name": exchange.model_name,
        "user_message": exchange.user_message,
        "assistant_response": exchange.assistant_response,
        "assistant_created_at": exchange.assistant_created_at,
        "assistant_created_at_iso": format_timestamp_iso(exchange.assistant_created_at),
        "assistant_created_at_display": format_response_timestamp(
            exchange.assistant_created_at,
            timezone_name=timezone_name,
        ),
        "assistant_created_at_timezone": resolve_timezone_name(timezone_name),
        "created_at": exchange.created_at,
        "created_at_iso": format_timestamp_iso(exchange.created_at),
        "deleted_at": exchange.deleted_at,
        "deleted_at_iso": format_timestamp_iso(exchange.deleted_at) if exchange.deleted_at else None,
        "deleted_reason": exchange.deleted_reason,
        "edited_at": exchange.edited_at,
        "edited_at_iso": format_timestamp_iso(exchange.edited_at) if exchange.edited_at else None,
        "is_deleted": exchange.deleted_at is not None,
    }


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
