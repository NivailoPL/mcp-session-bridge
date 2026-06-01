# Security Policy

MCP Session Bridge stores conversation transcripts and OAuth credentials. Treat deployments as sensitive.

## Supported Versions

| Version | Supported |
| --- | --- |
| `0.1.x` | Best-effort security fixes |

## Reporting A Vulnerability

Please do not open a public issue for a vulnerability.

For now, report security issues privately to the repository maintainer through the contact channel listed on the GitHub repository profile or project page. Include:

- a short description of the issue,
- affected version or commit,
- reproduction steps,
- potential impact,
- any suggested fix, if known.

If the project later adds a dedicated security contact address, this file should be updated before a broader public release.

## Sensitive Data

Never commit:

- `.env`
- SQLite databases
- OAuth tokens or generated owner credentials
- session summaries containing private data
- generated viewer exports
- files under `secrets/`

The repository `.gitignore` excludes these by default, but contributors should still review changes before committing.
