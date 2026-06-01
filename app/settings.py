from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    resource_path: str
    db_path: Path
    context_packs_dir: Path
    default_context_pack_id: str
    summaries_dir: Path
    transcript_chunk_max_lines: int
    transcript_chunk_max_chars: int
    owner_username: str
    owner_password_hash: str
    secret_key: str
    access_token_seconds: int
    refresh_token_seconds: int
    auth_code_seconds: int
    auth_challenge_seconds: int
    scope: str
    transport_allowed_hosts: list[str]
    transport_allowed_origins: list[str]

    @property
    def issuer_url(self) -> str:
        return f"{self.public_base_url}/"

    @property
    def resource_url(self) -> str:
        return f"{self.public_base_url}{self.resource_path}"

    @property
    def authz_endpoint(self) -> str:
        return f"{self.public_base_url}/oauth/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.public_base_url}/oauth/token"

    @property
    def registration_endpoint(self) -> str:
        return f"{self.public_base_url}/oauth/register"


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    public_base_url = _required("BRIDGE_PUBLIC_BASE_URL").rstrip("/")
    resource_path = os.getenv("BRIDGE_RESOURCE_PATH", "/mcp")
    if not resource_path.startswith("/"):
        resource_path = f"/{resource_path}"

    return Settings(
        public_base_url=public_base_url,
        resource_path=resource_path,
        db_path=Path(os.getenv("BRIDGE_DB_PATH", str(ROOT / "data" / "bridge.sqlite3"))),
        context_packs_dir=Path(os.getenv("BRIDGE_CONTEXT_PACKS_DIR", str(ROOT / "data" / "context-packs"))),
        default_context_pack_id=os.getenv("BRIDGE_DEFAULT_CONTEXT_PACK_ID", "manual-context"),
        summaries_dir=Path(os.getenv("BRIDGE_SUMMARIES_DIR", str(ROOT / "data" / "session-summaries"))),
        transcript_chunk_max_lines=int(os.getenv("BRIDGE_TRANSCRIPT_CHUNK_MAX_LINES", "180")),
        transcript_chunk_max_chars=int(os.getenv("BRIDGE_TRANSCRIPT_CHUNK_MAX_CHARS", "12000")),
        owner_username=os.getenv("BRIDGE_OWNER_USERNAME", "owner"),
        owner_password_hash=_required("BRIDGE_OWNER_PASSWORD_HASH"),
        secret_key=_required("BRIDGE_SECRET_KEY"),
        access_token_seconds=int(os.getenv("BRIDGE_ACCESS_TOKEN_SECONDS", "1800")),
        refresh_token_seconds=int(os.getenv("BRIDGE_REFRESH_TOKEN_SECONDS", "2592000")),
        auth_code_seconds=int(os.getenv("BRIDGE_AUTH_CODE_SECONDS", "300")),
        auth_challenge_seconds=int(os.getenv("BRIDGE_AUTH_CHALLENGE_SECONDS", "600")),
        scope=os.getenv("BRIDGE_SCOPE", "bridge"),
        transport_allowed_hosts=_csv_env(
            "BRIDGE_TRANSPORT_ALLOWED_HOSTS",
            "127.0.0.1:8787,localhost:8787",
        ),
        transport_allowed_origins=_csv_env(
            "BRIDGE_TRANSPORT_ALLOWED_ORIGINS",
            "http://127.0.0.1:8787,http://localhost:8787,https://claude.ai,https://chatgpt.com,https://chat.openai.com",
        ),
    )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]
