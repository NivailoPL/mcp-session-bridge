# MCP Session Bridge

Auth-first spike for testing Claude.ai and ChatGPT remote MCP connectors over HTTPS.

Endpoint: `https://mcp.panchmurka.wtf/mcp`

This MVP stores OAuth records, diagnostic probes, session metadata, and full
saved session exchanges. Use `magic-smoke` before adding sensitive context.

Session context packs live outside this repository in `/root/ww-context-packs`.
The first safe smoke-test pack is `magic-smoke`.

After `mcp.panchmurka.wtf` resolves to `89.167.57.190`, activate the Caddy route:

```bash
cd /root/mcp-session-bridge
.venv/bin/python scripts/activate_caddy.py
```
