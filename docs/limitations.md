# Limitations

MCP Session Bridge v0.1 is intentionally small and conservative.

## Not A File Context Store

The bridge does not automatically send project files, notes, PDFs, or other user context to models. Users should provide that context directly in the chat client.

The bridge stores conversation history and optional Markdown session summaries.

## SQLite Storage

SQLite is simple and useful for a single deployment, but it is not designed for multi-region writes or high write concurrency. Back up the database before risky maintenance.

## Chunked Transcript Reads

Long transcripts must be fetched through all chunks reported by `get_session_overview`. A model should not assume the first chunk is the whole conversation.

Default limits:

```env
BRIDGE_TRANSCRIPT_CHUNK_MAX_LINES=180
BRIDGE_TRANSCRIPT_CHUNK_MAX_CHARS=12000
```

## No Docker In v0.1

Docker documentation and images are not part of v0.1. Use local `uv`, Uvicorn, and the deployment templates.

## Client Differences

Remote MCP clients vary in how they display authentication, tool errors, and server instructions. Always verify the connector flow with `bridge_ping` and `auth_whoami`.

## Privacy Responsibility

The server stores full user messages and full assistant responses. Do not deploy it where untrusted users can write sensitive data unless you have appropriate access controls, backups, and retention policies.
