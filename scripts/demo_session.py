#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.session_package import render_session_overview, render_session_transcript  # noqa: E402
from app.storage import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a local demo MCP Session Bridge transcript.")
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "examples" / "output"),
        help="Directory for demo database and generated transcript files.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    store = Store(output_dir / "demo.sqlite3")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    session_id = _available_session_id(store, f"demo-{stamp}")
    session = store.create_session(session_id, "Demo multi-model session", "manual-context")
    print(f"Created session: {session.session_id}")

    exchanges = [
        (
            "Claude",
            "We are testing whether MCP Session Bridge can keep a shared transcript across models.",
            "I will start the shared session by naming the goal: preserve enough conversation history for the next model to continue without guessing.",
        ),
        (
            "GPT",
            "Please continue from Claude's note and explain what the bridge should do next.",
            "The bridge should expose an overview, return the transcript in bounded chunks, and save my full response before it is shown to the user.",
        ),
    ]

    for index, (model_name, user_message, assistant_response) in enumerate(exchanges, start=1):
        store.save_exchange(session.session_id, model_name, user_message, assistant_response)
        print(f"Saved exchange #{index}: USER -> {model_name}")

    saved_exchanges = store.list_exchanges(session.session_id)
    overview = render_session_overview(session, saved_exchanges, max_lines=180, max_chars=12000)
    transcript = render_session_transcript(session, saved_exchanges)

    transcript_path = output_dir / "demo-transcript.md"
    transcript_path.write_text(transcript["transcript_markdown"], encoding="utf-8")

    session_json_path = output_dir / "demo-session.json"
    session_json_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "Overview: "
        f"{overview['exchange_count']} exchanges, "
        f"{overview['turn_count']} turns, "
        f"{overview['transcript_chunk_count']} transcript chunk"
        f"{'' if overview['transcript_chunk_count'] == 1 else 's'}"
    )
    try:
        display_path = transcript_path.relative_to(ROOT)
    except ValueError:
        display_path = transcript_path
    print(f"Transcript written to: {display_path}")
    print("OK")
    return 0


def _available_session_id(store: Store, base_session_id: str) -> str:
    if store.get_session(base_session_id) is None:
        return base_session_id
    suffix = 2
    while store.get_session(f"{base_session_id}-{suffix}") is not None:
        suffix += 1
    return f"{base_session_id}-{suffix}"


if __name__ == "__main__":
    raise SystemExit(main())
