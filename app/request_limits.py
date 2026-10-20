from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestBodyLimitMiddleware:
    """Buffer POST bodies on the given paths and refuse them past ``max_bytes``."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        path: str | None = None,
        paths: Iterable[str] = (),
        jsonrpc: bool = True,
    ):
        self.app = app
        self.paths = {item.rstrip("/") or "/" for item in (*paths, *([path] if path else []))}
        self.max_bytes = max_bytes
        self.jsonrpc = jsonrpc

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or (str(scope.get("path", "")).rstrip("/") or "/") not in self.paths
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = 0
        if content_length > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.jsonrpc:
            payload = {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32000,
                    "message": "MCP request body is too large",
                },
                "id": None,
            }
        else:
            payload = {"error": "invalid_request", "error_description": "request body is too large"}
        await JSONResponse(payload, status_code=413)(scope, receive, send)
