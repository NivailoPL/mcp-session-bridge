from __future__ import annotations

import getpass
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from bridge_cli.config import read_env_file
from bridge_cli.files import atomic_write_json, read_json
from bridge_cli.install import ManagedInstaller
from bridge_cli.layout import Layout
from bridge_cli.presentation import TerminalTheme
from bridge_cli.runner import Runner


@dataclass(frozen=True)
class SetupStep:
    number: int
    id: str
    label: str
    state: str
    detail: str


class SetupInspector:
    def __init__(
        self,
        layout: Layout,
        source_root: Path,
        runner: Runner,
        *,
        legacy_db: Path | None = None,
        legacy_env: Path | None = None,
    ):
        self.layout = layout
        self.source_root = source_root
        self.runner = runner
        self.legacy_db = legacy_db
        self.legacy_env = legacy_env

    def collect(self) -> list[SetupStep]:
        managed_env = read_env_file(self.layout.pending_env_file) or read_env_file(
            self.layout.env_file
        )
        legacy_env = read_env_file(self.legacy_env) if self.legacy_env else {}
        effective_env = managed_env or legacy_env
        installation = read_json(self.layout.installation_file) or {}
        setup_state = read_json(self.layout.setup_file) or {}
        adoption_decided = "adopt_legacy" in setup_state
        managed_mode = installation.get("mode") == "managed"
        prepared = installation.get("mode") == "prepared"
        legacy_detected = bool(
            (self.legacy_db and self.legacy_db.exists())
            or (self.legacy_env and self.legacy_env.exists())
        )

        missing_tools = [name for name in ("systemctl", "curl", "uv") if shutil.which(name) is None]
        server_state = "READY" if self.source_root.exists() and not missing_tools else "ATTENTION"
        server_detail = (
            "Source checkout and host prerequisites are available."
            if server_state == "READY"
            else "Missing host prerequisites: " + ", ".join(missing_tools)
            if missing_tools
            else "Source checkout is unavailable."
        )
        installation_state = (
            "ACTIVE"
            if managed_mode
            else "READY"
            if prepared or adoption_decided
            else "DETECTED"
            if legacy_detected
            else "NOT STARTED"
        )
        installation_detail = (
            "Managed installation is active."
            if managed_mode
            else "Managed installation is prepared and awaiting activation."
            if prepared
            else "Existing installation will be adopted."
            if adoption_decided and setup_state.get("adopt_legacy")
            else "Fresh managed installation selected."
            if adoption_decided
            else "Existing Bridge files were found and can be adopted."
            if legacy_detected
            else "No existing Bridge installation was detected."
        )

        db_path = self.layout.db_path if self.layout.db_path.exists() else self.legacy_db
        db_state = "READY" if self.layout.db_path.exists() else "DETECTED" if db_path and db_path.exists() else "NOT STARTED"
        db_detail = "No database has been initialized."
        if db_path and db_path.exists():
            count, error = self._session_count(db_path)
            if error:
                db_state = "ATTENTION"
                db_detail = f"Database cannot be inspected: {error}."
            else:
                noun = "session" if count == 1 else "sessions"
                prefix = "Managed database" if db_path == self.layout.db_path else "Existing database"
                db_detail = f"{prefix}: {count} {noun}."

        admin_ready = bool(effective_env.get("BRIDGE_OWNER_PASSWORD_HASH"))
        admin_confirmed = managed_mode or prepared or bool(setup_state.get("administrator_configured"))
        admin_state = "READY" if admin_ready and admin_confirmed else "DETECTED" if admin_ready else "NEEDS INPUT"
        username = effective_env.get("BRIDGE_OWNER_USERNAME") or "not configured"

        public_url = effective_env.get("BRIDGE_PUBLIC_BASE_URL")
        public_confirmed = managed_mode or prepared or bool(setup_state.get("public_address_configured"))
        public_state = "READY" if public_url and public_confirmed else "DETECTED" if public_url else "NEEDS INPUT"
        public_detail = public_url or "A public hostname is required."

        staged_release = setup_state.get("release_dir")
        release_ready = managed_mode and self.layout.current_link.exists()
        if isinstance(staged_release, str):
            release_ready = release_ready or Path(staged_release).is_dir()
        release_state = "READY" if release_ready else "NOT INSTALLED"
        pending_service = self.layout.pending_service_unit.exists()
        service_text = self.layout.service_unit.read_text(encoding="utf-8") if self.layout.service_unit.exists() else ""
        managed_unit = str(self.layout.current_link) in service_text
        active = self._service_active()
        service_state = (
            "ACTIVE"
            if active and managed_unit
            else "READY"
            if pending_service
            else "DETECTED"
            if active
            else "NOT INSTALLED"
        )
        service_detail = (
            "Managed service is active."
            if active and managed_unit
            else "Managed service files are staged; the legacy service remains active."
            if pending_service and active
            else "A legacy Bridge service is currently active."
            if active
            else "Managed service files are staged."
            if pending_service
            else "Managed service files have not been prepared."
        )

        activation_state = "ACTIVE" if managed_mode and active else "READY" if prepared else "NOT STARTED"
        verify_state = "READY" if managed_mode and active else "WAITING"

        return [
            SetupStep(1, "server", "Server readiness", server_state, server_detail),
            SetupStep(2, "installation", "Existing installation", installation_state, installation_detail),
            SetupStep(3, "release", "Bridge files", release_state, "Release is staged." if release_state == "READY" else "Release has not been staged."),
            SetupStep(4, "database", "Database", db_state, db_detail),
            SetupStep(5, "administrator", "Administrator", admin_state, f"Owner: {username}."),
            SetupStep(6, "public_address", "Public address", public_state, public_detail),
            SetupStep(7, "service", "Background service", service_state, service_detail),
            SetupStep(8, "activate", "Activate installation", activation_state, "Activation switches the live service only after confirmation."),
            SetupStep(9, "verify", "Verify everything", verify_state, "Run operational checks after activation."),
        ]

    def _service_active(self) -> bool:
        result = self.runner.run("systemctl", "is-active", "mcp-session-bridge.service", check=False)
        return result.returncode == 0 and result.stdout.strip() == "active"

    @staticmethod
    def _session_count(path: Path) -> tuple[int, str | None]:
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
                row = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()
                return (int(row[0]) if row else 0, None)
        except sqlite3.Error as exc:
            return 0, str(exc)


