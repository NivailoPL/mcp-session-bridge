from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from bridge_cli.caddy import replace_site_address
from bridge_cli.config import read_env_file, update_env_file
from bridge_cli.files import atomic_write_json, atomic_write_text, read_json
from bridge_cli.install import ManagedInstaller, SetupAnswers, url_hostname
from bridge_cli.layout import Layout
from bridge_cli.migrations import migrate_database
from bridge_cli.database import DatabaseManager
from bridge_cli.operation_lock import operation_lock
from bridge_cli.service import ServiceManager
from bridge_cli.uninstall import UninstallManager
from bridge_cli.operations import StatusCollector
from bridge_cli.presentation import TerminalTheme
from bridge_cli.runner import SubprocessRunner
from bridge_cli.setup_wizard import SetupInspector, SetupWizard, render_setup_dashboard
from bridge_cli.validation import normalize_domain


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-bridge", description="Install and operate MCP Session Bridge.")
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--no-color", action="store_true", help="Disable terminal colors.")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Install, resume, or adopt Bridge.")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--domain")
    setup.add_argument("--username")
    configure = commands.add_parser("configure", help="Change domain or owner credentials.")
    configure.add_argument("section", nargs="?", choices=("domain", "administrator"))
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
    database = commands.add_parser("database", help="Inspect and manage the Bridge database.")
    database_actions = database.add_subparsers(dest="database_action", required=True)
    for name in ("inspect", "verify", "migrate"):
        command = database_actions.add_parser(name)
        command.add_argument("--json", action="store_true", dest="as_json")
    backup = database_actions.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--json", action="store_true", dest="as_json")
    import_db = database_actions.add_parser("import")
    import_db.add_argument("source", type=Path)
    import_db.add_argument("--replace", action="store_true", required=True)
    import_db.add_argument("--yes", action="store_true")
    import_db.add_argument("--json", action="store_true", dest="as_json")
    for name in ("optimize", "reset"):
        command = database_actions.add_parser(name)
        command.add_argument("--yes", action="store_true")
        command.add_argument("--json", action="store_true", dest="as_json")
    service = commands.add_parser("service", help="Inspect or control the Bridge service.")
    service_actions = service.add_subparsers(dest="service_action", required=True)
    for name in ("inspect", "start", "stop", "restart", "verify"):
        command = service_actions.add_parser(name)
        command.add_argument("--json", action="store_true", dest="as_json")
    service_logs = service_actions.add_parser("logs")
    service_logs.add_argument("--lines", type=int, default=100)
    service_logs.add_argument("--json", action="store_true", dest="as_json")
    installation = commands.add_parser("installation", help="Inspect or remove the managed installation.")
    installation_actions = installation.add_subparsers(dest="installation_action", required=True)
    inspect_installation = installation_actions.add_parser("inspect")
    inspect_installation.add_argument("--json", action="store_true", dest="as_json")
    uninstall = installation_actions.add_parser("uninstall")
    uninstall.add_argument("--output", type=Path)
    uninstall.add_argument("--remove-data", action="store_true")
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--allow-unverified-legacy", action="store_true")
    uninstall.add_argument("--json", action="store_true", dest="as_json")
    deploy = commands.add_parser("deploy", help="Deploy the current committed checkout.")
    deploy.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    deploy.add_argument("--yes", action="store_true")
    deploy.add_argument("--json", action="store_true", dest="as_json")
    update = commands.add_parser("update", help="Check for or install a stable update.")
    update.add_argument("--check", action="store_true")
    commands.add_parser("rollback", help="Restore the previous managed release.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    layout = Layout.for_root(args.root) if args.root else Layout.system()
    runner = SubprocessRunner()
    theme = TerminalTheme.detect(force_plain=args.no_color)
    try:
        if args.command in {"setup", "configure"}:
            if args.command == "setup" and args.dry_run:
                return _setup(args, layout, runner)
            _require_root(args.command)
            if args.command == "setup":
                return _setup(args, layout, runner)
            with operation_lock(layout.operation_lock_file, layout.legacy_operation_lock_file):
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
            _print_report(report, getattr(args, "as_json", False), theme)
            return 1 if report.overall in {"attention", "failed", "incomplete"} else 0
        if args.command == "logs":
            _require_root("logs")
            command = ["journalctl", "-u", "mcp-session-bridge.service", "-n", str(args.lines)]
            if args.follow:
                command.append("-f")
            os.execvp(command[0], command)
        if args.command == "version":
            report = StatusCollector(layout, runner, probe_http=False).collect()
            revision = report.version.get("commit")
            suffix = f"; commit {revision[:12]}" if revision else ""
            print(
                f"Bridge {report.version['current']} "
                f"(database schema {report.version['database_schema']}{suffix})"
            )
            return 0
        if args.command == "migrate":
            _require_root("migrate")
            result = (
                migrate_database(args.db)
                if args.db is not None
                else DatabaseManager(layout, runner).migrate()
            )
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "database":
            _require_root("database")
            return _database(args, layout, runner)
        if args.command == "service":
            _require_root("service")
            return _service(args, layout, runner)
        if args.command == "installation":
            _require_root("installation")
            return _installation(args, layout, runner)
        if args.command == "deploy":
            _require_root("deploy")
            return _deploy(args, layout, runner)
        if args.command in {"update", "rollback"}:
            from bridge_cli.release import run_release_command
            _require_root(args.command)
            return run_release_command(args, layout, runner)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    return 2


def _deploy(args: argparse.Namespace, layout: Layout, runner: SubprocessRunner) -> int:
    from bridge_cli.release import CheckoutDeployer

    installation = read_json(layout.installation_file) or {}
    source_root = args.source
    if source_root is None:
        running_root = Path(__file__).resolve().parents[1]
        configured = installation.get("source_root")
        source_root = Path(str(configured)) if configured else running_root
    manager = CheckoutDeployer(layout, runner)
    plan = manager.plan(source_root)
    if plan.already_active:
        result = {
            "ok": True,
            "operation": "deploy",
            "state": "current",
            "version": plan.version,
            "commit": plan.commit,
            "release_id": plan.release_id,
        }
    else:
        if not args.yes:
            if not sys.stdin.isatty():
                raise ValueError("Non-interactive deploy requires --yes.")
            print("Deploy current committed checkout")
            print(f"Source: {plan.source_root}")
            print(f"Branch: {plan.branch}")
            print(f"Commit: {plan.commit[:12]}")
            print(f"Release: {plan.release_id}")
            print("- build an immutable release from Git HEAD")
            print("- create a final database backup")
            print("- briefly restart Bridge")
            print("- verify health and roll back automatically on failure")
            if plan.untracked_files:
                print(f"NOTICE {len(plan.untracked_files)} untracked file(s) will not be deployed.")
            if input("Deploy now? [y/N]: ").strip().lower() not in {"y", "yes"}:
                print("Deploy cancelled; nothing changed.")
                return 0
        result = manager.deploy(plan.source_root, expected_commit=plan.commit)
    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["state"] == "current":
        print(f"PASS Commit {result['commit'][:12]} is already active.")
    else:
        print(f"PASS Deployed commit {result['commit'][:12]} as {result['release_id']}.")
        print(f"Safety backup: {result['database_backup']}")
        print("Global mcp-bridge and the background service now use this release.")
    return 0


def _database(args: argparse.Namespace, layout: Layout, runner: SubprocessRunner) -> int:
    manager = DatabaseManager(layout, runner)
    action = args.database_action
    if action == "inspect":
        result: dict[str, object] = {
            "format_version": 1,
            "operation": "database.inspect",
            "state": "complete",
            "database": manager.inspect().to_dict(),
        }
    elif action == "verify":
        result = manager.verify()
    elif action == "migrate":
        result = manager.migrate()
    elif action == "backup":
        result = manager.backup(args.output)
    elif action == "import":
        source = args.source.expanduser().resolve()
        incoming = manager.inspect(source)
        current = manager.inspect()
        if not incoming.healthy:
            raise ValueError(incoming.error or "Import source is not a healthy Bridge database.")
        if not args.yes:
            if not sys.stdin.isatty():
                raise ValueError("Non-interactive database import requires --replace --yes.")
            print("This operation will REPLACE the complete current database.")
            print(f"Current database: {current.sessions} sessions")
            print(f"Imported database: {incoming.sessions} sessions")
            print("A final safety backup will be created before replacement.")
            print("Expected downtime: a brief service restart.")
            if input("Replace the database now? [y/N]: ").strip().lower() not in {"y", "yes"}:
                print("Database import cancelled; nothing changed.")
                return 0
        result = manager.import_replace(source)
    elif action in {"optimize", "reset"}:
        if not args.yes:
            if not sys.stdin.isatty():
                raise ValueError(f"Non-interactive database {action} requires --yes.")
            warning = (
                "Reset will REPLACE the complete current database with an empty database."
                if action == "reset"
                else "Optimize will rebuild and replace the current SQLite file."
            )
            print(warning)
            if input(f"Continue with {action}? [y/N]: ").strip().lower() not in {"y", "yes"}:
                print(f"Database {action} cancelled; nothing changed.")
                return 0
        result = manager.reset() if action == "reset" else manager.optimize()
    else:
        return 2
    if getattr(args, "as_json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"PASS {result['operation']}: {result['state']}")
        database = result.get("database") or result.get("after") or result.get("artifact")
        if isinstance(database, dict):
            print(f"Database: {database.get('path')}")
            print(f"Sessions: {database.get('sessions', 0)}")
        if result.get("backup"):
            print(f"Safety backup: {result['backup']}")
        if result.get("credentials_notice"):
            print(f"NOTICE {result['credentials_notice']}")
    return 0


def _service(args: argparse.Namespace, layout: Layout, runner: SubprocessRunner) -> int:
    manager = ServiceManager(layout, runner)
    action = args.service_action
    result = manager.logs(args.lines) if action == "logs" else manager.inspect() if action in {"inspect", "verify"} else manager.mutate(action)
    if getattr(args, "as_json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if action == "logs":
            print(result["logs"])
        else:
            service = result["service"]
            print(f"PASS {result['operation']}: {'active' if service['active'] else 'inactive'}")
    if action == "verify" and not result["service"]["active"]:
        return 1
    if action == "logs" and result["state"] == "failed":
        return 1
    return 0


def _installation(args: argparse.Namespace, layout: Layout, runner: SubprocessRunner) -> int:
    if args.installation_action == "inspect":
        installation = read_json(layout.installation_file) or {}
        result = {
            "format_version": 1,
            "operation": "installation.inspect",
            "state": "complete",
            "installation": installation,
        }
    else:
        manager = UninstallManager(layout, runner)
        plan = manager.plan(
            export_to=args.output,
            remove_data=args.remove_data,
            allow_unverified=args.allow_unverified_legacy,
        )
        if not args.dry_run and not args.yes:
            if not sys.stdin.isatty():
                raise ValueError("Non-interactive uninstall requires --yes (or use --dry-run).")
            print("Uninstall plan:")
            for target in plan.targets:
                print(f"  - remove {target}")
            if plan.database_export:
                print(f"  - first export and verify the database at {plan.database_export}")
            if input("Execute this uninstall plan? [y/N]: ").strip().lower() not in {"y", "yes"}:
                print("Uninstall cancelled; nothing changed.")
                return 0
        result = manager.execute(plan, dry_run=args.dry_run)
    if getattr(args, "as_json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"PASS {result['operation']}: {result['state']}")
        if result.get("database_export"):
            print(f"Database export: {result['database_export']}")
        if result.get("note"):
            print(result["note"])
    return 0


def _setup(args: argparse.Namespace, layout: Layout, runner: SubprocessRunner) -> int:
    if not args.dry_run:
        _require_root("setup")
    source_root = Path(__file__).resolve().parents[1]
    legacy_db = source_root / "data/bridge.sqlite3"
    legacy_env = source_root / ".env"
    legacy_db = legacy_db if legacy_db.exists() else None
    legacy_env = legacy_env if legacy_env.exists() else None
    theme = TerminalTheme.detect(force_plain=getattr(args, "no_color", False))
    if args.dry_run:
        steps = SetupInspector(
            layout,
            source_root,
            runner,
            legacy_db=legacy_db,
            legacy_env=legacy_env,
        ).collect()
        print(render_setup_dashboard(steps, theme))
        print("DRY RUN No files, services, or configuration were changed.")
        return 0
    cli_state = ManagedInstaller(layout, source_root, runner).ensure_global_cli()
    cli_message = {
        "installed": "installed",
        "updated": "updated",
        "current": "already available",
        "managed": "already managed",
    }[cli_state]
    print(f"PASS Global CLI {cli_message}: {layout.command_path}")
    if not args.domain and not args.username:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Interactive setup requires a terminal. Re-run over SSH with a TTY.")
        return SetupWizard(
            layout,
            source_root,
            runner,
            legacy_db=legacy_db,
            legacy_env=legacy_env,
            theme=theme,
        ).run()
    existing = read_env_file(layout.env_file) or (read_env_file(legacy_env) if legacy_env else {})
    domain = (args.domain or input("Domain name: ")).strip()
    username = args.username or existing.get("BRIDGE_OWNER_USERNAME") or input("Owner username [owner]: ").strip() or "owner"
    password = getpass.getpass("Owner password: ")
    confirmation = getpass.getpass("Repeat owner password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match.")
    result = ManagedInstaller(layout, source_root, runner).install(
        SetupAnswers(domain=domain, username=username, password=password),
        dry_run=args.dry_run,
        legacy_db=legacy_db,
        legacy_env=legacy_env,
        activate=False,
        progress=_print_progress,
    )
    _print_steps(result)
    return 0


def _configure(args: argparse.Namespace, layout: Layout, runner: SubprocessRunner) -> int:
    _require_root("configure")
    if not read_env_file(layout.env_file):
        raise RuntimeError("Managed configuration is missing. Run mcp-bridge setup first.")
    section = getattr(args, "section", None)
    current = read_env_file(layout.env_file)
    if not section and not args.domain and not args.username:
        theme = TerminalTheme.detect(force_plain=getattr(args, "no_color", False))
        print(theme.heading("Bridge configuration"))
        print("  1. Public address")
        print("  2. Administrator")
        choice = input("Section: ").strip()
        section = {"1": "domain", "2": "administrator"}.get(choice, choice)
        if section not in {"domain", "administrator"}:
            raise ValueError("Choose Public address or Administrator.")
    updates: dict[str, str] = {}
    if section == "domain" and not args.domain:
        public_url = current.get("BRIDGE_PUBLIC_BASE_URL", "")
        current_domain = url_hostname(public_url) if public_url else ""
        args.domain = input(f"Domain name [{current_domain}]: ").strip() or current_domain
    if args.domain:
        domain = normalize_domain(args.domain)
        updates["BRIDGE_PUBLIC_BASE_URL"] = f"https://{domain}"
        updates["BRIDGE_TRANSPORT_ALLOWED_HOSTS"] = f"127.0.0.1:8787,localhost:8787,{domain}"
    if section == "administrator" and not args.username:
        current_username = current.get("BRIDGE_OWNER_USERNAME", "owner")
        args.username = input(f"Owner username [{current_username}]: ").strip() or current_username
    if args.username:
        updates["BRIDGE_OWNER_USERNAME"] = args.username.strip()
    if (section == "administrator" or args.username) and input("Rotate owner password? [y/N]: ").strip().lower() == "y":
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
    previous_caddyfile = (
        layout.caddyfile.read_text(encoding="utf-8")
        if layout.caddyfile.exists()
        else None
    )
    service_state = runner.run(
        "systemctl", "is-active", "mcp-session-bridge.service", check=False
    )
    service_was_active = (
        service_state.returncode == 0 and service_state.stdout.strip() == "active"
    )
    update_env_file(layout.env_file, updates)
    failures = []
    if args.domain:
        current_public_url = current.get("BRIDGE_PUBLIC_BASE_URL", "")
        old_domain = url_hostname(current_public_url) if current_public_url else None
        caddyfile_text = previous_caddyfile or ""
        replaced = replace_site_address(caddyfile_text, old_domain, domain)
        if replaced != caddyfile_text:
            atomic_write_text(layout.caddyfile, replaced)
        else:
            atomic_write_text(
                layout.caddy_fragment,
                f"{domain} {{\n    encode zstd gzip\n    reverse_proxy 127.0.0.1:8787\n}}\n",
            )
            import_line = f"import {layout.caddy_fragment.parent}/*.caddy"
            if import_line not in caddyfile_text:
                atomic_write_text(layout.caddyfile, caddyfile_text.rstrip() + "\n\n" + import_line + "\n")
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
        if previous_caddyfile is None:
            layout.caddyfile.unlink(missing_ok=True)
        else:
            atomic_write_text(layout.caddyfile, previous_caddyfile)
        restore_failures = []
        if layout.caddyfile.exists():
            restore_failures.append(
                runner.run("caddy", "validate", "--config", str(layout.caddyfile), check=False)
            )
            restore_failures.append(runner.run("systemctl", "reload", "caddy", check=False))
        if service_was_active:
            restore_failures.append(
                runner.run("systemctl", "restart", "mcp-session-bridge.service", check=False)
            )
            active = runner.run(
                "systemctl", "is-active", "mcp-session-bridge.service", check=False
            )
            if active.returncode != 0 or active.stdout.strip() != "active":
                restore_failures.append(active)
        restore_failed = next(
            (result for result in restore_failures if result.returncode != 0),
            None,
        )
        if restore_failed is not None:
            raise RuntimeError(
                "Configuration activation failed and automatic restore also failed: "
                + (restore_failed.stderr.strip() or restore_failed.stdout.strip() or "unknown error")
            )
        raise RuntimeError(
            failed.stderr.strip() or "Configuration activation failed; previous files restored."
        )
    installation = read_json(layout.installation_file)
    if installation and args.domain:
        installation["public_base_url"] = f"https://{domain}"
        atomic_write_json(layout.installation_file, installation)
    print("PASS Configuration updated and Bridge restarted.")
    return 0


def _print_steps(result: dict[str, object]) -> None:
    steps = result.get("steps", [])
    if not result.get("progress_reported"):
        for index, step in enumerate(steps, start=1):
            print(f"[{index}/{len(steps)}] {step}")
    state = result["state"]
    label = "PASS" if state in {"complete", "dry_run"} else "READY" if state == "prepared" else "WAITING"
    print(f"{label} setup state: {state}")
    if state == "dry_run":
        print("Next: run mcp-bridge setup without --dry-run when you are ready.")
    elif state == "prepared":
        print("Next: run mcp-bridge setup and choose Activate installation.")
    elif state == "waiting":
        print(f"Next: {result['next_step']}")
        print("The local Bridge service is installed; public HTTPS is pending DNS.")
    else:
        print("Next: mcp-bridge status")
        print("Next: mcp-bridge doctor")


def _print_progress(index: int, total: int, step: str, state: str) -> None:
    print(f"[{index}/{total}] {state:<8} {step}", flush=True)


def _print_report(report, as_json: bool, theme: TerminalTheme | None = None) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    active_theme = theme or TerminalTheme.detect()
    print(active_theme.heading(f"Bridge status: {report.overall.upper()}"))
    revision = report.version.get("commit")
    suffix = f" ({revision[:12]})" if revision else ""
    print(f"Version: {report.version['current']}{suffix}")
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
