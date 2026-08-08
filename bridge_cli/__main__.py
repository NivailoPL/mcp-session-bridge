from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from bridge_cli.config import read_env_file, update_env_file
from bridge_cli.files import atomic_write_text
from bridge_cli.install import ManagedInstaller, SetupAnswers
from bridge_cli.layout import Layout
from bridge_cli.migrations import migrate_database
from bridge_cli.operations import StatusCollector
from bridge_cli.runner import SubprocessRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-bridge", description="Install and operate MCP Session Bridge.")
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Install, resume, or adopt Bridge.")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--domain")
    setup.add_argument("--username")
    configure = commands.add_parser("configure", help="Change domain or owner credentials.")
    configure.add_argument("--domain")
    configure.add_argument("--username")
    status = commands.add_parser("status", help="Show a fast operational report.")
    status.add_argument("--json", action="store_true", dest="as_json")
    status.add_argument("--refresh", action="store_true")
    status.add_argument("--write-snapshot", action="store_true", help=argparse.SUPPRESS)
    doctor = commands.add_parser("doctor", help="Run deeper read-only diagnostics.")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    logs = commands.add_parser("logs", help="Show Bridge service logs.")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--lines", type=int, default=100)
    commands.add_parser("version", help="Show Bridge and schema versions.")
    migrate = commands.add_parser("migrate", help="Apply explicit database migrations.")
    migrate.add_argument("--db", type=Path)
    update = commands.add_parser("update", help="Check for or install a stable update.")
    update.add_argument("--check", action="store_true")
    commands.add_parser("rollback", help="Restore the previous managed release.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    layout = Layout.for_root(args.root) if args.root else Layout.system()
    runner = SubprocessRunner()
    try:
        if args.command in {"setup", "configure"}:
            if args.command == "setup" and args.dry_run:
                return _setup(args, layout, runner)
            _require_root(args.command)
            from bridge_cli.release import UpdateManager
            with UpdateManager(layout, runner).operation_lock():
                if args.command == "setup":
                    return _setup(args, layout, runner)
                return _configure(args, layout, runner)
        if args.command in {"status", "doctor"}:
            if getattr(args, "refresh", False):
                try:
                    from bridge_cli.release import UpdateManager

                    UpdateManager(layout, runner).check()
                except RuntimeError:
                    pass
            collector = StatusCollector(
                layout, runner, probe_http=layout.root == Path("/"), deep=args.command == "doctor"
            )
            report = collector.collect()
            if getattr(args, "write_snapshot", False):
                _require_root(args.command)
                collector.write_snapshot(report)
            _print_report(report, getattr(args, "as_json", False))
            return 1 if report.overall in {"attention", "failed", "incomplete"} else 0
        if args.command == "logs":
            _require_root("logs")
            command = ["journalctl", "-u", "mcp-session-bridge.service", "-n", str(args.lines)]
            if args.follow:
                command.append("-f")
            os.execvp(command[0], command)
        if args.command == "version":
            report = StatusCollector(layout, runner, probe_http=False).collect()
            print(f"Bridge {report.version['current']} (database schema {report.version['database_schema']})")
            return 0
        if args.command == "migrate":
            _require_root("migrate")
            print(json.dumps(migrate_database(args.db or layout.db_path), indent=2))
            return 0
        if args.command in {"update", "rollback"}:
            from bridge_cli.release import run_release_command
            _require_root(args.command)
            return run_release_command(args, layout, runner)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    return 2


def _setup(args: argparse.Namespace, layout: Layout, runner: SubprocessRunner) -> int:
    if not args.dry_run:
        _require_root("setup")
    domain = (args.domain or input("Domain name: ")).strip()
    username = args.username or input("Owner username [owner]: ").strip() or "owner"
    password = getpass.getpass("Owner password: ")
    if not args.dry_run:
        confirmation = getpass.getpass("Repeat owner password: ")
        if password != confirmation:
            raise ValueError("Passwords do not match.")
    source_root = Path(__file__).resolve().parents[1]
    legacy_db = source_root / "data/bridge.sqlite3"
    legacy_env = source_root / ".env"
    result = ManagedInstaller(layout, source_root, runner).install(
        SetupAnswers(domain=domain, username=username, password=password),
        dry_run=args.dry_run,
        legacy_db=legacy_db if legacy_db.exists() else None,
        legacy_env=legacy_env if legacy_env.exists() else None,
        activate=not args.dry_run,
        progress=_print_progress,
    )
    _print_steps(result)
    return 0


def _configure(args: argparse.Namespace, layout: Layout, runner: SubprocessRunner) -> int:
    _require_root("configure")
    if not read_env_file(layout.env_file):
        raise RuntimeError("Managed configuration is missing. Run mcp-bridge setup first.")
    updates: dict[str, str] = {}
    if args.domain:
        domain = args.domain.strip().lower()
        if "." not in domain or "://" in domain or "/" in domain:
            raise ValueError("Provide a hostname such as bridge.example.com.")
        updates["BRIDGE_PUBLIC_BASE_URL"] = f"https://{domain}"
        updates["BRIDGE_TRANSPORT_ALLOWED_HOSTS"] = f"127.0.0.1:8787,localhost:8787,{domain}"
    if args.username:
        updates["BRIDGE_OWNER_USERNAME"] = args.username.strip()
    if input("Rotate owner password? [y/N]: ").strip().lower() == "y":
        from app.security import password_hash
        password = getpass.getpass("New owner password: ")
        confirmation = getpass.getpass("Repeat new owner password: ")
        if password != confirmation or len(password) < 10:
            raise ValueError("Passwords must match and contain at least 10 characters.")
        updates["BRIDGE_OWNER_PASSWORD_HASH"] = password_hash(password)
    if not updates:
        print("No configuration changes requested.")
        return 0
    backup = layout.backup_root / "configure.env.bak"
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(layout.env_file, backup)
    previous_caddy = (
        layout.caddy_fragment.read_text(encoding="utf-8")
        if layout.caddy_fragment.exists()
        else None
    )
    update_env_file(layout.env_file, updates)
    failures = []
    if args.domain:
        atomic_write_text(
            layout.caddy_fragment,
            f"{domain} {{\n    encode zstd gzip\n    reverse_proxy 127.0.0.1:8787\n}}\n",
        )
        validation = runner.run(
            "caddy", "validate", "--config", str(layout.caddyfile), check=False
        )
        failures.append(validation)
        if validation.returncode == 0:
            failures.append(runner.run("systemctl", "reload", "caddy", check=False))
    if not any(result.returncode != 0 for result in failures):
        failures.append(
            runner.run("systemctl", "restart", "mcp-session-bridge.service", check=False)
        )
    failed = next((result for result in failures if result.returncode != 0), None)
    if failed is not None:
        shutil.copy2(backup, layout.env_file)
        if previous_caddy is None:
            layout.caddy_fragment.unlink(missing_ok=True)
        else:
            atomic_write_text(layout.caddy_fragment, previous_caddy)
        runner.run("systemctl", "reload", "caddy", check=False)
        raise RuntimeError(
            failed.stderr.strip() or "Configuration activation failed; previous files restored."
        )
    print("PASS Configuration updated and Bridge restarted.")
    return 0


def _print_steps(result: dict[str, object]) -> None:
    steps = result.get("steps", [])
    if not result.get("progress_reported"):
        for index, step in enumerate(steps, start=1):
            print(f"[{index}/{len(steps)}] {step}")
    state = result["state"]
    label = "PASS" if state in {"complete", "dry_run"} else "WAITING"
    print(f"{label} setup state: {state}")
    if state == "dry_run":
        print("Next: run mcp-bridge setup without --dry-run when you are ready.")
    elif state == "waiting":
        print(f"Next: {result['next_step']}")
        print("The local Bridge service is installed; public HTTPS is pending DNS.")
    else:
        print("Next: mcp-bridge status")
        print("Next: mcp-bridge doctor")


def _print_progress(index: int, total: int, step: str, state: str) -> None:
    print(f"[{index}/{total}] {state:<8} {step}", flush=True)


def _print_report(report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    print(f"Bridge status: {report.overall.upper()}")
    print(f"Version: {report.version['current']}")
    for check in report.checks:
        label = check.state.replace("_", " ").upper()
        print(f"[{label}] {check.label}: {check.message}")
        if check.remediation:
            print(f"  Next: {check.remediation}")
    if report.update.state == "available":
        print(f"[UPDATE AVAILABLE] {report.update.latest}; run mcp-bridge update")
    elif report.update.state == "current":
        print("[PASS] Bridge is up to date.")
    else:
        print("[NOT CHECKED] Update availability is unknown.")


def _require_root(command: str) -> None:
    if os.geteuid() == 0:
        return
    if shutil.which("sudo") is None:
        raise RuntimeError(f"bridge {command} requires root privileges and sudo is unavailable.")
    os.execvp("sudo", ["sudo", sys.executable, "-m", "bridge_cli", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
