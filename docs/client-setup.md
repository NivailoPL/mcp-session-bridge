# Client Setup

MCP Session Bridge is designed for remote MCP clients that support streamable HTTP and OAuth. The exact UI varies by client, but the server exposes the same resource URL everywhere.

## Local Endpoint

For local development:

```text
http://127.0.0.1:8787/mcp
```

The local server must be running:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload
```

## Hosted Endpoint

For a hosted deployment, set:

```env
BRIDGE_PUBLIC_BASE_URL=https://your-mcp.example.com
BRIDGE_RESOURCE_PATH=/mcp
```

Then the remote MCP endpoint is:

```text
https://your-mcp.example.com/mcp
```

## OAuth Flow

The server supports:

- OAuth authorization-code flow with PKCE
- Dynamic client registration
- Bearer tokens scoped with `BRIDGE_SCOPE`, default `bridge`

The client should discover metadata from:

```text
/.well-known/oauth-authorization-server
/.well-known/oauth-protected-resource
```

The login form uses:

```env
BRIDGE_OWNER_USERNAME
BRIDGE_OWNER_PASSWORD_HASH
```

Use `scripts/set_owner_password.py` to create or update the password hash.

## Transport Security Allowlist

The server enables DNS rebinding protection. Configure allowed hosts and browser origins for your deployment:

```env
BRIDGE_TRANSPORT_ALLOWED_HOSTS=your-mcp.example.com,your-mcp.example.com:443,127.0.0.1:8787,localhost:8787
BRIDGE_TRANSPORT_ALLOWED_ORIGINS=https://your-mcp.example.com,https://claude.ai,https://chatgpt.com,https://chat.openai.com
```

For local-only testing, the `.env.example` defaults are enough.

## First Tool Check

After connecting a client, call:

```text
bridge_ping
auth_whoami
```

`bridge_ping` proves the authenticated MCP tool path works. `auth_whoami` shows which OAuth client is attached to the current token.
