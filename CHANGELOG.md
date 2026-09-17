# Changelog

All notable changes to MCP Session Bridge will be documented in this file.

This project follows a lightweight changelog format inspired by Keep a Changelog.

## [Unreleased]

### Changed

- Removed the `upload_session_image` and `upload_group_image` MCP tools. Users upload images through the admin panel; models can still view session and group images with `view_session_image`. Existing images and admin preview/download remain available.

### Housekeeping

- Moved the served admin, graph and shared navigation assets out of the repository root into `web/`.
- Moved `session_audit.py` and the offline `session-viewer.html` into `tools/`, so the exporter and the page that reads its output live together; the export now defaults to landing beside the viewer.
- Removed the in-repo `docs/plans/` archive and an unreferenced 1.1 MB documentation image.
- Gave the README a documentation index that leads with the managed installation, sourced its logo from `brand/`, and added a `README.md` to `web/`, `tools/`, `scripts/` and `docs/`.
- Dropped stale `v0.1` framing from the documentation.
- Rewrote the brand package guide in English, with color names matching the asset filenames.
- Corrected the versioning example, which was a cycle behind, and a limitation that described a surface the repository no longer has.
- Added the PDF storage quota variable to `.env.example`.
- Removed the retired context-packs feature: the unused module, its settings and environment variables, the constant `context_source` field in two MCP responses, and the directory the installer created on every setup. The two database columns are kept so the schema stays version 2 and rollback still works.
- Documented the `database`, `service`, `migrate` and `installation` command families, which had no coverage at all, and rewrote Backups around `mcp-bridge database backup`.
- Rewrote client setup around the managed installation and named the clients the Bridge has been verified against.

## [2026.8.2] - 2026-08-28

### Highlights

- Refined the Admin workspace with a denser, more consistent session list, safer sensitive-session handling, and clearer settings controls.
- Added complete server-side Markdown database exports through both `mcp-bridge export` and **Settings → Database**, including grouped sessions and stored attachments.
- Adopted stable `YYYY.M.N` CalVer releases while preserving managed updates from historical `0.x` installations.
- Added styled destructive confirmations and masked model placeholders across Admin and Graph surfaces.
- Added a disposable demo database workflow for previewing the Admin UI without touching real Bridge data.

### New Features

- Added `mcp-bridge export` for complete, group-organized Markdown conversation archives with original stored attachments and machine-readable output.
- Added **Settings → Database → Export Database (.md)** as a server-side trigger for the same managed export.
- Added styled confirmation dialogs for destructive Admin and Graph operations.
- Added `scripts/demo_ui.py` to rebuild and serve isolated sample sessions for local UI previews.

### Quality of Life

- Unified session-row density, action placement, settings layouts, and sensitive-conversation reveal behavior across the Admin workspace.
- Preserved spaces in manually assigned session titles and included the current session ID in response guidance.
- Added masked placeholders for configured model names and a visible development-version label during beta cycles.

### Reliability and Operations

- Hardened Markdown export coordination with cross-process locking, scalable archive generation, safer attachment names, and complete regression coverage.
- Kept the PDF worker sandbox best-effort on platforms that do not support every isolation primitive.
- Added end-to-end OAuth connector coverage plus shared Admin and viewer test infrastructure.
- Added canonical CalVer validation to the updater and release workflow, including lockfile freshness checks and the supported `0.5.0` to `2026.8.2` transition.

### Security

- Markdown archives remain in the private VPS data directory; the Admin API cannot select, list, read, or download export files.
- Sensitive conversations remain covered until explicitly revealed, including before row-level actions execute.

### Included commits

