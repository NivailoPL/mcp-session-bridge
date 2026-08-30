# scripts/

Development and one-off setup helpers. These are for a checkout, not for a
managed installation — an installed Bridge is driven by `./mcp-bridge`
(see [docs/managed-installation.md](../docs/managed-installation.md)).

| | |
| --- | --- |
| `demo_session.py` | Create a small sample session and transcript. Runs in CI as a smoke test. |
| `demo_ui.py` | Rebuild a throwaway demo database and serve the admin UI against it on port 8788. Never touches `data/`. |
| `set_owner_password.py` | Generate the owner password hash for a local `.env`. |
| `activate_caddy.py` | Append a Bridge route to an existing Caddyfile, for the manual deployment path. |

For reading a real database, see [tools/](../tools/README.md).
