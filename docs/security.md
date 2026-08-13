# Security

MCP Session Bridge stores conversation transcripts and OAuth credentials. Treat the deployment as sensitive infrastructure.

## Secrets

Do not commit:

- `.env` or environment-specific variants such as `.env.local`
- anything under `data/`, including SQLite databases, WAL/SHM files, context packs, search documents, vector chunks, and legacy session summaries
- local database or backup files created elsewhere in the repository
- generated viewer exports
- private keys, certificates, and files under `secrets/`

The repository `.gitignore` excludes these by default. `.env.example` is the only environment file intended for source control and must contain placeholders only.

OpenAI and Cohere API keys saved through the admin UI are encrypted before they are stored in SQLite. Conversation text, uploaded files, BM25 documents, vector chunks, and embeddings are runtime data and are not separately encrypted by the application, so protect the database and every backup with host-level access controls.

Codex ChatGPT credentials follow a different boundary. Codex owns them under `/var/lib/mcp-session-bridge-codex/codex-home` with private permissions. They are never copied into Bridge SQLite, `bridge.env`, releases, Git, normal database exports, or application logs. Do not add this directory to ordinary backups; reauthenticate after disaster recovery.

Before pushing a release, review both the staged file list and ignored runtime files:

```bash
git diff --cached --name-only
git status --short --ignored
```

Never use `git add -f` for a runtime data or secret path. Keep production backups outside the repository and restrict the runtime data directory to the service account.

## OAuth And Tokens

- OAuth authorization codes and bearer tokens are stored as hashes.
- Refresh tokens can be revoked.
- MCP tool calls require a bearer token with the configured `BRIDGE_SCOPE`.
- Dynamic client registration is enabled for compatible remote MCP clients.

## Unlisted Sessions

Public MCP tools do not enumerate session IDs. A model without an ID must ask the user or create a new session, and file reads are limited to the named session plus its current group.

Unlisted sessions are protection against accidental discovery, not access control or a project-level ACL. The bridge does not know Claude Project or other client project boundaries. Any client with a valid bridge token and a known `session_id` can use the existing handoff tools for that session, so treat IDs as sensitive references and share them only with the intended project or conversation.

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

## Codex App Server Isolation

The optional App Server runs as `mcp-session-bridge-codex`, not as root or the Bridge service account. It receives no Bridge environment, database path, provider keys, Caddy listener, or repository workspace. A dedicated socket group permits only local Bridge-to-Codex transport; the browser talks to authenticated, CSRF-protected Bridge endpoints.

The v1 conversation always uses an empty dedicated working directory, an ephemeral thread, read-only sandboxing, approval policy `never`, no MCP servers, no web search, and disabled shell, unified-exec, apps, hooks, multi-agent, and remote-plugin features. The adapter rejects server tool requests and fails the turn if a tool or permission event appears. Future database access must be implemented as narrow audited application tools, not raw SQLite filesystem permission.

The App Server is an experimental versioned protocol. Every Bridge release pins one compatible Codex version and npm SHA-512 integrity. Runtime mismatch is rejected during initialization. Codex failure degrades only the Codex popup; Bridge `/healthz`, sessions, settings, search, and MCP remain independent.

## Reporting Issues

Follow the private reporting process in [`SECURITY.md`](../SECURITY.md). Do not disclose credentials, transcripts, uploaded files, or database extracts in public issues.
