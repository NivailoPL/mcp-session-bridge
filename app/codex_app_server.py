from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from bridge_cli.codex_contract import CODEX_VERSION

MAX_CHAT_MESSAGE_CHARS = 8_000
MAX_OWNED_THREADS = 32
DEFAULT_RPC_TIMEOUT_SECONDS = 15.0
DEFAULT_TURN_TIMEOUT_SECONDS = 300.0

_VERSION_PATTERN = re.compile(r"\b(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)\b")
_TOOL_EVENT_MARKERS = (
    "commandExecution",
    "fileChange",
    "mcpToolCall",
    "dynamicToolCall",
    "collabToolCall",
    "webSearch",
    "permissions/request",
)
_DISABLED_FEATURES = {
    "shell_tool": False,
    "unified_exec": False,
    "apps": False,
    "hooks": False,
    "multi_agent": False,
    "remote_plugin": False,
}
_NON_OPERATIONAL_ITEM_TYPES = {
    "userMessage",
    "agentMessage",
    "reasoning",
    "plan",
    "contextCompaction",
}


class CodexAppServerError(RuntimeError):
    """Base error safe to map to a bounded admin API response."""


class CodexUnavailableError(CodexAppServerError):
    pass


class CodexProtocolError(CodexAppServerError):
    pass


class CodexTimeoutError(CodexAppServerError):
    pass


class CodexPolicyViolationError(CodexAppServerError):
    pass


class _WebSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


@dataclass
class _TurnState:
    future: asyncio.Future[dict[str, Any]]
    fragments: list[str] = field(default_factory=list)


