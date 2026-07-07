# Deployment

This project can run as a normal ASGI app behind a reverse proxy. The included deployment files assume Uvicorn, systemd, and Caddy, but they are templates rather than a required platform.

Docker is intentionally out of scope for v0.1.

## Environment

Create a production `.env` from `.env.example` and set at least:

```env
BRIDGE_PUBLIC_BASE_URL=https://your-mcp.example.com
BRIDGE_RESOURCE_PATH=/mcp
BRIDGE_DB_PATH=data/bridge.sqlite3
BRIDGE_OWNER_USERNAME=owner
BRIDGE_OWNER_PASSWORD_HASH=<generated hash>
BRIDGE_SECRET_KEY=<long random secret>
BRIDGE_TRANSPORT_ALLOWED_HOSTS=your-mcp.example.com,your-mcp.example.com:443,127.0.0.1:8787,localhost:8787
BRIDGE_TRANSPORT_ALLOWED_ORIGINS=https://your-mcp.example.com,https://claude.ai,https://chatgpt.com,https://chat.openai.com
```

Generate the owner password hash:

```bash
uv run python scripts/set_owner_password.py --username owner
```

## Uvicorn

Local production-like command:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787 --no-access-log
```

## systemd

Template:

```text
deploy/mcp-session-bridge.service
```

Typical update:

```bash
cd /root/mcp-session-bridge
uv sync --frozen --no-dev
systemctl restart mcp-session-bridge
systemctl status mcp-session-bridge --no-pager -l
```

If the virtual environment is inconsistent:

```bash
cd /root/mcp-session-bridge
systemctl stop mcp-session-bridge
rm -rf .venv
uv cache clean --force
uv sync --frozen --no-dev
systemctl start mcp-session-bridge
```

## Caddy

Template:

```text
deploy/Caddyfile.mcp-session-bridge
```

The template uses `your-mcp.example.com`; replace it with your real hostname.

The helper script can append the route to an existing Caddyfile:

```bash
uv run python scripts/activate_caddy.py \
  --hostname your-mcp.example.com \
  --expected-ip <server-ip>
```

Review the generated Caddyfile before using it on a production host.
