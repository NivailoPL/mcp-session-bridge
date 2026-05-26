# MCP Session Bridge

Auth-first spike for testing Claude.ai and ChatGPT remote MCP connectors over HTTPS.

Endpoint: `https://mcp.panchmurka.wtf/mcp`

This spike intentionally stores only probe values. Do not use it for sensitive session
content until both clients have passed OAuth and tool-call testing.

After `mcp.panchmurka.wtf` resolves to `89.167.57.190`, activate the Caddy route:

```bash
cd /root/mcp-session-bridge
.venv/bin/python scripts/activate_caddy.py
```
