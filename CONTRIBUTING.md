# Contributing

Thanks for taking a look at MCP Session Bridge.

This project is early, so small, focused contributions are the easiest to review. Issues, bug reports, docs improvements, and pull requests are welcome.

## Development Setup

```bash
git clone https://github.com/NivailoPL/mcp-session-bridge.git
cd mcp-session-bridge
cp .env.example .env
uv sync
uv run python scripts/demo_session.py
uv run pytest
```

To preview the admin UI with realistic demo content, run
`uv run python scripts/demo_ui.py` and open `http://127.0.0.1:8788/admin`
(`owner` / `demo`). See [docs/installation.md](docs/installation.md) for details.

## Pull Requests

Before opening a pull request:

1. Keep the change focused.
2. Update docs or examples when behavior changes.
3. Add or update tests for user-facing behavior.
4. Run `uv run pytest`.
5. Make sure no secrets, private hostnames, database files, or generated exports are committed.

## Project Conventions

- Use English for public docs, examples, tests, and user-facing strings.
- Keep MCP tool behavior explicit and easy for models to follow.
- Prefer simple storage and deployment assumptions.
- Do not add Docker support until the project intentionally scopes it in.

## Reporting Security Issues

Please do not report security issues in public issues. See [SECURITY.md](SECURITY.md).
