# Limitations

MCP Session Bridge v0.1 is intentionally small and conservative.

## Explicit File Context Only

The bridge does not automatically ingest external files or directories, watch a project folder, or send local project material to models. A user or owner must explicitly upload supported text files through the admin UI, or a model must use `upload_session_file` or `upload_group_file` when asked.

Uploaded files are mutable runtime context. An owner can move, edit, or permanently delete them in the admin UI, which can change what a model is able to find during a conversation. Models are not automatically notified when these mutations happen; the current overview and file manifest must be checked deliberately.

## SQLite Storage

SQLite is simple and useful for a single deployment, but it is not designed for multi-region writes or high write concurrency. Back up the database before risky maintenance.

## Chunked Transcript Reads

Long transcripts must be fetched through all chunks reported by `get_session_overview`. A model should not assume the first chunk is the whole conversation.

Default limits:

```env
BRIDGE_TRANSCRIPT_CHUNK_MAX_LINES=180
BRIDGE_TRANSCRIPT_CHUNK_MAX_CHARS=12000
```

## Continuity Check

`get_last_speaker` lets a model skip re-fetching transcript chunks when it saved the last turn and is still in the same chat window. This is a best-effort optimization keyed on the self-declared `model_name`: the bridge cannot verify a model's real identity or whether it runs in the same window, so a wrong `model_name` or a fresh window can produce a misleading skip. When in doubt, fetch the chunks. `save_exchange` is still required on every turn.

## No Docker In v0.1

Docker documentation and images are not part of v0.1. Use local `uv`, Uvicorn, and the deployment templates.

## Client Differences

Remote MCP clients vary in how they display authentication, tool errors, and server instructions. Always verify the connector flow with `bridge_ping` and `auth_whoami`.

## Privacy Responsibility

The server stores full user messages and full assistant responses. Do not deploy it where untrusted users can write sensitive data unless you have appropriate access controls, backups, and retention policies.
