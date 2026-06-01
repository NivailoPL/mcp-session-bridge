# Security

MCP Session Bridge stores conversation transcripts and OAuth credentials. Treat the deployment as sensitive infrastructure.

## Secrets

Do not commit:

- `.env`
- SQLite databases
- session summaries that contain private data
- generated viewer exports
- files under `secrets/`

The repository `.gitignore` excludes these by default.

## OAuth And Tokens

- OAuth authorization codes and bearer tokens are stored as hashes.
- Refresh tokens can be revoked.
- MCP tool calls require a bearer token with the configured `BRIDGE_SCOPE`.
- Dynamic client registration is enabled for compatible remote MCP clients.

## Owner Login

The owner login protects the OAuth login form and admin UI. Generate the password hash with:

```bash
uv run python scripts/set_owner_password.py --username owner
```

Use a long generated password for hosted deployments.

## Transport Security

DNS rebinding protection is enabled. Configure:

```env
BRIDGE_TRANSPORT_ALLOWED_HOSTS=your-mcp.example.com,your-mcp.example.com:443
BRIDGE_TRANSPORT_ALLOWED_ORIGINS=https://your-mcp.example.com,https://claude.ai,https://chatgpt.com,https://chat.openai.com
```

Keep localhost values only when you need local development access.

## Admin UI

The admin UI can edit, soft-delete, and restore transcript exchanges. Mutations require login and CSRF protection.

Deleted exchanges remain in SQLite with `deleted_at` and `deleted_reason`, but active transcript reads skip them.

## Reporting Issues

Before publishing broadly, add a `SECURITY.md` file with a contact address for vulnerability reports. Until then, do not ask users to disclose security issues in public issues.
