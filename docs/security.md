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

## Installation Signing Key

`BRIDGE_SECRET_KEY` must be randomly generated, contain at least 32 characters,
and must not be the placeholder from `.env.example`. The application rejects an
invalid key before opening the database. Length validation cannot prove that a
manually chosen key is unpredictable; use the generator rather than a phrase.

For a new installation, `scripts/set_owner_password.py` replaces an empty or
example key with a random 48-byte URL-safe secret. It preserves an existing valid
key when changing the owner password, and retains other environment settings.
The managed installer generates a key for a fresh configuration but refuses to
adopt an existing configuration with an invalid key.

**Existing installations:** this validation can prevent startup after an update
if the old key is a placeholder or shorter than 32 characters. Check the key
locally without copying it into logs or support messages before updating.
The password script refuses automatic replacement of an empty/example key when
the configured database already exists. An invalid custom key is also rejected.

Recover such an installation during planned maintenance: restrict public access,
keep a private backup of the database and configuration, and prepare to re-enter
the provider API keys from their original source (or migrate their ciphertext
with the old key in a controlled offline process). Generate a fresh random secret,
update the authoritative service configuration, restart, re-enter provider keys,
and reconnect OAuth clients. Verify login and provider functionality before
restoring public access. Do not publish the backup or either secret.

Changing this key invalidates admin cookies and existing OAuth tokens and makes
previously encrypted provider keys unreadable with the new key. Changing only
the owner password does not rotate it. No automatic database re-encryption or
live key rotation is performed by the setup tools.

## OAuth And Tokens

- OAuth authorization codes and bearer tokens are stored as hashes.
- MCP tool calls require a bearer token with the configured `BRIDGE_SCOPE`.
- Dynamic client registration is enabled for compatible remote MCP clients.
- Settings › Connectors lists every harness the owner signed in. Disconnect deletes the client with all its codes and tokens, so its access ends at once.
- Refresh tokens rotate on every use. Expired codes and tokens, and clients that registered but never signed in within a day, are purged every ten minutes.

### Who a client is

A dynamically registered client is identified only by its redirect URI, the address the authorization code is delivered to. The name it registers is chosen by the client and is never trusted. Registration accepts:

- `https` URIs on internet hosts (web harnesses such as claude.ai or chatgpt.com),
- `http` or `https` URIs on this computer (`127.0.0.1`, `::1`, `localhost`) for desktop and CLI harnesses,
- custom app schemes such as `cursor://`.

Plain `http` to another host, URIs with a fragment, and `javascript:`, `data:`, `file:` and similar schemes are refused. A client may register at most ten redirect URIs.

The sign-in page is a consent screen. It shows the host the code will be sent to, whether that host has been approved before, and the client's self-declared name marked as unverified. It cannot be framed and sends no referrer.

A browser that signed in to Bridge with the owner password, on the admin login or on this page, sees only **Allow** and **Deny** for the next 12 hours, the same lifetime as an admin session. This rests on a separate signed cookie scoped to `/oauth` (`HttpOnly`, `SameSite=Lax`, `Secure` over HTTPS). Allow also requires a token bound to that cookie and to the one pending sign-in, so it cannot be submitted from another site or replayed for another client. Signing out of the admin clears the cookie. Deny returns `access_denied` to the harness and ends the sign-in.

### Connection lock

Anyone can register a client and send the owner a sign-in link for it on the owner's own Bridge. Without further protection, signing in on such a link hands the attacker a token. Settings › Connectors offers a lock against this, off by default and recommended:

- When it is on, a client whose redirect URI is an internet host can sign in for the first time only while the owner has a connection window open (10 minutes, 30 minutes or an hour). Outside the window the sign-in page explains how to open one and stores nothing.
- Clients on this computer and custom app schemes are not affected: an authorization code sent there cannot reach anyone else.
- A client that has signed in once is approved and is not affected later. Clients that already held tokens when this feature was installed are approved automatically.

The lock protects against links the owner did not start. It does not protect a harness provider from itself; disconnect any harness you no longer trust.

### Sign-in limits

The connector sign-in and the admin login check the same owner password and share one limiter: five wrong passwords per address within 15 minutes, and 30 across all addresses within a minute, after which sign-in answers 429 until the window passes. The password check runs off the event loop, at most two at a time. Client registration (20 per address per 10 minutes) and authorization (60) are limited too, and at most 500 sign-ins may be pending. Request bodies on the sign-in endpoints are capped at 16 KB.

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

## Markdown Conversation Exports

Markdown archives are deliberately written only to the VPS under `/var/lib/mcp-session-bridge/exports` by default. The Admin UI has one authenticated, CSRF-protected trigger with no path or file input; it returns only result metadata and the server path. There is no HTTP route for listing, reading, or downloading an export.

Treat every archive as highly sensitive. It contains sensitive-group sessions, raw content hidden by exchange masking or exclusion, available exchange audit snapshots, uploaded text, and original PDFs. Directory and file modes are restricted to `0700` and `0600`. Copy archives only over an authenticated administrative channel, store them encrypted when they leave the VPS, and remove obsolete copies manually.

## Codex App Server Isolation

The optional App Server runs as `mcp-session-bridge-codex`, not as root or the Bridge service account. It receives no Bridge environment, database path, provider keys, Caddy listener, or repository workspace. A dedicated socket group permits only local Bridge-to-Codex transport; the browser talks to authenticated, CSRF-protected Bridge endpoints.

The v1 conversation always uses an empty dedicated working directory, an ephemeral thread, read-only sandboxing, approval policy `never`, no MCP servers, no web search, and disabled shell, unified-exec, apps, hooks, multi-agent, and remote-plugin features. The adapter rejects server tool requests and fails the turn if a tool or permission event appears. Future database access must be implemented as narrow audited application tools, not raw SQLite filesystem permission.

The App Server is an experimental versioned protocol. Every Bridge release pins one compatible Codex version and npm SHA-512 integrity. Runtime mismatch is rejected during initialization. Codex failure degrades only the Codex popup; Bridge `/healthz`, sessions, settings, search, and MCP remain independent.

## Reporting Issues

Follow the private reporting process in [`SECURITY.md`](../SECURITY.md). Do not disclose credentials, transcripts, uploaded files, or database extracts in public issues.
