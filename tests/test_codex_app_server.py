from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.codex_app_server import (
    CodexAppServerClient,
    CodexPolicyViolationError,
    CodexProtocolError,
)


class FakeWebSocket:
    def __init__(self, *, version: str = "0.147.0", account: dict[str, Any] | None = None):
        self.version = version
        self.account = account
        self.sent: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.login_id = "login-1"

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        if "id" not in message:
            return
        method = message.get("method")
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "codexHome": "/var/lib/mcp-session-bridge-codex/codex-home",
                "platformFamily": "unix",
                "platformOs": "linux",
                "userAgent": f"codex-cli/{self.version}",
            }
        elif method == "account/read":
            result = {"account": self.account, "requiresOpenaiAuth": True}
        elif method == "account/login/start":
            result = {
                "type": "chatgptDeviceCode",
                "loginId": self.login_id,
                "verificationUrl": "https://auth.openai.com/device",
                "userCode": "ABCD-EFGH",
            }
        elif method in {"account/login/cancel", "account/logout"}:
            result = {}
            if method == "account/logout":
                self.account = None
        elif method == "thread/start":
            result = {
                "thread": {"id": "thread-1", "ephemeral": True},
                "model": "gpt-5.6",
                "modelProvider": "openai",
                "cwd": "/var/lib/mcp-session-bridge-codex/workspace",
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "sandbox": {"type": "readOnly"},
            }
        elif method == "turn/start":
            result = {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}}
        else:
            result = {}
        await self.incoming.put(json.dumps({"id": message["id"], "result": result}))
        if method == "turn/start":
            await self.incoming.put(
                json.dumps(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "itemId": "item-1",
                            "delta": "Hello from Codex",
                        },
                    }
                )
            )
            await self.incoming.put(
                json.dumps(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "items": [], "status": "completed"},
                        },
                    }
                )
            )

    async def recv(self) -> str:
        value = await self.incoming.get()
        if value is None:
            raise ConnectionError("closed")
        return value

    async def close(self) -> None:
        await self.incoming.put(None)


def _client(tmp_path: Path, socket: FakeWebSocket) -> CodexAppServerClient:
    async def connect() -> FakeWebSocket:
        return socket

    return CodexAppServerClient(
        socket_path=tmp_path / "codex.sock",
        workspace_dir=tmp_path / "workspace",
        expected_version="0.147.0",
        connect_factory=connect,
    )


def test_status_and_device_login_expose_no_credentials(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = FakeWebSocket()
        client = _client(tmp_path, socket)
        assert await client.status() == {
            "available": True,
            "authenticated": False,
            "account": None,
            "version": "0.147.0",
        }
        login = await client.start_device_login()
        assert login == {
            "login_id": "login-1",
            "verification_url": "https://auth.openai.com/device",
            "user_code": "ABCD-EFGH",
        }
        assert "token" not in json.dumps(login).lower()
        await client.cancel_device_login()
        await client.close()

    asyncio.run(scenario())


def test_account_payload_is_sanitized_and_logout_clears_threads(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = FakeWebSocket(
            account={"type": "chatgpt", "email": "owner@example.com", "planType": "plus"}
        )
        client = _client(tmp_path, socket)
        status = await client.status()
        assert status["account"] == {
            "type": "chatgpt",
            "email": "owner@example.com",
            "plan_type": "plus",
        }
        response = await client.chat("hello")
        assert response == {"thread_id": "thread-1", "message": "Hello from Codex"}
        await client.logout()
        with pytest.raises(ValueError, match="Unknown or expired Codex conversation"):
            await client.chat("continue", thread_id="thread-1")
        await client.close()

    asyncio.run(scenario())


def test_chat_uses_ephemeral_read_only_tool_free_thread(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = FakeWebSocket(
            account={"type": "chatgpt", "email": None, "planType": "pro"}
        )
        client = _client(tmp_path, socket)
        response = await client.chat("hello")
        assert response["message"] == "Hello from Codex"
        start = next(item for item in socket.sent if item.get("method") == "thread/start")
        params = start["params"]
        assert params["ephemeral"] is True
        assert params["approvalPolicy"] == "never"
        assert params["sandbox"] == "read-only"
        assert params["cwd"] == str(tmp_path / "workspace")
        assert params["config"]["web_search"] == "disabled"
        assert params["config"]["mcp_servers"] == {}
        assert all(value is False for value in params["config"]["features"].values())
        await client.close()

    asyncio.run(scenario())


def test_incompatible_runtime_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = _client(tmp_path, FakeWebSocket(version="0.146.0"))
        with pytest.raises(CodexProtocolError, match="expected 0.147.0"):
            await client.status()
        await client.close()

    asyncio.run(scenario())


def test_tool_event_interrupts_turn_and_fails_closed(tmp_path: Path) -> None:
    class ToolCallingSocket(FakeWebSocket):
        async def send(self, raw: str) -> None:
            message = json.loads(raw)
            if message.get("method") != "turn/start":
                await super().send(raw)
                return
            self.sent.append(message)
            await self.incoming.put(
                json.dumps(
                    {
                        "id": message["id"],
                        "result": {
                            "turn": {"id": "turn-1", "items": [], "status": "inProgress"}
                        },
                    }
                )
            )
            await self.incoming.put(
                json.dumps(
                    {
                        "method": "item/commandExecution/started",
                        "params": {"threadId": "thread-1", "turnId": "turn-1"},
                    }
                )
            )

    async def scenario() -> None:
        client = _client(
            tmp_path,
            ToolCallingSocket(
                account={"type": "chatgpt", "email": None, "planType": "plus"}
            ),
        )
        with pytest.raises(CodexPolicyViolationError, match="disabled"):
            await client.chat("read the filesystem")
        await client.close()

    asyncio.run(scenario())
