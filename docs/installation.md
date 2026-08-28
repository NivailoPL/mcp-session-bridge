# Installation

This guide sets up MCP Session Bridge for local development and for trying the demo workflow. It does not require a hosted domain or an MCP client.

For a persistent VPS with guided setup, systemd, Caddy, status reporting, and safe updates, use the [managed server installation](managed-installation.md) instead.

## Requirements

- Python 3.12 or newer
- `uv`
- Git

Install `uv` using the official instructions: https://docs.astral.sh/uv/

## Clone And Install

```bash
git clone https://github.com/NivailoPL/mcp-session-bridge.git
cd mcp-session-bridge
cp .env.example .env
uv sync
```

The default `.env.example` is configured for local development on `127.0.0.1:8787`.

## Generate Owner Credentials

The server uses the owner credentials for the OAuth login form and admin UI.

```bash
uv run python scripts/set_owner_password.py --username owner
```

The script prints a generated password and writes `BRIDGE_OWNER_PASSWORD_HASH` plus other missing defaults into `.env`.

For a one-time credential file:

```bash
uv run python scripts/set_owner_password.py \
  --username owner \
  --write-once-file secrets/owner-login.txt
```

`secrets/` is ignored by git except for its placeholder `.gitkeep`.

## Run The Demo

```bash
uv run python scripts/demo_session.py
```

Expected output:

```text
Created session: demo-...
Saved exchange #1: USER -> Claude
Saved exchange #2: USER -> GPT
Overview: 2 exchanges, 4 turns, 1 transcript chunk
Transcript written to: examples/output/demo-transcript.md
OK
```

The demo writes generated files to `examples/output/`, which is ignored by git.

## Run The Server

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload
```

Health check:

```bash
curl http://127.0.0.1:8787/healthz
```

Local MCP endpoint:

```text
http://127.0.0.1:8787/mcp
```

## Preview The Admin UI With Demo Data

To look at the admin UI with realistic content instead of an empty database:

```bash
uv run python scripts/demo_ui.py
```

The script rebuilds a throwaway demo database of ten English sessions across
four groups, one of which is marked sensitive, and serves the admin UI on
`http://127.0.0.1:8788/admin` with the credentials `owner` / `demo`. It uses a
separate port so it never collides with your own local instance, always writes
to the git-ignored `examples/output/demo.sqlite3`, and never reads or modifies
the real `data/` database.

Because the database is rebuilt on every run, it always matches the current
schema. Editing `admin-viewer.html` needs only a browser refresh; add
`--build-only` to regenerate the database without serving it.

## Run Tests

```bash
uv run pytest
```

If `.venv` was copied between machines or looks inconsistent, rebuild it:

```bash
rm -rf .venv
uv sync
uv run pytest
```
