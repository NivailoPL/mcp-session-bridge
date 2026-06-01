#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path("/root/mcp-session-bridge")
DEFAULT_CADDYFILE = Path("/etc/caddy/Caddyfile")
DEFAULT_BACKUP_DIR = Path("/root/firewall-backups")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hostname", required=True, help="Public hostname to activate in Caddy.")
    parser.add_argument("--expected-ip", help="Require the hostname to resolve to this IP before activation.")
    parser.add_argument("--project-dir", default=str(PROJECT), help="Deployed mcp-session-bridge directory.")
    parser.add_argument("--caddyfile", default=str(DEFAULT_CADDYFILE), help="Path to the active Caddyfile.")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR), help="Directory for Caddyfile backups.")
    parser.add_argument("--force", action="store_true", help="Activate even if DNS has not propagated yet.")
    args = parser.parse_args()

    if args.expected_ip:
        resolved = sorted({ip for ip in socket.gethostbyname_ex(args.hostname)[2]})
        if args.expected_ip not in resolved and not args.force:
            raise SystemExit(
                f"{args.hostname} resolves to {resolved}, not {args.expected_ip}. "
                "Update DNS first or pass --force."
            )

    project = Path(args.project_dir)
    caddyfile = Path(args.caddyfile)
    backup_dir = Path(args.backup_dir)
    block = (project / "deploy" / "Caddyfile.mcp-session-bridge").read_text(encoding="utf-8").strip()
    block = block.replace("your-mcp.example.com", args.hostname)
    text = caddyfile.read_text(encoding="utf-8")
    if args.hostname in text:
        print(f"{args.hostname} is already present in {caddyfile}")
        return

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"Caddyfile-before-mcp-session-bridge-activate-{stamp}"
    shutil.copy2(caddyfile, backup)
    caddyfile.write_text(text.rstrip() + "\n\n" + block + "\n", encoding="utf-8")

    subprocess.run(["caddy", "fmt", "--overwrite", str(caddyfile)], check=True)
    subprocess.run(["caddy", "validate", "--config", str(caddyfile)], check=True)
    subprocess.run(["systemctl", "reload", "caddy"], check=True)
    print(f"Activated {args.hostname}; backup: {backup}")


if __name__ == "__main__":
    main()