class CodexAppServerClient:
    """Small, fail-closed adapter for the pinned Codex App Server protocol."""

    def __init__(
        self,
        *,
        socket_path: Path,
        workspace_dir: Path,
        expected_version: str = CODEX_VERSION,
        connect_factory: Callable[[], Awaitable[_WebSocket]] | None = None,
        rpc_timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
        turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    ):
        self.socket_path = socket_path
        self.workspace_dir = workspace_dir
        self.expected_version = expected_version
        self._connect_factory = connect_factory or self._connect_unix_socket
        self.rpc_timeout_seconds = rpc_timeout_seconds
        self.turn_timeout_seconds = turn_timeout_seconds
        self._ws: _WebSocket | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._turns: dict[str, _TurnState] = {}
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._version: str | None = None
        self._login_payload: dict[str, str] | None = None
        self._login_outcome: str | None = None
        self._closing = False
        self._interrupt_tasks: set[asyncio.Task[None]] = set()

    async def _connect_unix_socket(self) -> _WebSocket:
        try:
            from websockets.asyncio.client import unix_connect
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise CodexUnavailableError("Codex transport dependency is unavailable.") from exc
        try:
            return await unix_connect(str(self.socket_path), uri="ws://localhost/")
        except (OSError, TimeoutError) as exc:
            raise CodexUnavailableError("Codex App Server is unavailable.") from exc

    async def _ensure_connected(self) -> None:
        if self._ws is not None and self._reader_task is not None and not self._reader_task.done():
            return
        async with self._connect_lock:
            if self._ws is not None and self._reader_task is not None and not self._reader_task.done():
                return
            self._closing = False
            try:
                self._ws = await self._connect_factory()
            except CodexAppServerError:
                raise
            except Exception as exc:
                raise CodexUnavailableError("Codex App Server is unavailable.") from exc
            self._reader_task = asyncio.create_task(self._reader())
            try:
                result = await self._rpc(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "mcp-session-bridge",
                            "title": "MCP Session Bridge",
                            "version": "1",
                        },
                        "capabilities": {"experimentalApi": False},
                    },
                    ensure_connected=False,
                )
                user_agent = str(result.get("userAgent", ""))
                match = _VERSION_PATTERN.search(user_agent)
                version = match.group(1) if match else None
                if version != self.expected_version:
                    raise CodexProtocolError(
                        f"Incompatible Codex App Server version; expected {self.expected_version}."
                    )
                self._version = version
                await self._notify("initialized", {})
            except Exception:
                await self.close()
                raise

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        ensure_connected: bool = True,
    ) -> Any:
        if ensure_connected:
            await self._ensure_connected()
        ws = self._ws
        if ws is None:
            raise CodexUnavailableError("Codex App Server is unavailable.")
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            request: dict[str, Any] = {"id": request_id, "method": method}
            if params is not None:
                request["params"] = params
            await ws.send(json.dumps(request))
            try:
                return await asyncio.wait_for(future, timeout=self.rpc_timeout_seconds)
            except TimeoutError as exc:
                raise CodexTimeoutError("Codex App Server request timed out.") from exc
        except CodexAppServerError:
            raise
        except Exception as exc:
            raise CodexUnavailableError("Codex App Server connection failed.") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise CodexUnavailableError("Codex App Server is unavailable.")
        try:
            await ws.send(json.dumps({"method": method, "params": params}))
        except Exception as exc:
            raise CodexUnavailableError("Codex App Server connection failed.") from exc

    async def _reader(self) -> None:
        try:
            assert self._ws is not None
            while True:
                raw = await self._ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise CodexProtocolError("Codex App Server sent an invalid message.")
                if "id" in message and "method" not in message:
                    self._resolve_response(message)
                    continue
                method = message.get("method")
                params = message.get("params")
                if not isinstance(method, str) or not isinstance(params, dict):
                    raise CodexProtocolError("Codex App Server sent an invalid notification.")
                if "id" in message:
                    await self._deny_server_request(message)
                    self._fail_tool_event(params)
                    continue
                await self._handle_notification(method, params)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        finally:
            if not self._closing:
                self._invalidate_connection()

    def _resolve_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        future = self._pending.get(request_id) if isinstance(request_id, int) else None
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if code == -32001:
                future.set_exception(CodexUnavailableError("Codex App Server is busy."))
            else:
                future.set_exception(CodexProtocolError("Codex App Server rejected the request."))
            return
        if "result" not in message:
            future.set_exception(CodexProtocolError("Codex App Server response is incomplete."))
            return
        future.set_result(message["result"])

    async def _deny_server_request(self, message: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            return
        await ws.send(
            json.dumps(
                {
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Interactive tools are disabled."},
                }
            )
        )

    def _turn_state(self, turn_id: str) -> _TurnState:
        state = self._turns.get(turn_id)
        if state is None:
            state = _TurnState(asyncio.get_running_loop().create_future())
            self._turns[turn_id] = state
        return state

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "account/login/completed":
            login_id = params.get("loginId")
            if self._login_payload is None or login_id != self._login_payload.get("login_id"):
                return
            self._login_outcome = "failed" if params.get("error") else "authenticated"
            return
        if method == "item/agentMessage/delta":
            turn_id = params.get("turnId")
            delta = params.get("delta")
            if isinstance(turn_id, str) and isinstance(delta, str):
                self._turn_state(turn_id).fragments.append(delta)
            return
        if method in {"item/started", "item/completed"}:
            turn_id = params.get("turnId")
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") not in _NON_OPERATIONAL_ITEM_TYPES:
                self._fail_tool_event(params)
                return
            if isinstance(turn_id, str) and isinstance(item, dict):
                text = item.get("text")
                if method == "item/completed" and item.get("type") == "agentMessage" and isinstance(text, str):
                    self._turn_state(turn_id).fragments[:] = [text]
            return
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                return
            state = self._turn_state(turn["id"])
            if state.future.done():
                return
            status = turn.get("status")
            if status == "completed":
                state.future.set_result(turn)
            elif status == "interrupted":
                state.future.set_exception(CodexProtocolError("Codex turn was interrupted."))
            else:
                state.future.set_exception(CodexProtocolError("Codex turn failed."))
            return
        if any(marker.lower() in method.lower() for marker in _TOOL_EVENT_MARKERS):
            self._fail_tool_event(params)

    def _fail_tool_event(self, params: dict[str, Any]) -> None:
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str):
            return
        state = self._turn_state(turn_id)
        if not state.future.done():
            state.future.set_exception(
                CodexPolicyViolationError("Codex tools are disabled for this chat.")
            )
        self._schedule_interrupt(params.get("threadId"), turn_id)

    def _schedule_interrupt(self, thread_id: Any, turn_id: str) -> None:
        if not isinstance(thread_id, str):
            return

        async def interrupt() -> None:
            if self._closing:
                return
            try:
                await self._rpc(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": turn_id},
                    ensure_connected=False,
                )
            except CodexAppServerError:
                pass

        task = asyncio.create_task(interrupt())
        self._interrupt_tasks.add(task)
        task.add_done_callback(self._interrupt_tasks.discard)

    def _invalidate_connection(self) -> None:
        self._ws = None
        self._version = None
        self._thread_locks.clear()
        self._login_payload = None
        self._login_outcome = None
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(CodexUnavailableError("Codex App Server disconnected."))
        for state in list(self._turns.values()):
            if not state.future.done():
                state.future.set_exception(CodexUnavailableError("Codex App Server disconnected."))
        self._turns.clear()

    async def status(self) -> dict[str, Any]:
        await self._ensure_connected()
        result = await self._rpc("account/read", {"refreshToken": False})
        account = result.get("account") if isinstance(result, dict) else None
        return {
            "available": True,
            "authenticated": isinstance(account, dict),
            "account": _safe_account(account),
            "version": self._version,
        }

    async def start_device_login(self) -> dict[str, str]:
        await self._ensure_connected()
        if self._login_payload is not None and self._login_outcome is None:
            return dict(self._login_payload)
        result = await self._rpc("account/login/start", {"type": "chatgptDeviceCode"})
        login_id = result.get("loginId") if isinstance(result, dict) else None
        verification_url = result.get("verificationUrl") if isinstance(result, dict) else None
        user_code = result.get("userCode") if isinstance(result, dict) else None
        parsed = urlparse(verification_url) if isinstance(verification_url, str) else None
        if (
            not isinstance(login_id, str)
            or not isinstance(user_code, str)
            or parsed is None
            or parsed.scheme != "https"
            or not parsed.netloc
        ):
            raise CodexProtocolError("Codex returned an invalid device login response.")
        self._login_outcome = None
        self._login_payload = {
            "login_id": login_id,
            "verification_url": verification_url,
            "user_code": user_code,
        }
        return dict(self._login_payload)

    async def device_login_status(self) -> dict[str, Any]:
        status = await self.status()
        if status["authenticated"]:
            self._login_payload = None
            self._login_outcome = None
            return {**status, "login_status": "authenticated"}
        if self._login_outcome is not None:
            return {**status, "login_status": self._login_outcome}
        return {**status, "login_status": "pending" if self._login_payload else "signed_out"}

    async def cancel_device_login(self) -> None:
        if self._login_payload is None:
            return
        await self._rpc("account/login/cancel", {"loginId": self._login_payload["login_id"]})
        self._login_payload = None
        self._login_outcome = "cancelled"

    async def logout(self) -> None:
        await self._rpc("account/logout", None)
        self._login_payload = None
        self._login_outcome = None
        self._thread_locks.clear()

    async def chat(self, message: str, *, thread_id: str | None = None) -> dict[str, str]:
        normalized = message.strip()
        if not normalized:
            raise ValueError("message must not be empty.")
        if len(normalized) > MAX_CHAT_MESSAGE_CHARS:
            raise ValueError(f"message must be {MAX_CHAT_MESSAGE_CHARS} characters or fewer.")
        await self._ensure_connected()
        if thread_id is None:
            self._make_thread_capacity()
            result = await self._rpc(
                "thread/start",
                {
                    "cwd": str(self.workspace_dir),
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "approvalsReviewer": "user",
                    "sandbox": "read-only",
                    "personality": "none",
                    "config": {
                        "features": dict(_DISABLED_FEATURES),
                        "web_search": "disabled",
                        "mcp_servers": {},
                    },
                },
            )
            thread = result.get("thread") if isinstance(result, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(thread_id, str) or thread.get("ephemeral") is not True:
                raise CodexProtocolError("Codex failed to create an ephemeral conversation.")
            self._thread_locks[thread_id] = asyncio.Lock()
        elif thread_id not in self._thread_locks:
            raise ValueError("Unknown or expired Codex conversation.")

        lock = self._thread_locks[thread_id]
        if lock.locked():
            raise ValueError("A Codex response is already in progress.")
        async with lock:
            result = await self._rpc(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": normalized}],
                    "clientUserMessageId": str(uuid.uuid4()),
                },
            )
            turn = result.get("turn") if isinstance(result, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str):
                raise CodexProtocolError("Codex failed to start a turn.")
            state = self._turn_state(turn_id)
            try:
                await asyncio.wait_for(state.future, timeout=self.turn_timeout_seconds)
            except TimeoutError as exc:
                self._schedule_interrupt(thread_id, turn_id)
                raise CodexTimeoutError("Codex response timed out.") from exc
            finally:
                self._turns.pop(turn_id, None)
            response = "".join(state.fragments).strip()
            if not response:
                raise CodexProtocolError("Codex returned an empty response.")
            if self._thread_locks.get(thread_id) is lock:
                self._thread_locks.pop(thread_id)
                self._thread_locks[thread_id] = lock
            return {"thread_id": thread_id, "message": response}

    def _make_thread_capacity(self) -> None:
        if len(self._thread_locks) < MAX_OWNED_THREADS:
            return
        for owned_thread_id, lock in tuple(self._thread_locks.items()):
            if not lock.locked():
                self._thread_locks.pop(owned_thread_id, None)
                return
        raise ValueError("Too many Codex conversations are active.")

    async def close(self) -> None:
        self._closing = True
        task = self._reader_task
        self._reader_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        tasks = tuple(self._interrupt_tasks)
        self._interrupt_tasks.clear()
        for interrupt_task in tasks:
            interrupt_task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._invalidate_connection()


def _safe_account(account: Any) -> dict[str, Any] | None:
    if not isinstance(account, dict):
        return None
    account_type = account.get("type")
    if account_type == "chatgpt":
        email = account.get("email")
        plan_type = account.get("planType")
        return {
            "type": "chatgpt",
            "email": email if isinstance(email, str) else None,
            "plan_type": plan_type if isinstance(plan_type, str) else "unknown",
        }
    if account_type == "apiKey":
        return {"type": "api_key", "email": None, "plan_type": None}
    return None
