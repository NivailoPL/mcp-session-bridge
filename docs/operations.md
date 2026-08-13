# Operations

This page collects routine commands for running, inspecting, and maintaining MCP Session Bridge.
## Managed CLI

For a server installed with `./mcp-bridge setup`, use the global command from any directory.

Fast status:

```bash
mcp-bridge status
```

Deeper read-only diagnostics:

```bash
mcp-bridge doctor
```

Recent service logs:

```bash
mcp-bridge logs
mcp-bridge logs --follow
mcp-bridge logs --lines 250
```

Version and database schema:

```bash
mcp-bridge version
```

Check for a stable release without installing it:

```bash
mcp-bridge update --check
```

Install and verify the latest stable release:

```bash
mcp-bridge update
```

Restore the release and database from the most recent completed update:

```bash
mcp-bridge rollback
```

The authenticated admin page exposes the same summary under **Settings → Status**. An amber dot on both **Settings** and **Status** means a release is available. Updates remain CLI-only so a browser session cannot perform package, database, service, or rollback operations.

Managed data lives under `/var/lib/mcp-session-bridge`; dated setup and update backups live under `/var/backups/mcp-session-bridge`. Do not edit a version directory under `/opt/mcp-session-bridge/releases` in place.

## Codex App Server

The optional Codex companion has an independent lifecycle:

```bash
mcp-bridge codex-runtime status
mcp-bridge codex-runtime verify
mcp-bridge codex-runtime logs --lines 200
mcp-bridge codex-runtime repair
mcp-bridge codex-runtime disable
```

Enable it from `mcp-bridge setup` under **Codex app-server**, or non-interactively with `mcp-bridge codex-runtime enable`. Enabling downloads only the version and integrity-pinned npm artifact declared by the active Bridge release.

The Codex button in the Admin Viewer lazily checks the companion. Sign-in uses OpenAI device authorization and is separate from the Bridge owner login. The test conversation is ephemeral: messages and the Codex thread ID stay in page memory and disappear on refresh. Codex unavailability does not change `/healthz` and must not interrupt normal sessions, search, settings, or MCP traffic.

Stop the companion and confirm failure isolation:

```bash
systemctl stop mcp-session-bridge-codex.service
curl http://127.0.0.1:8787/healthz
mcp-bridge codex-runtime status
```

Restart it with `mcp-bridge codex-runtime repair`. Do not copy, inspect, or include `/var/lib/mcp-session-bridge-codex/codex-home` in normal Bridge backups.



## Health Check

```bash
curl http://127.0.0.1:8787/healthz
```

Expected response:

```json
{"ok": true, "service": "mcp-session-bridge"}
```

## Session Audit CLI

List sessions:

```bash
uv run python scripts/session_audit.py list
```

Show a transcript as Markdown:

```bash
uv run python scripts/session_audit.py show <session_id>
```

Show speaker sequence:

```bash
uv run python scripts/session_audit.py show <session_id> --format sequence
```

Show JSON:

```bash
uv run python scripts/session_audit.py show <session_id> --format json
```

## Offline Viewer

Export data:

```bash
uv run python scripts/session_audit.py export-viewer --output session-viewer-data.json
```

Continuously refresh the export:

```bash
uv run python scripts/session_audit.py export-viewer --output session-viewer-data.json --watch 5
```

Serve the viewer locally:

```bash
python3 -m http.server 8799 --bind 127.0.0.1
```

Open:

```text
http://127.0.0.1:8799/session-viewer.html
```

If you open `session-viewer.html` directly from disk, use its JSON load button and select `session-viewer-data.json`.

## Admin UI

The admin UI is served by the backend:

```text
/admin/sessions
```

It supports:

- viewing sessions and exchanges
- editing model name, user message, and assistant response
- soft-deleting exchanges from the active transcript
- restoring soft-deleted exchanges

## Calibrate MCP Tool-Result Size

Open **Settings → Transcript** in the admin UI. Enter a label for the harness being measured, choose a payload size and profile, then copy the generated instruction into that harness. The model must call `run_output_probe` through MCP and immediately follow it with `submit_output_probe_observation` using only canaries it can actually see.

The result history distinguishes checkpoint-complete delivery, tail truncation, removed head or middle content, non-contiguous delivery, verification failure, and pending runs. A pending run was created by the server but never acknowledged by the model; it may mean that the harness rejected or made the result unusable. A complete result validates the sampled checkpoints and final closed block rather than every unsampled character.

Recommendations use 75% of the largest verified payload for each harness/profile. When every MCP client shares one chunk configuration, use the smallest verified recommendation. Saving the global maximum characters and lines changes subsequent `get_session_overview` and `get_session_transcript_chunk` calls immediately. The first limit reached creates the next chunk.

The exact payload character count and the bridge's compact serialized-result character count are recorded separately. These are empirical transport measurements, not promises about a model tokenizer or future harness versions, so rerun calibration after material client changes.

## Backups

Back up at least:

- the SQLite database configured by `BRIDGE_DB_PATH`
- the production `.env`

Do not publish these backups.

Codex authentication is intentionally excluded. Prefer signing in again after disaster recovery instead of copying its token store.

## Rebuild The Virtual Environment

If imports fail after an interrupted install or a moved virtual environment:

```bash
rm -rf .venv
uv cache clean --force
uv sync --frozen
uv run pytest
```
