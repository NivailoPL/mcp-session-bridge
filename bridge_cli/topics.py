"""Read-only view of the Topics lab for the terminal: ``mcp-bridge topics``.

It opens the database in SQLite read-only mode and reuses the topic store's
queries, so what it prints matches the admin. The OpenRouter key lives in a
separate encrypted setting and is never read here.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from threading import Lock
from typing import Any

from app.admin_topics import run_export
from app.topic_store import TopicStoreMixin


class ReadOnlyTopicStore(TopicStoreMixin):
    def __init__(self, db_path: Path):
        if not db_path.exists():
            raise RuntimeError(f"Database not found: {db_path}")
        self.db_path = db_path
        self._lock = Lock()
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'topic_runs'"
            ).fetchone() is None:
                raise RuntimeError("This Bridge has no Topics lab tables yet. Deploy a version with Topics first.")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def get_app_setting(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_app_setting(self, key: str, value: str) -> dict[str, Any]:  # pragma: no cover - never called
        raise RuntimeError("The topics CLI is read-only.")


def add_parser(commands: argparse._SubParsersAction) -> None:
    topics = commands.add_parser("topics", help="Read the Topics lab: map, runs and journal (read-only).")
    actions = topics.add_subparsers(dest="topics_action", required=True)
    for name, help_text in (
        ("status", "Active map, chapter counts per topic and recent runs."),
        ("versions", "Every map version."),
    ):
        command = actions.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true", dest="as_json")
    version = actions.add_parser("version", help="Topics of one map version.")
    version.add_argument("version_id", type=int)
    version.add_argument("--json", action="store_true", dest="as_json")
    journal = actions.add_parser("journal", help="Journal of every step, newest first.")
    journal.add_argument("--limit", type=int, default=40)
    journal.add_argument("--since-hours", type=float)
    journal.add_argument("--json", action="store_true", dest="as_json")
    run = actions.add_parser("run", help="One run: summary, failures and, with --json, every request and answer.")
    run.add_argument("run_id", type=int)
    run.add_argument("--json", action="store_true", dest="as_json")
    proposals = actions.add_parser("proposals", help="Proposals with their test results, newest first.")
    proposals.add_argument("--all", action="store_true", help="Include accepted and rejected ones.")
    proposals.add_argument("--json", action="store_true", dest="as_json")
    export = actions.add_parser("export", help="Write the whole lab (maps, runs, journal, assignments) to a JSON file.")
    export.add_argument("--out", type=Path, required=True)


def run_command(args: argparse.Namespace, db_path: Path) -> int:
    store = ReadOnlyTopicStore(db_path)
    action = args.topics_action
    if action == "status":
        return _emit(_status(store), args.as_json, _print_status)
    if action == "versions":
        return _emit({"versions": store.list_topic_versions()}, args.as_json, _print_versions)
    if action == "version":
        version = store.get_topic_version(args.version_id)
        if version is None:
            raise RuntimeError(f"Unknown map version {args.version_id}.")
        return _emit({"version": version}, args.as_json, _print_version)
    if action == "journal":
        since = int(time.time() - args.since_hours * 3600) if args.since_hours else None
        return _emit({"journal": store.list_topic_journal(limit=args.limit, since=since)}, args.as_json, _print_journal)
    if action == "run":
        export = run_export(store, args.run_id)
        if export is None:
            raise RuntimeError(f"Unknown run {args.run_id}.")
        return _emit(export, args.as_json, _print_run)
    if action == "proposals":
        statuses = None if args.all else ("testing", "pending", "outdated", "failed")
        return _emit({"proposals": store.list_topic_proposals(statuses)}, args.as_json, _print_proposals)
    if action == "export":
        data = _status(store)
        data["versions"] = [store.get_topic_version(v["version_id"]) for v in store.list_topic_versions()]
        data["runs"] = [run_export(store, r["run_id"]) for r in store.list_topic_runs(limit=1000)]
        data["journal"] = store.list_topic_journal(limit=100_000)
        data["proposals"] = store.list_topic_proposals(limit=10_000)
        args.out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        args.out.chmod(0o600)
        print(f"Wrote {args.out}")
        return 0
    raise RuntimeError(f"Unknown topics action {action}")


def _status(store: ReadOnlyTopicStore) -> dict[str, Any]:
    settings = store.get_topic_settings()
    version = store.active_topic_version()
    current = store.topic_current_assignments(version["version_id"]) if version else {}
    chapters = {c["strand_id"]: c for c in store.topic_chapters()}
    live = [item for sid, item in current.items() if sid in chapters]
    names = {topic["key"]: topic["name"] for topic in version["topics"]} if version else {}
    per_topic = Counter(item["topic_key"] for item in live)
    return {
        "settings": settings,
        "active_version": {k: v for k, v in version.items() if k != "topics"} if version else None,
        "counts": store.topic_overview_counts(version["version_id"] if version else None, settings["include_sensitive"]),
        "per_topic": [
            {"key": key, "name": names.get(key, key), "chapters": count}
            for key, count in per_topic.most_common()
        ],
        "assignments": [
            {
                "strand_id": item["strand_id"],
                "title": chapters[item["strand_id"]]["title"],
                "session_title": chapters[item["strand_id"]]["session_title"],
                "topic_key": item["topic_key"],
                "confidence": item["confidence"],
            }
            for item in live
        ],
        "recent_runs": store.list_topic_runs(limit=5),
    }


def _emit(data: dict[str, Any], as_json: bool, printer) -> int:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        printer(data)
    return 0


def _when(timestamp: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp)) if timestamp else "-"


def _print_status(data: dict[str, Any]) -> None:
    version = data["active_version"]
    print(f"Active map: v{version['version_id']} ({version['topic_count']} topics)" if version else "No map yet.")
    counts = data["counts"]
    print(
        f"Chapters {counts['chapters']}: classified {counts['classified']}, unclassified {counts['unclassified']}, "
        f"renamed since {counts['changed']}, unsorted {counts['pool']}, sensitive skipped {counts['sensitive_skipped']}"
    )
    for row in data["per_topic"]:
        print(f"  {row['chapters']:>4}  {row['name']} ({row['key']})")
    print("Recent runs:")
    for run in data["recent_runs"]:
        print(
            f"  #{run['run_id']} {run['kind']} {run['status']} {run['done_count']}/{run['total_count']} "
            f"${run['cost_usd']:.4f} {_when(run['created_at'])}"
            + (f"  [{run['error_code']}]" if run["error_code"] else "")
        )


def _print_versions(data: dict[str, Any]) -> None:
    for version in data["versions"]:
        mark = "*" if version["is_active"] else " "
        print(f"{mark} v{version['version_id']} {version['origin']} {version['topic_count']} topics "
              f"{_when(version['created_at'])} {version['note']}")


def _print_version(data: dict[str, Any]) -> None:
    version = data["version"]
    print(f"Map v{version['version_id']} ({version['origin']}) {version['note']}")
    for topic in version["topics"]:
        print(f"- {topic['key']} | {topic['name']} | {topic['family']}: {topic['description']}")


def _print_journal(data: dict[str, Any]) -> None:
    for entry in data["journal"]:
        ref = f" run {entry['run_id']}" if entry["run_id"] else ""
        print(f"{_when(entry['created_at'])} {entry['actor']:<6} {entry['action']:<18}{ref}  {entry['message']}")


def _print_run(data: dict[str, Any]) -> None:
    run = data["run"]
    print(f"Run #{run['run_id']} {run['kind']} on map v{run['version_id']}: {run['status']}")
    print(f"  scope {run['scope']}, done {run['done_count']}/{run['total_count']}, failed {run['failed_count']}, "
          f"skipped {run['skipped_count']}, cost ${run['cost_usd']:.4f}")
    if run["error_message"]:
        print(f"  {run['error_code']}: {run['error_message']}")
    if run["metrics"]:
        print("  metrics:", json.dumps(run["metrics"], ensure_ascii=False))
    for item in data["items"]:
        if item["status"] == "failed":
            print(f"  failed: #{item['strand_id']} {item['chapter_title']}: {item['error']}")
    print("Use --json for every request and raw answer.")


def _print_proposals(data: dict[str, Any]) -> None:
    if not data["proposals"]:
        print("No open proposals.")
    for p in data["proposals"]:
        test = p["test"] or {}
        verdict = test.get("verdict") or test.get("error") or "-"
        detail = (
            f"landed {test.get('landed')}/{test.get('intended')}, pulled {test.get('pulled_count')}, "
            f"confidence {test.get('confidence_before')} -> {test.get('confidence_after')}, "
            f"unsorted {test.get('pool_before')} -> {test.get('pool_after')}"
            if "landed" in test else ""
        )
        print(f"#{p['proposal_id']} {p['status']:<8} {p['op']:<10} {p['summary']}")
        print(f"    from {p['source']} · test: {verdict}" + (f" · {detail}" if detail else ""))
