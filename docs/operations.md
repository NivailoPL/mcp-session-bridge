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

### Every command

Most commands require root. The read-only ones do not: `version`, `status`,
`doctor`, `codex-runtime status`, `codex-runtime verify`, and `setup --dry-run`.
Every command that reports a result accepts `--json` for machine-readable output.

| Command | What it does |
| --- | --- |
| `setup` | Install, resume, or adopt an installation. See [managed-installation.md](managed-installation.md). |
| `configure [domain\|administrator]` | Change the domain or the owner credentials. |
| `status` | Fast operational report. `--refresh` also checks for a new stable release first. |
| `doctor` | Deeper read-only diagnostics. `--refresh` behaves as it does for `status`. Exits non-zero when the report is anything but healthy. |
| `logs` | Service logs through `journalctl`. `--follow`, `--lines N`. |
| `version` | Bridge version, database schema version, and the release commit. |
| `migrate` | Apply database migrations explicitly. `--db PATH` targets a database outside the managed layout. |
| `export` | Markdown archive of every conversation and attachment. See below. |
| `database …` | Inspect and manage the database. See [Database](#database). |
| `service …` | Inspect or control the systemd service. See [Service control](#service-control). |
| `codex-runtime …` | The optional Codex companion. See [Codex App Server](#codex-app-server). |
| `installation inspect` | Print the recorded installation metadata. |
| `installation uninstall` | Remove the installation. See [Uninstalling](#uninstalling). |
| `deploy` | Deploy the current committed checkout, for development hosts. `--yes` skips the prompt; `--allow-downgrade` permits a non-fast-forward checkout. |
| `update [--check]` | Install the latest stable release, or only report whether one exists. |
| `rollback` | Restore the release and database from the most recent completed update. |

## Database

All of these run against the managed database and all require root.

Read-only:

```bash
mcp-bridge database inspect
mcp-bridge database verify
```

`inspect` reports size, session and exchange counts and schema version.
`verify` runs an integrity check and exits non-zero when the database is
unhealthy.

Apply migrations, the same work `mcp-bridge migrate` does:

```bash
mcp-bridge database migrate
```

Take a verified copy. The destination must not already exist, and the result is
checked before the command reports success:

```bash
mcp-bridge database backup --output /root/bridge-backup-2026-09-01.sqlite3
```

The backup contains full transcripts. Treat it as sensitive.

### Destructive database commands

These three replace the live database. Each stages the new database in a
temporary file, verifies it, takes a safety backup under
`/var/backups/mcp-session-bridge`, and swaps it in under an operation lock while
the service is stopped. Each prompts unless you pass `--yes`, and a
non-interactive run without `--yes` is refused rather than assumed.

```bash
mcp-bridge database optimize                      # PRAGMA optimize + VACUUM
mcp-bridge database import --replace <source>     # replace with another Bridge database
mcp-bridge database reset --yes                   # replace with an empty database
```

`reset` destroys every conversation. `import --replace` reports the session
count on both sides before asking. Both are recoverable only from the safety
backup they just took, so verify that backup exists before relying on it.

## Service control

```bash
mcp-bridge service inspect
mcp-bridge service verify
mcp-bridge service start
mcp-bridge service stop
mcp-bridge service restart
mcp-bridge service logs --lines 200
```

`inspect` and `verify` report the same state; `verify` exits non-zero when the
service is not active, which makes it the one to use in a scripted check.

The admin UI can request a restart under **Settings**, through a narrowly scoped
helper unit that accepts no service name from the request. Every other service
operation is CLI-only.

## Uninstalling

Always look at the plan first:

```bash
mcp-bridge installation uninstall --dry-run
```

That prints every path it would remove and changes nothing. To proceed:

```bash
mcp-bridge installation uninstall --output /root/bridge-final-export.sqlite3
```

Without `--remove-data` the database and other data under
`/var/lib/mcp-session-bridge` are left in place. `--remove-data` removes them
too, so take `--output` first. A non-interactive run requires `--yes`.

## Markdown Conversation Export

Create a timestamped conversation archive on the VPS:

```bash
mcp-bridge export
```

The command prints the absolute destination under `/var/lib/mcp-session-bridge/exports`. For automation or a different new destination:

```bash
mcp-bridge export --json
mcp-bridge export --output /safe/new/archive-directory
```

The export contains one Markdown file per session, grouped by the current session group, plus stored text files and original PDFs. It also includes sensitive sessions, raw excluded or masked exchanges, and the exchange edit/delete/restore/mask history retained by SQLite. It does not include OAuth records, provider keys, application settings, search indexes, or Graph data.

The authenticated **Settings → Database** button runs this same server-side operation. It reports the path but provides no browser download, file listing, path selector, or archive-reading endpoint. Export directories are not automatically rotated; inspect free space and delete obsolete archives over SSH.

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
uv run python tools/session_audit.py list
```

Show a transcript as Markdown:

```bash
uv run python tools/session_audit.py show <session_id>
```

Show speaker sequence:

```bash
uv run python tools/session_audit.py show <session_id> --format sequence
```

Show JSON:

```bash
uv run python tools/session_audit.py show <session_id> --format json
```

## Offline Viewer

The viewer and its exporter live together in `tools/`. The export is written
next to the HTML by default, which is where the page fetches it from.

Export data:

```bash
uv run python tools/session_audit.py export-viewer
```

Continuously refresh the export:

```bash
uv run python tools/session_audit.py export-viewer --watch 5
```

Serve the viewer locally:

```bash
python3 -m http.server 8799 --bind 127.0.0.1 --directory tools
```

Open:

```text
http://127.0.0.1:8799/session-viewer.html
```

If you open `tools/session-viewer.html` directly from disk, use its JSON load button and select the exported `session-viewer-data.json`.

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

On a managed installation, take a verified copy with the CLI rather than by
copying the file yourself. A live SQLite database has WAL and SHM sidecars, and
`cp` while the service is running can capture a torn state; `database backup`
uses the SQLite backup API and verifies the result before reporting success:

```bash
mcp-bridge database backup --output /root/bridge-backup-2026-09-01.sqlite3
```

Bridge also writes dated backups on its own, under
`/var/backups/mcp-session-bridge`, before every update and before every
destructive database command. Those are a safety net for an operation that just
happened, not a backup schedule -- nothing prunes or offsites them for you.

Back up alongside the database:

- `/etc/mcp-session-bridge/bridge.env`, which holds the secret key and the owner
  password hash. Without it, existing OAuth tokens cannot be validated.
- the Caddy configuration, if you changed it by hand.

For a checkout rather than a managed installation, the equivalents are the
database at `BRIDGE_DB_PATH` and the local `.env`.

A backup contains every transcript in full. Do not publish it, and keep it
somewhere you would be willing to keep the conversations themselves.

For a portable, readable archive rather than a restorable database, use
[`mcp-bridge export`](#markdown-conversation-export).

Codex authentication is intentionally excluded. Prefer signing in again after disaster recovery instead of copying its token store.

## Rebuild The Virtual Environment

If imports fail after an interrupted install or a moved virtual environment:

```bash
rm -rf .venv
uv cache clean --force
uv sync --frozen
uv run pytest
```
