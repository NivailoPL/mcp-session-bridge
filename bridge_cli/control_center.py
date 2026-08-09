from __future__ import annotations

import json
import time
from pathlib import Path

from bridge_cli.database import DatabaseManager
from bridge_cli.migrations import migrate_database
from bridge_cli.terminal_menu import MenuDriver, MenuItem
from bridge_cli.uninstall import UninstallManager
from bridge_cli.operation_lock import operation_lock
from bridge_cli.service import ServiceManager
from bridge_cli.files import atomic_write_json, read_json
from bridge_cli.operations import StatusCollector


class ControlCenter:
    def __init__(self, wizard, driver: MenuDriver):
        self.wizard = wizard
        self.driver = driver
        self.database = DatabaseManager(wizard.layout, wizard.runner)
        self.selected_main = "full_install"
        self.selected_submenu: dict[str, str] = {}

    def run(self) -> int:
        while True:
            choice = self.driver.choose(
                "MCP Session Bridge — Setup & configuration",
                self._main_items(),
                selected_id=self.selected_main,
            )
            if choice is None or choice == "exit":
                self.wizard.output("Setup saved. Run mcp-bridge setup to continue.")
                return 0
            if not choice:
                continue
            self.selected_main = choice
            if choice == "full_install":
                self._full_install()
            elif choice == "verify":
                self._section("verify")
            else:
                self._section(choice)

    def _main_items(self) -> list[MenuItem]:
        steps = self.wizard._steps()
        descriptions = {
            "server": "Source checkout and host prerequisites",
            "installation": "Adopt an existing installation or uninstall",
            "release": "Stage files or check for updates",
            "database": "Verify, import, backup, optimize, or reset",
            "administrator": "Setup for web access",
            "public_address": "Domain, HTTPS, and reverse proxy",
            "service": "Install, start, stop, restart, or inspect logs",
        }
        sections = [
            MenuItem(step.id, step.label, descriptions.get(step.id, step.detail), step.state)
            for step in steps
            if step.id not in {"activate", "verify"}
        ]
        activation = next(step for step in steps if step.id == "activate")
        verify = next(step for step in steps if step.id == "verify")
        return [
            MenuItem(
                "full_install",
                "Run full installation",
                "Recommended starting point; plans and completes only required steps",
                activation.state,
            ),
            *sections,
            MenuItem("verify", "Verify installation", "Standard and deep operational checks", verify.state),
            MenuItem("exit", "Exit", "Leave setup without changing anything"),
        ]

    def _section(self, section: str) -> None:
        while True:
            actions = self._actions(section)
            selected = self.selected_submenu.get(section, actions[0].id)
            choice = self.driver.choose(self._section_title(section), actions, selected_id=selected)
            if choice is None or choice == "return":
                return
            if not choice:
                continue
            self.selected_submenu[section] = choice
            self._execute(section, choice)

    def _section_title(self, section: str) -> str:
        step = next(item for item in self.wizard._steps() if item.id == section)
        lines = [f"{step.label}: ({step.state})", step.detail]
        if section == "database":
            inspected_path = (
                self.wizard.layout.db_path
                if self.wizard.layout.db_path.exists()
                else self.wizard.legacy_db
            )
            info = self.database.inspect(inspected_path)
            lines.extend(
                [
                    "",
                    info.path,
                    f"{info.size_bytes / 1024 / 1024:.1f} MB main + {info.wal_bytes / 1024 / 1024:.1f} MB WAL, {info.sessions} sessions",
                    f"schema {info.schema_version if info.schema_version is not None else 'unknown'}, integrity {info.integrity}",
                ]
            )
        return "\n".join(lines)

    def _actions(self, section: str) -> list[MenuItem]:
        mapping: dict[str, list[MenuItem]] = {
            "server": [],
            "installation": [
                MenuItem("adopt", "Choose installation type", "Adopt the detected installation or start fresh"),
                MenuItem("uninstall", "Uninstall Bridge", "Preview scoped removal and preserve the database"),
            ],
            "release": [
                MenuItem(
                    "deploy",
                    "Deploy Current Checkout",
                    "Install the current committed Git revision with backup and rollback",
                ),
                MenuItem("stage", "Stage current checkout", "Prepare release files without changing the live service"),
                MenuItem("update", "Check for update", "Use mcp-bridge update --check"),
            ],
            "database": [
                MenuItem("verify", "Verify Database"),
                MenuItem("import", "Import Database", "REPLACES the complete current database"),
                MenuItem("backup", "Backup Database", "Create a verified portable SQLite file"),
                MenuItem("migrate", "Schema Migration"),
                MenuItem("optimize", "Optimize Database", "Rebuild a verified copy and replace safely"),
                MenuItem("reset", "WARNING: Reset to Empty", "Back up, then replace with an empty database"),
            ],
            "administrator": [
                MenuItem("configure", "Configure Administrator", "Preserve or rotate web credentials"),
            ],
            "public_address": [
                MenuItem("configure", "Configure Public Address", "Stage domain configuration"),
            ],
            "service": [
                MenuItem("stage", "Stage Service Files", "Does not switch the live service"),
                MenuItem("activate", "Activate Managed Installation", "Final backup, cutover, health-check, rollback"),
                MenuItem("restart", "Restart Service"),
                MenuItem("logs", "View Logs", "Use mcp-bridge logs"),
            ],
            "verify": [
                MenuItem("standard", "Standard Verification", "Fast operational report"),
                MenuItem("deep", "Deep Diagnostics", "Extended read-only checks"),
                MenuItem("last", "Last Saved Report", "Read without running checks"),
                MenuItem("export", "Export Diagnostic Report", "Secret-free JSON; conversation content excluded"),
            ],
        }
        return [*mapping[section], MenuItem("return", "Return", "Back to the main menu")]

    def _execute(self, section: str, action: str) -> None:
        if section == "installation" and action == "adopt":
            with operation_lock(self.wizard.layout.operation_lock_file, self.wizard.layout.legacy_operation_lock_file):
                self.wizard._configure_adoption()
        elif section == "installation" and action == "uninstall":
            self._uninstall()
        elif section == "release" and action == "deploy":
            self._deploy_checkout()
        elif section == "release" and action == "stage":
            with operation_lock(self.wizard.layout.operation_lock_file, self.wizard.layout.legacy_operation_lock_file):
                self.wizard.installer.stage_release()
            self.wizard.output("PASS Bridge release staged; the live service was not changed.")
        elif section == "release" and action == "update":
            from bridge_cli.release import UpdateManager

            result = UpdateManager(self.wizard.layout, self.wizard.runner).check(force=True)
            if result.get("state") == "available":
                self.wizard.output(
                    f"UPDATE AVAILABLE: {result.get('current')} -> {result.get('latest')}"
                )
            elif result.get("state") == "current":
                self.wizard.output(f"PASS Bridge {result.get('current')} is current.")
            else:
                self.wizard.output(f"NOT CHECKED: {result.get('error', 'update state is unknown')}")
            self.wizard.input("Press Enter to return.")
        elif section == "database":
            self._database_action(action)
        elif section == "administrator" and action == "configure":
            with operation_lock(self.wizard.layout.operation_lock_file, self.wizard.layout.legacy_operation_lock_file):
                self.wizard._configure_administrator()
        elif section == "public_address" and action == "configure":
            with operation_lock(self.wizard.layout.operation_lock_file, self.wizard.layout.legacy_operation_lock_file):
                self.wizard._configure_public_address()
        elif section == "service" and action == "stage":
            with operation_lock(self.wizard.layout.operation_lock_file, self.wizard.layout.legacy_operation_lock_file):
                self.wizard._prepare_service()
        elif section == "service" and action == "activate":
            with operation_lock(self.wizard.layout.operation_lock_file, self.wizard.layout.legacy_operation_lock_file):
                self.wizard._activate()
        elif section == "service" and action == "restart":
            result = ServiceManager(self.wizard.layout, self.wizard.runner).mutate("restart")
            self.wizard.output(f"PASS {result['operation']}: active")
            self.wizard.input("Press Enter to return.")
        elif section == "service" and action == "logs":
            result = ServiceManager(self.wizard.layout, self.wizard.runner).logs(100)
            self.wizard.output(str(result["logs"]))
            self.wizard.input("Press Enter to return.")
        elif section == "verify":
            self._verify_action(action)

    def _deploy_checkout(self) -> None:
        from bridge_cli.release import CheckoutDeployer

        manager = CheckoutDeployer(self.wizard.layout, self.wizard.runner)
        plan = manager.plan(self.wizard.source_root)
        if plan.already_active:
            self.wizard.output(f"PASS Commit {plan.commit[:12]} is already active.")
            self.wizard.input("Press Enter to return.")
            return
        lines = [
            "Deploy current committed checkout",
            f"Source: {plan.source_root}",
            f"Branch: {plan.branch}",
            f"Commit: {plan.commit[:12]}",
            f"Release: {plan.release_id}",
            "- create a final database backup",
            "- briefly restart Bridge",
            "- verify health and roll back automatically on failure",
        ]
        if plan.untracked_files:
            lines.append(
                f"NOTICE {len(plan.untracked_files)} untracked file(s) will not be deployed."
            )
        self.wizard.output("\n".join(lines))
        if self.wizard.input("Deploy now? [y/N]: ").strip().lower() not in {"y", "yes"}:
            self.wizard.output("Deploy cancelled; nothing changed.")
            return
        result = manager.deploy(plan.source_root, expected_commit=plan.commit)
        self.wizard.output(
            f"PASS Deployed commit {result['commit'][:12]} as {result['release_id']}.\n"
            f"Safety backup: {result['database_backup']}"
        )
        self.wizard.input("Press Enter to return.")

    def _database_action(self, action: str) -> None:
        if action == "verify":
            result = self.database.verify()
        elif action == "backup":
            default = self._timestamped_path("mcp-bridge-database", ".sqlite3")
            raw = self.wizard.input(f"Backup destination [{default}]: ").strip()
            result = self.database.backup(Path(raw) if raw else default)
        elif action == "import":
            source = Path(self.wizard.input("SQLite database to import: ").strip()).expanduser()
            incoming = self.database.inspect(source)
            current = self.database.inspect()
            if not incoming.healthy:
                raise ValueError(incoming.error or "The selected file is not a healthy Bridge database.")
            self.wizard.output("This operation will REPLACE the complete current database.")
            self.wizard.output(f"Current database: {current.sessions} sessions")
            self.wizard.output(f"Imported database: {incoming.sessions} sessions")
            self.wizard.output("A final safety backup will be created before replacement.")
            if self.wizard.input("Replace the database now? [y/N]: ").strip().lower() not in {"y", "yes"}:
                self.wizard.output("Database import cancelled; nothing changed.")
                return
            result = self.database.import_replace(source)
        elif action == "migrate":
            result = self.database.migrate()
        elif action in {"optimize", "reset"}:
            warning = "REPLACE the database with an empty database" if action == "reset" else "rebuild and replace the SQLite file"
            self.wizard.output(f"WARNING: this will {warning}. A safety backup is created first.")
            if self.wizard.input("Continue? [y/N]: ").strip().lower() not in {"y", "yes"}:
                self.wizard.output("Cancelled; nothing changed.")
                return
            result = self.database.reset() if action == "reset" else self.database.optimize()
        else:
            return
        self.wizard.output(f"PASS {result['operation']}: {result['state']}")
        if result.get("backup"):
            self.wizard.output(f"Safety backup: {result['backup']}")
        if result.get("credentials_notice"):
            self.wizard.output(f"NOTICE {result['credentials_notice']}")
        self.wizard.input("Press Enter to return.")

    def _full_install(self) -> None:
        steps = {step.id: step for step in self.wizard._steps()}
        planned = [
            label for step_id, label in (
                ("installation", "choose installation type"),
                ("release", "stage Bridge files"),
                ("database", "prepare database"),
                ("administrator", "configure administrator"),
                ("public_address", "configure public address"),
                ("service", "stage background service"),
                ("activate", "activate and verify"),
            ) if steps[step_id].state not in {"READY", "ACTIVE"}
        ]
        if not planned:
            self.wizard.output("PASS Installation is already prepared or active. Run Verify installation.")
            self.wizard.input("Press Enter to return.")
            return
        self.wizard.output("Full installation plan:\n  - " + "\n  - ".join(planned))
        if self.wizard.input("Run this plan? [y/N]: ").strip().lower() not in {"y", "yes"}:
            self.wizard.output("Full installation cancelled; nothing changed.")
            return
        with operation_lock(self.wizard.layout.operation_lock_file, self.wizard.layout.legacy_operation_lock_file):
            if steps["installation"].state not in {"READY", "ACTIVE"}:
                self.wizard._configure_adoption()
            if steps["release"].state != "READY":
                self.wizard.installer.stage_release()
            if steps["database"].state != "READY":
                self.wizard._prepare_database()
            if steps["administrator"].state != "READY":
                self.wizard._configure_administrator()
            if steps["public_address"].state not in {"READY", "WAITING"}:
                self.wizard._configure_public_address()
            if steps["service"].state not in {"READY", "ACTIVE"}:
                self.wizard._prepare_service()
            self.wizard._activate(confirmed=True)

    def _uninstall(self) -> None:
        default = self._timestamped_path("mcp-bridge-database-export", ".sqlite3")
        raw = self.wizard.input(f"Database export path [{default}]: ").strip()
        export = Path(raw) if raw else default
        manager = UninstallManager(self.wizard.layout, self.wizard.runner)
        try:
            plan = manager.plan(export_to=export, remove_data=True)
        except RuntimeError as exc:
            if "ownership manifest is missing" not in str(exc):
                raise
            self.wizard.output("WARNING: this legacy installation has no ownership manifest.")
            if self.wizard.input("Allow removal of the known Bridge paths anyway? [y/N]: ").strip().lower() not in {"y", "yes"}:
                self.wizard.output("Uninstall cancelled; nothing changed.")
                return
            plan = manager.plan(export_to=export, remove_data=True, allow_unverified=True)
        self.wizard.output("Uninstall preview:")
        self.wizard.output(f"  - first export and verify the database at {export}")
        for target in plan.targets:
            self.wizard.output(f"  - remove {target}")
        self.wizard.output("  - preserve this source checkout")
        if self.wizard.input("Execute uninstall? [y/N]: ").strip().lower() not in {"y", "yes"}:
            self.wizard.output("Uninstall cancelled; nothing changed.")
            return
        result = manager.execute(plan)
        self.wizard.output(f"PASS Uninstall complete. Database export: {result['database_export']}")
        self.wizard.input("Press Enter to return.")

    def _verify_action(self, action: str) -> None:
        if action in {"standard", "deep"}:
            collector = StatusCollector(
                self.wizard.layout,
                self.wizard.runner,
                probe_http=self.wizard.layout.root == Path("/"),
                deep=action == "deep",
            )
            report = collector.collect()
            collector.write_snapshot(report)
            self.wizard.output(f"Bridge status: {report.overall.upper()}")
            for check in report.checks:
                self.wizard.output(f"[{check.state.upper()}] {check.label}: {check.message}")
        elif action == "last":
            snapshot = read_json(self.wizard.layout.status_file)
            self.wizard.output(json.dumps(snapshot or {"state": "not_available"}, indent=2))
        elif action == "export":
            snapshot = read_json(self.wizard.layout.status_file)
            if not snapshot:
                raise RuntimeError("No saved verification report exists. Run Standard or Deep Verification first.")
            default = self._timestamped_path("mcp-bridge-diagnostics", ".json")
            raw = self.wizard.input(f"Diagnostic export [{default}]: ").strip()
            destination = Path(raw) if raw else default
            atomic_write_json(destination, snapshot, 0o600)
            self.wizard.output(f"PASS Secret-free diagnostic report exported: {destination}")
        self.wizard.input("Press Enter to return.")

    @staticmethod
    def _timestamped_path(prefix: str, suffix: str) -> Path:
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        return Path.cwd() / f"{prefix}-{timestamp}{suffix}"
