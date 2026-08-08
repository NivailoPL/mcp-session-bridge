# Changelog

All notable changes to MCP Session Bridge will be documented in this file.

This project follows a lightweight changelog format inspired by Keep a Changelog.

## [Unreleased]

### New Features

- Added an advanced Settings control for large MCP tool results. Optimized mode is the default and returns transcript chunks, output probes, and downloaded file contents once; Maximum compatibility restores the SDK's duplicated text and structured representations.
- Added an authenticated, CSRF-protected **Restart Bridge** action backed by a fixed systemd helper unit. The panel tracks configured versus active output mode and reminds owners to refresh tool lists in every harness after restart.
- Added a guided `bridge setup` flow for managed Linux VPS installations, including legacy database adoption, versioned releases, systemd/Caddy provisioning, and explicit `PASS`, `WAITING`, and `FAILED` states.
- Added `bridge status`, `doctor`, `logs`, `version`, `update`, and `rollback` operations with versioned machine-readable reports.
- Added **Settings → Status** with live health signals, cached diagnostics, update availability, and notification dots on both Settings entry points.

### Reliability and Operations

- Output probe runs now record their active tool-result mode, and chunk recommendations compare only runs made in the currently active mode.
- Database schema changes now use recorded migrations; managed services refuse implicit startup migrations.
- Stable updates verify GitHub release digests, preflight migrations on a copy, back up live SQLite, verify activation, and automatically restore the previous release and database on failure.
- Managed setup reports unresolved public DNS as `WAITING` while keeping the local service installed.

### Breaking Changes

- Removed the public MCP `list_sessions` tool. Continuing an existing conversation now requires an explicitly supplied `session_id`; clients without one must ask the user or create a new session.
- `list_session_files` now requires `session_id`. `download_session_file` now requires both `session_id` and `file_id`, and both tools expose only files belonging to that session or its current group.
- MCP clients must refresh or reconnect after deployment to load the updated tool schemas, and project prompts should be updated before the next handoff.

### Security

- Missing and session-inaccessible file IDs now return the same generic error to avoid revealing whether another file exists.
- Documented that unlisted sessions reduce accidental discovery but are not per-project access control; a client that knows a valid session ID retains access.

## [0.3.0] - 2026-07-14

### Highlights

- The admin dashboard now has one coherent file workspace for uploading, moving, reading, editing, and deleting session and group context without leaving the active conversation.
- Owners can search their complete context locally with BM25 or opt into private, group-scoped Hybrid retrieval backed by OpenAI embeddings and optional Cohere reranking.
- Search configuration, provider credentials, index lifecycle, privacy boundaries, processing estimates, and build status now live in one dedicated Settings experience.

### New Features

- Replaced the admin page's top file strip and nested detail overlay with one always-available file workspace opened from the conversation rail.
- Added explicit admin uploads, OS drag-and-drop, and session/group drag-and-drop moves while preserving each file's identity.
- Added text and Markdown editing with stale-write protection, plus a clearly warned permanent-delete flow.
- Added an admin-only context search overlay with local BM25 and optional Hybrid vector search.
- Added General, Search, and API tabs to the settings overlay, including encrypted OpenAI and Cohere credentials.
- Added configurable conversation/session-file/group-file ingestion, token chunking, overlap, candidate limits, and refresh thresholds.
- Added cancellable, generation-safe vector index builds with explicit group consent for external processing and a separate local-only result lane.
- Added optional Cohere reranking after BM25 and vector candidates are combined with reciprocal-rank fusion.
- Added local rebuild estimates for document count, chunk count, embedding tokens, overlap overhead, and model-specific OpenAI cost.

### Quality of Life

