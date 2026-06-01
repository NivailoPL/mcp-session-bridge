# Changelog

All notable changes to MCP Session Bridge will be documented in this file.

This project follows a lightweight changelog format inspired by Keep a Changelog.

## [0.1.0] - 2026-06-01

### Added

- Remote MCP server using FastMCP and streamable HTTP.
- OAuth authorization-code + PKCE flow with dynamic client registration.
- SQLite-backed sessions and full user/model exchange storage.
- Chunked transcript reads through `get_session_overview` and `get_session_transcript_chunk`.
- Session summary storage through Markdown files.
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
