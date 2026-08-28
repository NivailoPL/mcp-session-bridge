#!/usr/bin/env python3
"""Build a disposable demo database and serve the admin UI against it.

The database is rebuilt from scratch on every run, so it always matches the
current schema, and it is written to the git-ignored ``examples/output``
directory. The real ``data/`` database is never touched.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.security import password_hash  # noqa: E402
from app.storage import Store  # noqa: E402

DB_PATH = ROOT / "examples" / "output" / "demo.sqlite3"
DEFAULT_PORT = 8788
OWNER_USERNAME = "owner"
OWNER_PASSWORD = "demo"
CONTEXT_PACK_ID = "manual-context"

# Conversations are spread backwards over this many days so the session list
# shows a plausible range of dates instead of ten identical timestamps.
SPAN_DAYS = 21

GROUPS: list[dict[str, Any]] = [
    {
        "group_id": "product",
        "name": "Product Planning",
        "color": "#4f7cff",
        "icon_key": "book",
        "is_sensitive": False,
    },
    {
        "group_id": "engineering",
        "name": "Engineering",
        "color": "#2fa36b",
        "icon_key": "code",
        "is_sensitive": False,
    },
    {
        "group_id": "northwind",
        "name": "Client NDA - Northwind",
        "color": "#b4553f",
        "icon_key": "legal",
        "is_sensitive": True,
    },
]

SESSIONS: list[dict[str, Any]] = [
    {
        "session_id": "demo-roadmap-q3",
        "title": "Tidepool roadmap for Q3",
        "group_id": "product",
        "exchanges": [
            (
                "Claude",
                "We have three months before the Tidepool beta. Help me decide what actually ships.",
                "Three things fit in a quarter, not seven. I would ship the CSV importer, saved views, and a "
                "single shareable report. Everything else on your list depends on one of those, so they are "
                "the ones that unblock the rest.",
            ),
            (
                "GPT",
                "Claude suggested importer, saved views, shareable report. What is the risk in that order?",
                "The importer is the only item with unknown scope, because customer files are inconsistent. "
                "Start it first and keep the report last, so if anything slips it is the piece you can cut "
                "without breaking the other two.",
            ),
            (
                "Claude",
                "Agreed. Write the one-paragraph version I can paste into the roadmap doc.",
                "Q3 delivers a working import path from customer spreadsheets, saved views so a team can keep "
                "its own slices of the data, and one shareable read-only report. Anything beyond that moves to "
                "Q4 rather than shrinking these three.",
            ),
        ],
    },
    {
        "session_id": "demo-pricing-tiers",
        "title": "Pricing tiers for the beta",
        "group_id": "product",
        "exchanges": [
            (
                "GPT",
                "Two tiers or three for the Tidepool beta?",
                "Two. A third tier during a beta mostly creates questions you cannot answer yet, because you "
                "have no usage data to defend the boundary between them.",
            ),
            (
                "GPT",
                "What separates the two tiers then?",
                "Seat count and history retention. Both are easy to explain, easy to measure, and neither "
                "requires you to gate a feature you might still redesign.",
            ),
            (
                "Gemini",
                "Sanity-check the two-tier plan from GPT before I take it to the team.",
                "It holds up. The one thing I would add is a written commitment that beta pricing changes at "
                "general availability, so early customers are not surprised later.",
            ),
        ],
    },
    {
        "session_id": "demo-onboarding-copy",
        "title": "Onboarding copy review",
        "group_id": "product",
        "exchanges": [
            (
                "Gemini",
                "The empty state says 'No data yet. Import a file to get started.' Too blunt?",
                "It is fine, but it stops one step short. Tell the reader what a good file looks like, "
                "because the failure people actually hit is uploading the wrong sheet.",
            ),
            (
                "Gemini",
                "Rewrite it.",
                "\"Nothing here yet. Import a spreadsheet with one row per transaction and a header row, and "
                "your first view appears in a few seconds.\"",
            ),
        ],
    },
    {
        "session_id": "demo-search-ranking",
        "title": "Search ranking rewrite",
        "group_id": "engineering",
        "exchanges": [
            (
                "Claude",
                "Our search returns the right documents but in a useless order. Where do I start?",
                "Start by writing down what 'right order' means for twenty real queries. Ranking work without "
                "a labelled set turns into an argument about taste, and you cannot tell a regression from an "
                "improvement.",
            ),
            (
                "Claude",
                "I labelled 24 queries. Now what?",
                "Measure the current ranking against them before changing anything. Even a rough score gives "
                "you the baseline that every later change has to beat.",
            ),
            (
                "GPT",
                "Baseline is nDCG@10 of 0.41. Is that as bad as it sounds?",
                "It is mediocre rather than broken. Pure term-frequency scoring usually lands in that range, "
                "which suggests the problem is missing signals, not a bug in the index.",
            ),
            (
                "GPT",
                "Which signals are worth adding first?",
                "Field weighting and recency. Title matches almost always deserve more weight than body "
                "matches, and in a workspace tool the newer document is usually the one the reader wants.",
            ),
            (
                "Codex",
                "Show me the scoring function with field weights added.",
                "Here is the shape of it:\n\n```python\ndef score(doc, query, now):\n    title = bm25(doc.title, query) * 2.5\n    body = bm25(doc.body, query)\n    age_days = (now - doc.updated_at) / 86400\n    recency = 1.0 / (1.0 + age_days / 30)\n    return (title + body) * (0.8 + 0.2 * recency)\n```\n\n"
                "The recency term is deliberately weak. It should break ties, not overturn a strong text match.",
            ),
            (
                "Codex",
                "Why 2.5 for the title weight?",
                "No principled reason, it is a starting point. Sweep it between 1.5 and 4.0 against your "
                "labelled set and keep whatever wins, because the right value depends on how long your "
                "titles are.",
            ),
            (
                "Claude",
                "Sweep says 3.0. nDCG@10 is now 0.58.",
                "That is a real improvement and it came from two signals, which is a good sign. Before you "
                "add a third, look at the queries that are still wrong, since they usually fail for one "
                "shared reason rather than twenty different ones.",
            ),
            (
                "Claude",
                "Six of the eight bad queries are short. Two words or fewer.",
                "Short queries carry almost no term evidence, so ranking falls back to whatever weak signal "
                "is left. That is the case where semantic matching earns its cost, rather than across the "
                "whole corpus.",
            ),
            (
                "GPT",
                "So embeddings only for short queries? That feels like a hack.",
                "It is a routing decision, not a hack. You pay for the expensive path only where the cheap "
                "path is known to fail, and you keep the latency of common queries unchanged.",
            ),
            (
                "GPT",
                "Summarise the current numbers as a table for the team.",
                "| Change | nDCG@10 | Latency p95 |\n|---|---|---|\n| Baseline BM25 | 0.41 | 24 ms |\n"
                "| Field weights | 0.52 | 25 ms |\n| Weights + recency | 0.58 | 26 ms |\n"
                "| Hybrid, short queries only | 0.66 | 31 ms |\n\nThe last row is the one to discuss, since "
                "it is the only change that adds an external dependency.",
            ),
            (
                "Codex",
                "What breaks if the embedding provider is down?",
                "Short queries fall back to the lexical path and get worse results, which is acceptable. What "
                "is not acceptable is the request hanging, so the call needs a timeout well under your p95 "
                "budget and a cached result for repeated queries.",
            ),
            (
                "Claude",
                "Good. Write the two-sentence summary for the pull request.",
                "Search ranking now weights title matches and mildly favours recent documents, raising nDCG@10 "
                "from 0.41 to 0.58 on a 24-query labelled set. Short queries additionally use semantic "
                "matching, with a lexical fallback whenever the provider is slow or unavailable.",
            ),
        ],
    },
    {
        "session_id": "demo-pool-timeouts",
        "title": "Postgres connection pool timeouts",
        "group_id": "engineering",
        "exchanges": [
            (
                "Codex",
                "We get 'timeout acquiring connection' about twice an hour under normal load.",
                "Twice an hour under normal load points at connections that are never returned rather than a "
                "pool that is too small. A leak looks exactly like this: fine most of the time, then a burst "
                "of failures.",
            ),
            (
                "Codex",
                "How do I confirm that before I start changing pool settings?",
                "Log the pool's in-use count every ten seconds. A leak shows a floor that creeps upward and "
                "never returns to zero, while genuine load saturates and then recovers.",
            ),
            (
                "Codex",
                "The floor climbs by roughly one connection every twenty minutes. So, a leak.",
                "That rate is slow enough to be an error path rather than a hot one. Look for a query that "
                "is executed outside a context manager somewhere an exception can be raised between checkout "
                "and release.",
            ),
            (
                "Claude",
                "Found it. The nightly export borrows a connection and only releases it on success.",
                "That matches the arithmetic exactly. Wrap it so release happens in a finally block, and add "
                "the in-use gauge to your dashboard so the next leak is visible before it becomes an "
                "incident.",
            ),
        ],
    },
    {
        "session_id": "demo-flaky-tests",
        "title": "Flaky end-to-end tests",
        "group_id": "engineering",
        "exchanges": [
            (
                "Claude",
                "Three end-to-end tests fail maybe one run in five. Always different ones.",
                "Different tests failing at a similar rate usually means shared state rather than three "
                "separate bugs. The common suspects are test order, a shared database, and fixed sleeps.",
            ),
            (
                "Claude",
                "They all hit the same seeded database. We do not reset it between tests.",
                "Then the tests are ordering-dependent and the failures only look random because the runner "
                "shuffles. Give each test its own transaction and roll it back at teardown.",
            ),
            (
                "Claude",
                "Done. Twenty consecutive green runs. 🎉",
                "Twenty runs at a one-in-five failure rate is convincing evidence. Keep the shuffle enabled, "
                "since it is what surfaced the problem in the first place.",
            ),
        ],
    },
    {
        "session_id": "demo-csv-importer",
        "title": "Migrating the CSV importer",
        "group_id": "engineering",
        "exchanges": [
            (
                "GPT",
                "The importer chokes on files exported from Excel. Encoding, I assume?",
                "Almost certainly a byte-order mark and CRLF line endings. Excel writes UTF-8 with a BOM, so "
                "the first header cell arrives with three invisible bytes attached and never matches.",
            ),
            (
                "GPT",
                "Strip the BOM and move on?",
                "Read the file as utf-8-sig, which handles the BOM whether or not it is present. Also accept "
                "semicolons as delimiters, because that is what Excel writes in most of Europe.",
            ),
            (
                "Codex",
                "Any other format surprises worth handling now?",
                "Two. Numbers with a comma as the decimal separator, and dates in day-first order. Both parse "
                "without error into wrong values, which is worse than a crash because nobody notices.",
            ),
        ],
    },
    {
        "session_id": "demo-northwind-retention",
        "title": "Northwind data retention questions",
        "group_id": "northwind",
        "exchanges": [
            (
                "Claude",
                "Northwind asks how long we keep imported files. We have never written this down.",
                "Then answer with what the system actually does today, not with what sounds reassuring. An "
                "aspirational retention policy you cannot demonstrate is worse than a short one you can.",
            ),
            (
                "Claude",
                "Today: uploads stay until the workspace is deleted, backups roll off after 30 days.",
                "State exactly that, plus how a customer requests deletion and how long it takes. Those are "
                "the two questions behind the one they asked.",
            ),
            (
                "GPT",
                "Draft the paragraph for their security review.",
                "Imported files are retained for the life of the workspace and removed within seven days of a "
                "deletion request. Encrypted backups are kept for 30 days, after which restoration of deleted "
                "content is no longer possible.",
            ),
        ],
    },
    {
        "session_id": "demo-northwind-checklist",
        "title": "Northwind integration checklist",
        "group_id": "northwind",
        "exchanges": [
            (
                "GPT",
                "What has to be true before the Northwind pilot starts next month?",
                "Four things: their sandbox credentials issued, the field mapping agreed in writing, an "
                "error contact on both sides, and a rollback plan if the first import produces wrong "
                "numbers.",
            ),
            (
                "GPT",
                "The field mapping is the one that always slips. Why?",
                "Because it is the only item that needs a decision from someone outside engineering. Ask for "
                "it first and treat a delay there as a schedule risk rather than a detail.",
            ),
        ],
    },
    {
        "session_id": "demo-uncategorized-note",
        "title": "New session",
        "group_id": "uncategorized",
        "auto_title": True,
        "exchanges": [
            (
                "Claude",
                "Quick question about SQLite WAL mode before I forget it again",
                "In WAL mode readers never block the writer and the writer never blocks readers, which is why "
                "it suits a single-process server well. The cost is two extra files next to the database that "
                "have to travel with it in any backup.",
            ),
            (
                "Claude",
                "So a backup that copies only the .sqlite3 file can be inconsistent?",
                "Yes, if a checkpoint has not run. Use the backup API or VACUUM INTO rather than copying the "
                "file, and you get a single consistent artifact.",
            ),
        ],
    },
]


@contextmanager
def frozen_clock(start: float) -> Iterator[dict[str, float]]:
    """Freeze ``time.time`` so seeded rows get the timestamps we choose.

    ``Store`` stamps every row with ``time.time()`` at write time, so this is
    the least invasive way to spread the demo conversations over three weeks.
    """

    original = time.time
    state = {"now": start}
    time.time = lambda: state["now"]  # type: ignore[assignment]
    try:
        yield state
    finally:
        time.time = original  # type: ignore[assignment]


def build_database() -> tuple[int, int]:
    if DB_PATH.parent != ROOT / "examples" / "output":
        raise SystemExit(f"Refusing to build the demo database outside examples/output: {DB_PATH}")

    for suffix in ("", "-wal", "-shm"):
        Path(f"{DB_PATH}{suffix}").unlink(missing_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    gaps = random.Random(0)
    real_now = time.time()
    span = SPAN_DAYS * 86400
    session_gap = span / len(SESSIONS)
    exchange_count = 0

    # Sessions are authored grouped by group, but dated in a shuffled order so
    # the session list mixes groups the way a real one would.
    slots = list(range(len(SESSIONS)))
    random.Random(1).shuffle(slots)

    with frozen_clock(real_now - span) as clock:
        store = Store(DB_PATH)

        for group in GROUPS:
            store.create_session_group(
                group["name"],
                group["color"],
                group["icon_key"],
                group_id=group["group_id"],
            )
            if group["is_sensitive"]:
                store.update_session_group(group["group_id"], is_sensitive=True)

        for index, session in enumerate(SESSIONS):
            clock["now"] = real_now - span + slots[index] * session_gap
            store.create_session(
                session["session_id"],
                session["title"],
                CONTEXT_PACK_ID,
                title_is_auto=session.get("auto_title", False),
                group_id=session["group_id"],
            )
            for model_name, user_message, assistant_response in session["exchanges"]:
                clock["now"] += gaps.randint(120, 2400)
                store.save_exchange(session["session_id"], model_name, user_message, assistant_response)
                exchange_count += 1

    return len(SESSIONS), exchange_count


def serve(port: int) -> None:
    os.environ["BRIDGE_DB_PATH"] = str(DB_PATH)
    os.environ["BRIDGE_PUBLIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    os.environ["BRIDGE_OWNER_USERNAME"] = OWNER_USERNAME
    os.environ["BRIDGE_OWNER_PASSWORD_HASH"] = password_hash(OWNER_PASSWORD)
    os.environ["BRIDGE_SECRET_KEY"] = "demo-only-secret-not-for-production"
    os.environ["BRIDGE_TRANSPORT_ALLOWED_HOSTS"] = f"127.0.0.1:{port},localhost:{port}"
    os.environ["BRIDGE_TRANSPORT_ALLOWED_ORIGINS"] = f"http://127.0.0.1:{port},http://localhost:{port}"

    import uvicorn

    print(f"Admin UI:  http://127.0.0.1:{port}/admin")
    print(f"Sign in:   {OWNER_USERNAME} / {OWNER_PASSWORD}")
    print("Stop with Ctrl+C. Editing admin-viewer.html only needs a browser refresh.")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to serve on (default {DEFAULT_PORT}).")
    parser.add_argument("--build-only", action="store_true", help="Rebuild the demo database without serving it.")
    args = parser.parse_args()

    sessions, exchanges = build_database()
    print(f"Demo database: {DB_PATH.relative_to(ROOT)} ({sessions} sessions, {exchanges} exchanges)")

    if args.build_only:
        return 0

    serve(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
