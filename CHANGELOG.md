# Changelog

All notable changes to MCP Session Bridge will be documented in this file.

This project follows a lightweight changelog format inspired by Keep a Changelog.

## [Unreleased]

### New Features

- Replaced the admin page's top file strip and nested detail overlay with one always-available file workspace opened from the conversation rail.
- Added explicit admin uploads, OS drag-and-drop, and session/group drag-and-drop moves while preserving each file's identity.
- Added text and Markdown editing with stale-write protection, plus a clearly warned permanent-delete flow.

### Quality of Life

- File formats now have prominent labels, and the workspace remains usable with the keyboard, file picker, or touch-sized controls when a session has no messages yet.
- Admin and MCP read surfaces now reconcile against the same current file manifest after owner uploads, moves, edits, or deletes.

### Security

- Admin file mutations require an authenticated owner session and CSRF protection, enforce bounded UTF-8 text uploads, and do not expose new MCP mutation tools.

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
