# Changelog

All notable changes to MCP Session Bridge will be documented in this file.

This project follows a lightweight changelog format inspired by Keep a Changelog.

## [Unreleased]

## [0.5.0] - 2026-08-17

### Highlights

- The Sessions dashboard is easier to scan with compact rows, date separators, timestamps, and clearer selected-session context.
- Admin surfaces now share a complete project identity, icon set, and responsive Pearl Gradient navigation.
- Managed updates are safer: restart flows wait for Bridge health, and migrations run with code from the prepared release.
- Runtime status and release metadata now report database compatibility from the same schema source of truth.

### Quality of Life

- Redesigned the Sessions list around compact conversation rows, chronological date buckets, and selected-session metadata.
- Added a reusable brand asset pack plus consistent headers and navigation across desktop, tablet, and mobile layouts.

### Reliability and Operations

- Managed setup now waits for Bridge health after restarting the service.
- Prepared-release migrations now execute with the code and environment from the release being activated.
- Status output now distinguishes its response format version from the SQLite database schema version.
- Release manifests now derive their database schema from the runtime constant and verify the complete manifest shape in regression tests.

### Included commits

- [`d3ac68b`](https://github.com/NivailoPL/mcp-session-bridge/commit/d3ac68b9987d3fba9694c84351dcb62502d0262e) feat(codex): add isolated app-server adapter and admin API
- [`0d3c9a1`](https://github.com/NivailoPL/mcp-session-bridge/commit/0d3c9a1e0a1ab4e1681a9c96eea94692f852c0d3) feat(admin): add ephemeral Codex chat popup
- [`4712d86`](https://github.com/NivailoPL/mcp-session-bridge/commit/4712d86d96b814283a042d8bee5d32cb1561cbb3) feat(setup): manage pinned Codex sidecar lifecycle
- [`7ef34f5`](https://github.com/NivailoPL/mcp-session-bridge/commit/7ef34f5611de787ec6b5e78db836ae16bac69393) fix(review): harden Codex runtime lifecycle
- [`12d53bd`](https://github.com/NivailoPL/mcp-session-bridge/commit/12d53bd41ca0bebb5bfa8b99cd61697ffb5796bc) fix(codex): disable unsupported websocket compression
- [`9afbd95`](https://github.com/NivailoPL/mcp-session-bridge/commit/9afbd951936b7544d9da26df86f5fa8ac753cbfe) fix(setup): wait for Bridge health after restart
- [`9ed8d12`](https://github.com/NivailoPL/mcp-session-bridge/commit/9ed8d12e4f9aab9635d178d468121133325bf91b) fix(codex): allow Bridge socket-group traversal
- [`3d40e3b`](https://github.com/NivailoPL/mcp-session-bridge/commit/3d40e3bb68327f1497fd662b373f8089574235d3) fix(codex): restore shared socket directory mode
- [`a1393f3`](https://github.com/NivailoPL/mcp-session-bridge/commit/a1393f31f408be2124dff3a33d6c211965f944ab) fix(codex): stop sidecar cleanly
- [`f35714d`](https://github.com/NivailoPL/mcp-session-bridge/commit/f35714d081f14fbd0c791d6120a7347be64f4fe7) feat(admin): add project branding and icons
- [`b38bcc1`](https://github.com/NivailoPL/mcp-session-bridge/commit/b38bcc128f0d977600f0440c08116f68276901b1) feat(graph): add configurable extraction workspace
- [`2149d8d`](https://github.com/NivailoPL/mcp-session-bridge/commit/2149d8d254af6db892ce3c5719fa3fdf26adb954) fix(deploy): migrate with prepared release code
- [`55592d9`](https://github.com/NivailoPL/mcp-session-bridge/commit/55592d980facb52921caf5142446500828e8dbf4) fix(graph): make worker lifecycle process-safe
- [`1463a03`](https://github.com/NivailoPL/mcp-session-bridge/commit/1463a03441242ffa13629c5c66062496a6c93234) fix(graph): make extraction failures recoverable
- [`0195bc7`](https://github.com/NivailoPL/mcp-session-bridge/commit/0195bc7a20fa3584739a9ca26a7b0b7d6fc3b3bc) feat(graph): add destructive full rescan
- [`b28251c`](https://github.com/NivailoPL/mcp-session-bridge/commit/b28251c76b099cb391a5caef736a246045d25d5f) feat(graph): consolidate extraction workflow and Codex workspace
- [`73bc124`](https://github.com/NivailoPL/mcp-session-bridge/commit/73bc124fcaf3fc879174389c362d05544929282b) feat(admin): add Pearl Gradient workspace navigation
- [`6f950c8`](https://github.com/NivailoPL/mcp-session-bridge/commit/6f950c8b26275791a714754038f54ba07c7bce2f) feat(admin): add brand lockup to Sessions header
- [`c25d3cc`](https://github.com/NivailoPL/mcp-session-bridge/commit/c25d3cc2a2e57a0d7a3d6197a55ce46d35fc0620) Admin UI: visual polish for sessions and graph nav
- [`6ac0a30`](https://github.com/NivailoPL/mcp-session-bridge/commit/6ac0a30faeb427fa0dd324b62998ec87b9ed7747) Merge pull request #1 from NivailoPL/codex/admin-visual-polish
- [`73d014a`](https://github.com/NivailoPL/mcp-session-bridge/commit/73d014a9c082d2a8f0ebf9296e1471ce9775acbe) chore: remove graph, Codex, and branding
- [`c5a758d`](https://github.com/NivailoPL/mcp-session-bridge/commit/c5a758d471c9112009ed8080fd74f1140fee0eef) fix(storage): preserve schema v2 after graph removal
- [`7332cad`](https://github.com/NivailoPL/mcp-session-bridge/commit/7332cad556543901eef42aacfe8f620ca7e1347b) restore(admin): recover Graph workspace and branding
- [`c1d0fc4`](https://github.com/NivailoPL/mcp-session-bridge/commit/c1d0fc461388952544ba5d721bc9dbbedfab089c) fix(status): distinguish format version from database schema
- [`ed3de92`](https://github.com/NivailoPL/mcp-session-bridge/commit/ed3de92777f4f6cca29bd163bb1c8792443ee08f) feat(admin): redesign session list
- [`5751a5b`](https://github.com/NivailoPL/mcp-session-bridge/commit/5751a5b6027a69cfc612c1cb9ded252758761258) Merge pull request #2 from NivailoPL/codex/session-list-redesign
- [`54cbecb`](https://github.com/NivailoPL/mcp-session-bridge/commit/54cbecb33f1e2c9b64d5c0807dae657cf30b811c) fix(release): keep manifest schema in sync
- [`610d3ae`](https://github.com/NivailoPL/mcp-session-bridge/commit/610d3ae9ab045807c1173cf76a2dbe7038e2c1a9) feat(admin): gate experimental graph workspace
- [`57f7880`](https://github.com/NivailoPL/mcp-session-bridge/commit/57f78809cd97003e91f06e66556c53b9e39802de) Merge pull request #3 from NivailoPL/codex/disable-graph-release

[Compare changes: v0.4.1...v0.5.0](https://github.com/NivailoPL/mcp-session-bridge/compare/v0.4.1...v0.5.0)

## [0.4.0] - 2026-08-09

### Highlights

- Managed installations gained a resumable setup dashboard plus status, diagnostics, update, rollback, and recovery operations.
- Owners can tune large MCP tool-result compatibility and restart Bridge safely from the authenticated admin settings.
- Session reads are now unlisted by default, and file access is explicitly scoped to a supplied session.
- Stable updates verify release digests, preflight migrations, back up SQLite, health-check activation, and roll back failures.

### New Features

- Added an advanced Settings control for large MCP tool results. Optimized mode is the default and returns transcript chunks, output probes, and downloaded file contents once; Maximum compatibility restores the SDK's duplicated text and structured representations.
- Added an authenticated, CSRF-protected **Restart Bridge** action backed by a fixed systemd helper unit. The panel tracks configured versus active output mode and reminds owners to refresh tool lists in every harness after restart.
- Added a resumable, section-based `mcp-bridge setup` dashboard for managed Linux VPS installations, including legacy detection, selective configuration, staged preparation, and an explicit final activation.
- Added Indygo terminal branding for interactive headings with automatic non-TTY suppression plus `NO_COLOR` and `--no-color` support.
- Added `mcp-bridge status`, `doctor`, `logs`, `version`, `update`, and `rollback` operations with versioned machine-readable reports.
- Added **Settings → Status** with live health signals, cached diagnostics, update availability, and notification dots on both Settings entry points.

### Reliability and Operations

- Output probe runs now record their active tool-result mode, and chunk recommendations compare only runs made in the currently active mode.
- Database schema changes now use recorded migrations; managed services refuse implicit startup migrations.
- Stable updates verify GitHub release digests, preflight migrations on a copy, back up live SQLite, verify activation, and automatically restore the previous release and database on failure.
- Managed setup reports unresolved public DNS as `WAITING` while keeping the local service installed.
- Setup preparation no longer replaces live systemd or Caddy files. Activation stops the old service only for the final SQLite copy, explicitly restarts and health-checks the managed service, adopts an existing Caddy site without duplication, and restores the previous service configuration on failure.

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
