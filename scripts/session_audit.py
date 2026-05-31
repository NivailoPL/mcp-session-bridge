from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.session_package import render_session_transcript  # noqa: E402
from app.settings import load_settings  # noqa: E402
from app.storage import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect WW-MCP saved sessions.")
    parser.add_argument("--db-path", help="Override BRIDGE_DB_PATH from .env.")
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

    export_parser = subparsers.add_parser("export-viewer", help="Export session data for the offline HTML viewer.")
    export_parser.add_argument(
        "--output",
        default=str(ROOT / "session-viewer-data.json"),
        help="JSON output path consumed by session-viewer.html.",
    )
    export_parser.add_argument(
        "--watch",
        type=float,
        default=0,
        help="Rewrite the output every N seconds until interrupted.",
    )

    args = parser.parse_args()
    db_path = Path(args.db_path) if args.db_path else load_settings().db_path
    store = Store(db_path)

    if args.command == "list":
        return _list_sessions(store)
    if args.command == "show":
        return _show_session(store, args.session_id, args.format)
    if args.command == "export-viewer":
        return _export_viewer_data(store, Path(args.output), args.watch)

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


def _export_viewer_data(store: Store, output_path: Path, watch_seconds: float) -> int:
    if watch_seconds < 0:
        print("--watch must be 0 or greater.", file=sys.stderr)
        return 2

    while True:
        payload = build_viewer_payload(store)
        _write_json_atomic(output_path, payload)
        print(
            f"Wrote {output_path} | sessions={len(payload['sessions'])} | "
            f"turns={payload['turn_count']} | generated={payload['generated_at_iso']}"
        )
        sys.stdout.flush()

        if watch_seconds <= 0:
            return 0
        time.sleep(watch_seconds)


def build_viewer_payload(store: Store) -> dict[str, Any]:
    sessions = store.list_sessions()
    transcripts: dict[str, dict[str, Any]] = {}
    turn_count = 0

    for session_summary in sessions:
        session = store.get_session(session_summary["session_id"])
        if session is None:
            continue
        transcript = render_session_transcript(session, store.list_exchanges(session.session_id))
        transcripts[session.session_id] = transcript
        turn_count += transcript["turn_count"]

    generated_at = int(time.time())
    return {
        "ok": True,
        "schema": "mcp-session-bridge.viewer.v1",
        "generated_at": generated_at,
        "generated_at_iso": _format_ts(generated_at),
        "session_count": len(sessions),
        "turn_count": turn_count,
        "sessions": sessions,
        "transcripts": transcripts,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def _format_ts(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
