#!/usr/bin/env python3
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.security import SECRET_KEY_PLACEHOLDER, password_hash, token_urlsafe, validate_secret_key
from bridge_cli.config import read_env_file, update_env_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--username", default="owner")
    parser.add_argument("--password")
    parser.add_argument("--write-once-file")
    args = parser.parse_args()

    env_path = Path(args.env)
    password = args.password or _generated_password()
    values = read_env_file(env_path)
    key = values.get("BRIDGE_SECRET_KEY", "")
    if not key.strip() or key.strip() == SECRET_KEY_PLACEHOLDER:
        db_path = Path(values.get("BRIDGE_DB_PATH") or str(ROOT / "data" / "bridge.sqlite3"))
        if db_path.exists():
            parser.error(
                "Cannot replace BRIDGE_SECRET_KEY while an existing database is present. "
                "See docs/security.md for existing-installation recovery."
            )
        key = token_urlsafe(48)
    try:
        values["BRIDGE_SECRET_KEY"] = validate_secret_key(key)
    except ValueError as exc:
        parser.error(str(exc))
    values.setdefault("BRIDGE_PUBLIC_BASE_URL", "http://127.0.0.1:8787")
    values.setdefault("BRIDGE_RESOURCE_PATH", "/mcp")
    values.setdefault("BRIDGE_DB_PATH", str(ROOT / "data" / "bridge.sqlite3"))
    values.setdefault("BRIDGE_TRANSCRIPT_CHUNK_MAX_LINES", "180")
    values.setdefault("BRIDGE_TRANSCRIPT_CHUNK_MAX_CHARS", "12000")
    values["BRIDGE_OWNER_USERNAME"] = args.username
    values["BRIDGE_OWNER_PASSWORD_HASH"] = password_hash(password)
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

    update_env_file(env_path, values)
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


if __name__ == "__main__":
    main()
