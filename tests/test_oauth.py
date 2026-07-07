from pathlib import Path

from app.oauth import OAuthHandlers
from app.settings import Settings
from app.storage import Store


def test_oauth_accepts_public_base_url_as_resource_alias(tmp_path: Path) -> None:
    handler = OAuthHandlers(_settings(tmp_path), Store(tmp_path / "bridge.sqlite3"))

    assert handler._canonical_resource("https://mcp.example.test") == "https://mcp.example.test/mcp"
    assert handler._canonical_resource("https://mcp.example.test/") == "https://mcp.example.test/mcp"
    assert handler._canonical_resource("https://mcp.example.test/mcp") == "https://mcp.example.test/mcp"
    assert handler._canonical_resource("https://other.example.test") is None


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        public_base_url="https://mcp.example.test",
        resource_path="/mcp",
        db_path=tmp_path / "bridge.sqlite3",
        context_packs_dir=tmp_path / "context-packs",
        default_context_pack_id="manual-context",
        transcript_chunk_max_lines=180,
        transcript_chunk_max_chars=12000,
        owner_username="owner",
        owner_password_hash="hash",
        secret_key="secret",
        access_token_seconds=1800,
        refresh_token_seconds=2592000,
        auth_code_seconds=300,
        auth_challenge_seconds=600,
        scope="bridge",
        transport_allowed_hosts=["mcp.example.test"],
        transport_allowed_origins=["https://chatgpt.com"],
    )
