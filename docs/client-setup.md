# Client Setup

MCP Session Bridge is designed for remote MCP clients that support streamable
HTTP and OAuth. The exact UI varies by client, but the server exposes the same
resource URL everywhere.

Verified against chatgpt.com, claude.ai, grok.com, the Codex App, and the Claude
Code App. Any client that accepts a custom MCP connector should work; these are
the ones that have been run end to end.

## The Endpoint

After [a managed installation](managed-installation.md), your endpoint is your
domain plus `/mcp`:

```text
https://your-mcp.example.com/mcp
```

Setup already configured the domain, the certificate, the owner credentials and
the transport allowlist. Nothing in this document needs to be set by hand on a
managed installation; read on only if a client misbehaves and you want to know
what it is talking to.

Add that URL as a custom connector, authorise it, then paste
[project-prompt-template.md](project-prompt-template.md) into the project
instructions for conversations that should use the Bridge.

## Steps, Per Client

1. Find the custom connector or custom MCP server setting.
2. Give it the resource URL above. Some clients ask for a name as well.
3. Authorise. The client opens the Bridge login, you sign in as the owner, and
   the client stores the resulting token.
4. Confirm with `bridge_ping`, described under
   [First Tool Check](#first-tool-check).

A client that offers "add by URL" and one that offers a JSON snippet both need
the same single value: the resource URL.

## Running From A Checkout

For local development the endpoint is:

```text
http://127.0.0.1:8787/mcp
```

with the server running:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload
```

A checkout you host yourself sets its own base URL:

```env
BRIDGE_PUBLIC_BASE_URL=https://your-mcp.example.com
BRIDGE_RESOURCE_PATH=/mcp
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

A managed installation set these during setup; change them with
`mcp-bridge configure administrator`. In a checkout, use
`scripts/set_owner_password.py` to create or update the password hash.

## Transport Security Allowlist

The server enables DNS rebinding protection. A managed installation configures
this for your domain during setup. For a checkout, set the allowed hosts and
browser origins yourself:

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
