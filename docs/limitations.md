# Limitations

MCP Session Bridge v0.1 is intentionally small and conservative.

## Explicit File Context Only

The bridge does not automatically ingest external files or directories, watch a project folder, or send local project material to models. A user or owner must explicitly upload supported text files or PDFs through the admin UI, or a model must use the matching text/PDF upload tool when asked.

Uploaded files are mutable runtime context. An owner can move, edit, or permanently delete them in the admin UI, which can change what a model is able to find during a conversation. Models are not automatically notified when these mutations happen; the current overview and file manifest must be checked deliberately.

## PDF Text Extraction Without OCR

The bridge preserves the original PDF and extracts an existing text layer for MCP reads and RAG search. OCR is not supported. Scanned or image-only PDFs may still be uploaded, previewed, downloaded, moved, and deleted, but they are marked as having no extractable text and are not indexed for search. Encrypted/password-protected PDFs are rejected.

PDF limits are 20 MB through the admin UI, 10 MB through MCP, 500 pages, and 5 MB of extracted UTF-8 text. Durable PDF storage has a 1 GB global default quota, configurable with `BRIDGE_PDF_STORAGE_MAX_BYTES`.

The offline admin demo remains text-only and rejects PDF uploads explicitly.

## SQLite Storage

SQLite is simple and useful for a single deployment, but it is not designed for multi-region writes or high write concurrency. Back up the database before risky maintenance.

## Chunked Transcript Reads

Long transcripts must be fetched through all chunks reported by `get_session_overview`. A model should not assume the first chunk is the whole conversation.

Default limits:

```env
BRIDGE_TRANSCRIPT_CHUNK_MAX_LINES=180
BRIDGE_TRANSCRIPT_CHUNK_MAX_CHARS=12000
```

The owner can override both values at runtime in **Settings → Transcript**. The first limit reached wins. Runtime values are stored in SQLite and take effect for subsequent overview/chunk calls without restarting the service.

Harness capacity is empirical and can change without notice. The output-probe tools verify unpredictable markers at the start, quarters, middle, end, and last fully closed block. A `complete` result is therefore strong evidence of intact delivery at those sampled positions and at the tail, not a byte-for-byte proof for every unsampled character. The bridge also cannot prove why a harness rejected a result when the model never submits an observation; such runs remain pending. Character counts are exact for the generated payload, while token counts are deliberately not treated as exact because clients and models can use different tokenizers and wrappers.

## Continuity Check

`get_last_speaker` lets a model skip re-fetching transcript chunks when it saved the last turn and is still in the same chat window. This is a best-effort optimization keyed on the self-declared `model_name`: the bridge cannot verify a model's real identity or whether it runs in the same window, so a wrong `model_name` or a fresh window can produce a misleading skip. When in doubt, fetch the chunks. `save_exchange` is still required on every turn.

## No Docker In v0.1

Docker documentation and images are not part of v0.1. Use local `uv`, Uvicorn, and the deployment templates.

## Client Differences

Remote MCP clients vary in how they display authentication, tool errors, and server instructions. Always verify the connector flow with `bridge_ping` and `auth_whoami`.

## Privacy Responsibility

The server stores full user messages and full assistant responses. Do not deploy it where untrusted users can write sensitive data unless you have appropriate access controls, backups, and retention policies.

Sessions are unlisted through MCP, and session-scoped file reads cannot select an arbitrary group. This reduces accidental discovery but is not access control: a client that already knows a valid `session_id` can still read that session's transcript and files visible through its current group. The bridge does not understand boundaries created by Claude Projects or other MCP clients, does not issue per-project tokens, and does not prevent access through a guessed or leaked ID.
