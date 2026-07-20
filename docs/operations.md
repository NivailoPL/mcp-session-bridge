# Operations

This page collects routine commands for running, inspecting, and maintaining MCP Session Bridge.

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

## Rebuild The Virtual Environment

If imports fail after an interrupted install or a moved virtual environment:

```bash
rm -rf .venv
uv cache clean --force
uv sync --frozen
uv run pytest
```