- File formats now have prominent labels, and the workspace remains usable with the keyboard, file picker, or touch-sized controls when a session has no messages yet.
- Admin and MCP read surfaces now reconcile against the same current file manifest after owner uploads, moves, edits, or deletes.
- Search moved from the old inline field into a dedicated overlay with Basic and Hybrid modes, cancellable requests, source metadata, highlighted snippets, and a focused result detail view.
- Vector index controls now use a dedicated action bar with contextual Build, Rebuild, Stop, and Delete states, animated progress, completion time, and a clear ready indicator.
- Settings, Files, Search, and group overlays can be dismissed by clicking their backdrop; the file workspace still protects unsaved drafts before closing.

### Reliability and Operations

- Vector rebuilds run on the server independently of the Settings window and atomically promote a completed generation, keeping the previous index available if a rebuild is stopped or fails.
- Source revisions track conversation and file changes, while configurable message and maximum-wait thresholds control when automatic rebuilds are scheduled.
- Provider failures are retried, Cohere failures fall back to Hybrid ordering with a warning, and interrupted builds preserve the last usable generation.

### Security

- Admin file mutations require an authenticated owner session and CSRF protection, enforce bounded UTF-8 text uploads, and do not expose new MCP mutation tools.
- Unselected groups never enter embedding or reranking provider payloads; Basic search remains entirely local and RAG is disabled by default.
- Search remains an admin-only capability in this release; no context-search tools are exposed to connected models.

## [0.2.0] - 2026-07-07

### Highlights

- The admin room got a serious upgrade: sessions can now be grouped, filtered, renamed, inspected, and backed by reusable text files without leaving the dashboard.
- Models now have a faster continuity check with `get_last_speaker`, so a returning assistant can avoid replaying the whole transcript when it already saved the latest turn.
- Durable notes have been consolidated around session and group files, turning summaries, plans, and reusable context into one cleaner inventory system.

### New Features

- Added configurable bridge display timezones for admin and MCP responses, including ready-to-show response timestamps from `save_exchange`.
- Added session groups, plus session-level and group-level uploaded text files for reusable context.
- Added admin visibility for uploaded files, including file lists and file detail views.
- Added `get_last_speaker` to report the most recent saved model turn for same-chat continuity checks.
- Added manual session title editing in the admin API.
- Added optional AI-powered session renaming from the first user message, with encrypted API key storage and configurable model selection.
- Added estimated token counts for each exchange and total token counts on session detail responses.

### Quality of Life

- Session lists now sort by the latest real conversation turn instead of admin metadata updates, keeping active conversations where users expect them.
- The model prompt template now documents the fast `get_last_speaker` path and the full transcript fetch fallback more consistently.
- The project prompt template was condensed to stay friendlier to ChatGPT-sized setup windows.
- Summary, plan, note, and reusable-context guidance now points models to `upload_session_file` and `upload_group_file`.

### Fixes and Tuning

- OAuth authorization now accepts public resource and issuer aliases while preserving the canonical MCP resource internally.
- Public docs no longer describe summaries as a separate storage system when uploaded text files are the durable context path.
- Deployment and setup docs no longer require `BRIDGE_SUMMARIES_DIR`.

### Retired

- Removed the older Markdown-only `save_session_summary` and `list_session_summaries` tools in favor of the more general session/group file workflow.
- Removed the filesystem-backed `SessionSummaryStore` now that durable notes live through uploaded text files.

## [0.1.0] - 2026-06-01

### Added

- Remote MCP server using FastMCP and streamable HTTP.
- OAuth authorization-code + PKCE flow with dynamic client registration.
- SQLite-backed sessions and full user/model exchange storage.
- Chunked transcript reads through `get_session_overview` and `get_session_transcript_chunk`.
- Session and group text file storage for reusable context.
- MCP server-level instructions for the core model workflow.
- Offline transcript viewer export flow.
- Authenticated admin UI for transcript correction.
- Local demo script and sample artifacts.
- Public installation, client setup, model instruction, deployment, security, limitations, and operations docs.

### Changed

- Renamed public project identity to MCP Session Bridge.
- Reworked public docs and examples in English.
- Moved private deployment-specific configuration into `.env`.

### Security

- Added security notes and private-reporting guidance.
- Kept secrets, databases, summaries, and generated viewer exports out of git by default.
