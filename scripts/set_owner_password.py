#!/usr/bin/env python3
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.security import password_hash, token_urlsafe


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--username", default="owner")
    parser.add_argument("--password")
    parser.add_argument("--write-once-file")
    args = parser.parse_args()

    env_path = Path(args.env)
    password = args.password or _generated_password()
    values = _read_env(env_path)
    values.setdefault("BRIDGE_PUBLIC_BASE_URL", "http://127.0.0.1:8787")
    values.setdefault("BRIDGE_RESOURCE_PATH", "/mcp")
    values.setdefault("BRIDGE_DB_PATH", str(ROOT / "data" / "bridge.sqlite3"))
    values.setdefault("BRIDGE_TRANSCRIPT_CHUNK_MAX_LINES", "180")
    values.setdefault("BRIDGE_TRANSCRIPT_CHUNK_MAX_CHARS", "12000")
    values["BRIDGE_OWNER_USERNAME"] = args.username
    values["BRIDGE_OWNER_PASSWORD_HASH"] = password_hash(password)
    values.setdefault("BRIDGE_SECRET_KEY", token_urlsafe(48))
    values.setdefault("BRIDGE_ACCESS_TOKEN_SECONDS", "1800")
    values.setdefault("BRIDGE_REFRESH_TOKEN_SECONDS", "2592000")
    values.setdefault("BRIDGE_AUTH_CODE_SECONDS", "300")
    values.setdefault("BRIDGE_AUTH_CHALLENGE_SECONDS", "600")
    values.setdefault("BRIDGE_SCOPE", "bridge")
    values.setdefault("BRIDGE_TRANSPORT_ALLOWED_HOSTS", "127.0.0.1:8787,localhost:8787")
    values.setdefault(
        "BRIDGE_TRANSPORT_ALLOWED_ORIGINS",
        "http://127.0.0.1:8787,http://localhost:8787,https://claude.ai,https://chatgpt.com,https://chat.openai.com",
    )

    _write_env(env_path, values)
    env_path.chmod(0o600)

    if args.write_once_file:
        once_path = Path(args.write_once_file)
        once_path.parent.mkdir(parents=True, exist_ok=True)
        once_path.write_text(
            f"username={args.username}\npassword={password}\n",
            encoding="utf-8",
        )
        once_path.chmod(0o600)
        print(f"Wrote one-time owner credentials to {once_path}")
    else:
        print(f"username={args.username}")
        print(f"password={password}")


def _generated_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(28))


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    order = [
        "BRIDGE_PUBLIC_BASE_URL",
        "BRIDGE_RESOURCE_PATH",
        "BRIDGE_DB_PATH",
        "BRIDGE_TRANSCRIPT_CHUNK_MAX_LINES",
        "BRIDGE_TRANSCRIPT_CHUNK_MAX_CHARS",
        "BRIDGE_OWNER_USERNAME",
        "BRIDGE_OWNER_PASSWORD_HASH",
        "BRIDGE_SECRET_KEY",
        "BRIDGE_ACCESS_TOKEN_SECONDS",
        "BRIDGE_REFRESH_TOKEN_SECONDS",
        "BRIDGE_AUTH_CODE_SECONDS",
        "BRIDGE_AUTH_CHALLENGE_SECONDS",
        "BRIDGE_SCOPE",
        "BRIDGE_TRANSPORT_ALLOWED_HOSTS",
        "BRIDGE_TRANSPORT_ALLOWED_ORIGINS",
    ]
    lines = [f"{key}={values[key]}" for key in order if key in values]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