def render_setup_dashboard(steps: list[SetupStep], theme: TerminalTheme) -> str:
    lines = [theme.heading("MCP Session Bridge"), theme.heading("Setup & configuration"), ""]
    width = max(len(step.label) for step in steps)
    for step in steps:
        lines.append(f"  {step.number}. {step.label:<{width}}  {step.state}")
        lines.append(f"     {step.detail}")
    lines.extend(["", "Choose a step, press Enter for the recommended next step, or q to exit."])
    return "\n".join(lines)


class SetupWizard:
    def __init__(
        self,
        layout: Layout,
        source_root: Path,
        runner: Runner,
        *,
        legacy_db: Path | None,
        legacy_env: Path | None,
        theme: TerminalTheme,
        input_fn: Callable[[str], str] = input,
        password_fn: Callable[[str], str] = getpass.getpass,
        output_fn: Callable[[str], None] = print,
    ):
        self.layout = layout
        self.source_root = source_root
        self.runner = runner
        self.legacy_db = legacy_db
        self.legacy_env = legacy_env
        self.theme = theme
        self.input = input_fn
        self.password = password_fn
        self.output = output_fn
        self.installer = ManagedInstaller(layout, source_root, runner)

    def run(self) -> int:
        while True:
            steps = self._steps()
            self.output(render_setup_dashboard(steps, self.theme))
            recommended = self._recommended(steps)
            raw = self.input(f"Step [{recommended}]: ").strip().lower()
            if raw in {"q", "quit", "exit"}:
                self.output("Setup saved. Run mcp-bridge setup to continue.")
                return 0
            selection = raw or str(recommended)
            choice = int(selection) if selection.isdigit() else 0
            if choice not in range(1, 10):
                self.output("Choose a number from 1 to 9.")
                continue
            if choice == 1:
                readiness = next(step for step in steps if step.id == "server")
                self.output(f"{readiness.state} {readiness.detail}")
            elif choice == 2:
                self._configure_adoption()
            elif choice == 3:
                self.installer.stage_release()
                self.output("PASS Bridge release staged; the live service was not changed.")
            elif choice == 4:
                self._prepare_database()
            elif choice == 5:
                self._configure_administrator()
            elif choice == 6:
                self._configure_public_address()
            elif choice == 7:
                self._prepare_service()
            elif choice == 8:
                self._activate()
            else:
                self._verify()

    def _steps(self) -> list[SetupStep]:
        return SetupInspector(
            self.layout,
            self.source_root,
            self.runner,
            legacy_db=self.legacy_db,
            legacy_env=self.legacy_env,
        ).collect()

    @staticmethod
    def _recommended(steps: list[SetupStep]) -> int:
        by_id = {step.id: step for step in steps}
        required_states = (
            ("installation", {"READY", "ACTIVE"}),
            ("release", {"READY"}),
            ("database", {"READY"}),
            ("administrator", {"READY"}),
            ("public_address", {"READY", "WAITING"}),
            ("service", {"READY", "ACTIVE"}),
            ("activate", {"ACTIVE"}),
        )
        for step_id, completed in required_states:
            if by_id[step_id].state not in completed:
                return by_id[step_id].number
        return 9

    def _state(self) -> dict[str, object]:
        return read_json(self.layout.setup_file) or {"format_version": 1}

    def _write_state(self, values: dict[str, object]) -> None:
        state = self._state()
        state.update(values)
        atomic_write_json(self.layout.setup_file, state, 0o600)

    def _configure_adoption(self) -> None:
        if not self.legacy_db and not self.legacy_env:
            self._write_state({"adopt_legacy": False})
            self.output("PASS Fresh installation selected.")
            return
        answer = self.input("Adopt the detected Bridge data and configuration? [Y/n]: ").strip().lower()
        adopt = answer not in {"n", "no"}
        state = self._state()
        previous = state.get("adopt_legacy")
        if (
            isinstance(previous, bool)
            and previous != adopt
            and (self.layout.db_path.exists() or self.layout.pending_env_file.exists())
        ):
            raise RuntimeError(
                "The adoption choice cannot change after database or configuration staging."
            )
        self._write_state({"adopt_legacy": adopt})
        self.output("PASS Existing installation selected for adoption." if adopt else "PASS Fresh managed installation selected.")

    def _adopt(self) -> bool:
        state = self._state()
        if "adopt_legacy" not in state:
            raise RuntimeError("Choose step 2 and confirm the installation type first.")
        return bool(state["adopt_legacy"])

    def _prepare_database(self) -> None:
        self.installer.stage_database(self.legacy_db if self._adopt() else None)
        self.output("PASS Database staged; the source database was preserved.")

    def _configure_administrator(self) -> None:
        existing = read_env_file(self.layout.pending_env_file) or read_env_file(
            self.layout.env_file
        ) or (read_env_file(self.legacy_env) if self.legacy_env and self._adopt() else {})
        if existing.get("BRIDGE_OWNER_PASSWORD_HASH"):
            keep = self.input("Keep the existing administrator credentials? [Y/n]: ").strip().lower()
            if keep not in {"n", "no"}:
                self.installer.configure_administrator(None, None, self.legacy_env if self._adopt() else None)
                self._write_state({"administrator_configured": True})
                self.output("PASS Existing administrator credentials preserved.")
                return
        username = self.input(f"Owner username [{existing.get('BRIDGE_OWNER_USERNAME') or 'owner'}]: ").strip() or existing.get("BRIDGE_OWNER_USERNAME") or "owner"
        password = self.password("Owner password: ")
        confirmation = self.password("Repeat owner password: ")
        if password != confirmation:
            raise ValueError("Passwords do not match.")
        self.installer.configure_administrator(username, password, self.legacy_env if self._adopt() else None)
        self._write_state({"administrator_configured": True})
        self.output("PASS Administrator staged.")

    def _configure_public_address(self) -> None:
        existing = read_env_file(self.layout.pending_env_file) or read_env_file(
            self.layout.env_file
        ) or (read_env_file(self.legacy_env) if self.legacy_env and self._adopt() else {})
        current = urlparse(existing.get("BRIDGE_PUBLIC_BASE_URL", "")).hostname or ""
        domain = self.input(f"Domain name{f' [{current}]' if current else ''}: ").strip() or current
        self.installer.configure_domain(domain, self.legacy_env if self._adopt() else None)
        self._write_state({"public_address_configured": True})
        self.output("PASS Public address staged; DNS and Caddy were not changed.")

    def _prepare_service(self) -> None:
        self.installer.stage_service()
        self.output("PASS Managed service files staged; the live service was not changed.")

    def _activate(self) -> None:
        self.output(
            self.theme.heading("Activation plan")
            + "\n  - create a final backup\n  - briefly stop the current Bridge\n"
            + "  - take the final SQLite copy\n  - switch and verify the managed service\n"
            + "  - restore the previous service automatically on failure"
        )
        if self.input("Activate now? [y/N]: ").strip().lower() not in {"y", "yes"}:
            self.output("Activation cancelled; the current service was not changed.")
            return
        result = self.installer.activate(self.legacy_db if self._adopt() else None)
        self.output(f"PASS Activation state: {result['state']}.")

    def _verify(self) -> None:
        from bridge_cli.operations import StatusCollector

        report = StatusCollector(self.layout, self.runner, probe_http=self.layout.root == Path("/"), deep=True).collect()
        self.output(f"Bridge status: {report.overall.upper()}")
        for check in report.checks:
            self.output(f"[{check.state.upper()}] {check.label}: {check.message}")
