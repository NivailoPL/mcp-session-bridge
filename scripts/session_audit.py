from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.session_package import render_session_transcript  # noqa: E402
from app.settings import load_settings  # noqa: E402
from app.storage import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect WW-MCP saved sessions.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List saved sessions.")

    show_parser = subparsers.add_parser("show", help="Print a session transcript.")
    show_parser.add_argument("session_id")
    show_parser.add_argument(
        "--format",
        choices=["markdown", "sequence", "json"],
        default="markdown",
        help="Output format.",
    )

    args = parser.parse_args()
    store = Store(load_settings().db_path)

    if args.command == "list":
        return _list_sessions(store)
    if args.command == "show":
        return _show_session(store, args.session_id, args.format)

    parser.error("unknown command")
    return 2


def _list_sessions(store: Store) -> int:
    sessions = store.list_sessions()
    if not sessions:
        print("No sessions saved yet.")
        return 0

    for session in sessions:
        updated_at = _format_ts(session["updated_at"])
        print(
            f"{session['session_id']} | exchanges={session['exchange_count']} | "
            f"updated={updated_at} | {session['title']}"
        )
    return 0


def _show_session(store: Store, session_id: str, output_format: str) -> int:
    session = store.get_session(session_id)
    if session is None:
        print(f"Unknown session_id: {session_id}", file=sys.stderr)
        return 1

    transcript = render_session_transcript(session, store.list_exchanges(session.session_id))
    if output_format == "json":
        print(json.dumps(transcript, ensure_ascii=False, indent=2))
    elif output_format == "sequence":
        print("\n".join(transcript["turn_sequence"]))
    else:
        print(transcript["transcript_markdown"], end="")
    return 0


def _format_ts(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
