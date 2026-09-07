import base64
import pytest

from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, ImageContent, TextContent
from starlette.testclient import TestClient
from tests.image_samples import make_image


PNG = make_image()


def mcp_client(main, monkeypatch):
    async def verify(self, token):
        return AccessToken(token=token, client_id="image-test", scopes=[main.settings.scope])

    monkeypatch.setattr(main.BridgeTokenVerifier, "verify_token", verify)
    return TestClient(main.app, base_url="http://127.0.0.1:8787", headers={
        "Authorization": "Bearer isolated-test-token",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    })


def call(client, name, arguments):
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
        "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_native_image_survives_bridge_stateless_http(load_main, monkeypatch):
    main = load_main()

    @main.mcp.tool(structured_output=False)
    def image_transport_probe() -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text="Image metadata"),
            ImageContent(type="image", data=base64.b64encode(PNG).decode(), mimeType="image/png")])

    with mcp_client(main, monkeypatch) as client:
        result = call(client, "image_transport_probe", {})
    assert not result.get("isError")
    assert result["content"][1]["type"] == "image"
    assert result["content"][1]["mimeType"] == "image/png"
    assert base64.b64decode(result["content"][1]["data"]) == PNG


@pytest.mark.parametrize("format,filename", [("PNG", "x.png"), ("JPEG", "x.jpg")])
@pytest.mark.parametrize("scope", ["session", "group"])
def test_image_tools_through_http(load_main, monkeypatch, format, filename, scope):
    main = load_main()
    main.store.create_session("images", "Images")
    raw = make_image(format)
    with mcp_client(main, monkeypatch) as client:
        listing = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]
        names = {tool["name"] for tool in listing["tools"]}
        assert {"upload_session_image", "upload_group_image", "view_session_image"} <= names
        uploaded = call(client, f"upload_{scope}_image", {
            "session_id" if scope == "session" else "group_id": "images" if scope == "session" else "uncategorized",
            "filename": filename, "content_base64": base64.b64encode(raw).decode(),
        })
        metadata = uploaded["structuredContent"]
        assert metadata["ok"], uploaded
        result = call(client, "view_session_image", {"session_id": "images", "file_id": metadata["file"]["file_id"]})
        assert not result.get("isError"), result
        assert result["content"][1]["type"] == "image"
        assert result["content"][1]["mimeType"] == ("image/png" if format == "PNG" else "image/jpeg")
        assert base64.b64decode(result["content"][1]["data"]) == raw
