# tools/

Operator tooling that reads a Bridge database directly, without going through
the server. Useful when the service is down, or when you want to read a
transcript without a browser session.

| | |
| --- | --- |
| `session_audit.py` | List sessions, print a transcript as Markdown, JSON or a speaker sequence, and export data for the offline viewer. |
| `session-viewer.html` | A standalone page that reads that export. No server, no login; open it from disk or serve the directory. |

`export-viewer` writes `session-viewer-data.json` here by default, which is
where the page fetches it from. That export is git-ignored — it contains
transcripts.

Usage is documented in [docs/operations.md](../docs/operations.md).