- [`7bdfacc`](https://github.com/NivailoPL/mcp-session-bridge/commit/7bdfaccf73f202bce6cde3c6ac1eb33f78628f75) refactor(admin): collapse the design vocabulary onto one token scale
- [`6c1f5d3`](https://github.com/NivailoPL/mcp-session-bridge/commit/6c1f5d3ff6dfd5a819a736aa6d25ec6ad18151d5) feat(admin): one row height and one rail in the session list
- [`027dda6`](https://github.com/NivailoPL/mcp-session-bridge/commit/027dda6492215edd2dc42fff54c7705ec26841ea) feat(admin): row actions become an icon cluster
- [`e687447`](https://github.com/NivailoPL/mcp-session-bridge/commit/e6874474cfd7e1ae1813f4c58ff8c102e542ecd1) feat(admin): findable session key, and a cover that keeps the panel usable
- [`5fb04ef`](https://github.com/NivailoPL/mcp-session-bridge/commit/5fb04efa4c9937978dfef7c2813b76e807707d14) fix(admin): air in the header, and a cover that survives a screenshot
- [`259c279`](https://github.com/NivailoPL/mcp-session-bridge/commit/259c2798f0acc65e1c280699078d21f213db2c9d) fix(admin): every row action reveals a covered group before it runs
- [`ef340ff`](https://github.com/NivailoPL/mcp-session-bridge/commit/ef340ffa89e35c84a7b5c9c8e9dc5fcceae33d41) fix(admin): the settings panel scrolls, not the whole dialog
- [`cf97b19`](https://github.com/NivailoPL/mcp-session-bridge/commit/cf97b190926803f616b7d7a379bcd57c9fa44dee) fix(admin): one control vocabulary and one grid family in settings
- [`bf0d3a2`](https://github.com/NivailoPL/mcp-session-bridge/commit/bf0d3a271dcf1bf89cf6a59937d3aea0abd90a6a) chore(release): mark 0.5.1 beta target
- [`5195924`](https://github.com/NivailoPL/mcp-session-bridge/commit/519592458b5ca977fef9e796681a9ff9df607215) fix(prompt): include session id in response header
- [`7454e2e`](https://github.com/NivailoPL/mcp-session-bridge/commit/7454e2e300dcff36b601bf77f2d369279e1b3ef2) fix(admin): restore sensitive conversation reveal
- [`bc495c8`](https://github.com/NivailoPL/mcp-session-bridge/commit/bc495c8fefda5b04b1c1dfe12cf33cb5bae2fd1f) Refine sensitive curtain hover styling
- [`c42ffa1`](https://github.com/NivailoPL/mcp-session-bridge/commit/c42ffa10e08b1b58f73b903dc1f3617a963f6989) feat(admin): show development version label
- [`d782c91`](https://github.com/NivailoPL/mcp-session-bridge/commit/d782c9167bcfda92a3012d923d6e7576dde13c8a) fix(pdf): keep the worker sandbox best-effort per platform
- [`c86f8ee`](https://github.com/NivailoPL/mcp-session-bridge/commit/c86f8eec89d864ed81a6bdf64b8745e53455e922) test(oauth): cover the connector handshake end to end
- [`825d02c`](https://github.com/NivailoPL/mcp-session-bridge/commit/825d02cc019a3cd79f07660d7ae36af877098c0c) test(admin): assert what the viewer does, not what its source says
- [`31ccc03`](https://github.com/NivailoPL/mcp-session-bridge/commit/31ccc0370aedaea6fce05539a1a4b08359353bc6) Merge pull request #5 from NivailoPL/chore/test-suite-health
- [`6c2a0d4`](https://github.com/NivailoPL/mcp-session-bridge/commit/6c2a0d438c3307900affdc3d0dc5a34331cf4adc) feat(export): add server-side Markdown archive CLI
- [`e2e2fab`](https://github.com/NivailoPL/mcp-session-bridge/commit/e2e2fab999567f26fe01a111998b138228e4feb5) feat(admin): trigger Markdown database exports on VPS
- [`a83ffc1`](https://github.com/NivailoPL/mcp-session-bridge/commit/a83ffc11d139215c561cd88fd0216e6d1504fa26) docs(export): harden and document VPS archives
- [`b7d28f9`](https://github.com/NivailoPL/mcp-session-bridge/commit/b7d28f98c2a1935a6e152e491e094b867915215d) refactor(export): simplify attachment filename sanitizing
- [`578a3d0`](https://github.com/NivailoPL/mcp-session-bridge/commit/578a3d01ba7a15db07b437a4f54074642cd2aa0f) fix(export): harden archive coordination and scale
- [`c98373e`](https://github.com/NivailoPL/mcp-session-bridge/commit/c98373e95e817ca12cd61cf52285e4f01330fb61) Merge pull request #6 from NivailoPL/codex/markdown-database-export
- [`5249dc0`](https://github.com/NivailoPL/mcp-session-bridge/commit/5249dc06aa6393632a2ea089f606d0a9aafca6f9) fix(admin): keep spaces in manual session titles
- [`0273522`](https://github.com/NivailoPL/mcp-session-bridge/commit/0273522ca36e893affa02c5d99f0b3280b3d18f4) chore(release): adopt CalVer versioning
- [`9a0c068`](https://github.com/NivailoPL/mcp-session-bridge/commit/9a0c068926e91f78a58ba74c1876cf6f43f586a6) feat(admin): style destructive confirmations
- [`526dc61`](https://github.com/NivailoPL/mcp-session-bridge/commit/526dc61beff1b3d766542ccf89896e012e00d321) feat(admin): show masked model placeholder
- [`d4ac54c`](https://github.com/NivailoPL/mcp-session-bridge/commit/d4ac54c4cc2b8802fd2b018173c80ac47521537d) Merge pull request #7 from NivailoPL/codex/niv-7-styled-confirmations
- [`ca57639`](https://github.com/NivailoPL/mcp-session-bridge/commit/ca57639bc92a963cf3e4fdbb8c380f4f0e2fc444) feat(demo): add demo database for admin UI previews
- [`676ca63`](https://github.com/NivailoPL/mcp-session-bridge/commit/676ca633a6b97c81abf4b301415685cdfea87462) test(version): support stable release metadata

[Compare changes: v0.5.0...v2026.8.2](https://github.com/NivailoPL/mcp-session-bridge/compare/v0.5.0...v2026.8.2)

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
