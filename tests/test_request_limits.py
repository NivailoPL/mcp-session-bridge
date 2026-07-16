from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from app.request_limits import RequestBodyLimitMiddleware


def _limited_client(max_bytes: int = 16) -> tuple[TestClient, list[bool]]:
    called: list[bool] = []

    async def downstream(scope, receive, send) -> None:
        called.append(True)
        await JSONResponse({"ok": True})(scope, receive, send)

    return (
        TestClient(
            RequestBodyLimitMiddleware(
                downstream,
                path="/mcp",
                max_bytes=max_bytes,
            )
        ),
        called,
    )


def test_mcp_request_limit_rejects_content_length_before_dispatch() -> None:
    client, called = _limited_client()

    response = client.post(
        "/mcp",
        content=b"small",
        headers={"content-length": "17"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["message"] == "MCP request body is too large"
    assert called == []


def test_mcp_request_limit_counts_streamed_body_without_content_length() -> None:
    client, called = _limited_client()
    request = client.build_request(
        "POST",
        "/mcp",
        content=iter((b"1234567890", b"1234567890")),
    )

    response = client.send(request)

    assert response.status_code == 413
    assert called == []


def test_request_limit_does_not_affect_other_routes() -> None:
    client, called = _limited_client()

    response = client.post("/admin/api/example", content=b"x" * 100)

    assert response.status_code == 200
    assert called == [True]
