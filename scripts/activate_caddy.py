#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path("/root/mcp-session-bridge")
CADDYFILE = Path("/etc/caddy/Caddyfile")
BACKUP_DIR = Path("/root/firewall-backups")
EXPECTED_IP = "89.167.57.190"
HOSTNAME = "mcp.panchmurka.wtf"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Activate even if DNS has not propagated yet.")
    args = parser.parse_args()

    resolved = sorted({ip for ip in socket.gethostbyname_ex(HOSTNAME)[2]})
    if EXPECTED_IP not in resolved and not args.force:
        raise SystemExit(f"{HOSTNAME} resolves to {resolved}, not {EXPECTED_IP}. Update DNS first or pass --force.")

    block = (PROJECT / "deploy" / "Caddyfile.mcp-session-bridge").read_text(encoding="utf-8").strip()
    text = CADDYFILE.read_text(encoding="utf-8")
    if HOSTNAME in text:
        print(f"{HOSTNAME} is already present in {CADDYFILE}")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_DIR / f"Caddyfile-before-mcp-session-bridge-activate-{stamp}"
    shutil.copy2(CADDYFILE, backup)
    CADDYFILE.write_text(text.rstrip() + "\n\n" + block + "\n", encoding="utf-8")

    subprocess.run(["caddy", "fmt", "--overwrite", str(CADDYFILE)], check=True)
    subprocess.run(["caddy", "validate", "--config", str(CADDYFILE)], check=True)
    subprocess.run(["systemctl", "reload", "caddy"], check=True)
    print(f"Activated {HOSTNAME}; backup: {backup}")


if __name__ == "__main__":
    main()
