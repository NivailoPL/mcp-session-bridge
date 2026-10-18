"""Admin API and page for the Topics lab.

It reuses the admin login, CSRF check and JSON helpers of ``AdminHandlers``.
Every run starts from an explicit admin request; nothing here schedules work.
The OpenRouter key is stored encrypted and never returned.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.fernet import InvalidToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from app.topics import POOL_KEY, TOPIC_OPENROUTER_KEY_SETTING, TopicError, default_topic_settings

if TYPE_CHECKING:
    from app.admin import AdminHandlers
    from app.topic_runtime import TopicRuntime

_NOT_FOUND_CODES = {"version_unknown", "run_unknown", "proposal_unknown"}
_CONFLICT_CODES = {
    "run_active", "run_paused", "run_not_paused", "run_not_running", "map_changed", "feature_off",
    "key_missing", "map_missing", "nothing_to_do", "proposal_closed", "proposal_outdated", "codex_unavailable",
    "run_not_stoppable",
}
SECOND_TOPIC_MIN = 0.2
OPENROUTER_KEY_MAX_CHARS = 300


class TopicAdminHandlers:
    def __init__(self, admin: AdminHandlers, runtime: TopicRuntime, html_path: Path) -> None:
        self.admin = admin
        self.store = admin.store
        self.runtime = runtime
        self.html_path = html_path

    # ---------- key ----------

    def read_key(self) -> str:
        encrypted = self.store.get_app_setting(TOPIC_OPENROUTER_KEY_SETTING)
        if not encrypted:
            return ""
        try:
            return self.admin._decrypt_secret(encrypted)
        except (InvalidToken, ValueError):
            return ""

    # ---------- page ----------

    async def page(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        try:
            body = self.html_path.read_text(encoding="utf-8")
        except OSError:
            return HTMLResponse("Topics workspace is not installed.", status_code=500,
                                headers=self.admin._no_store_headers())
        return HTMLResponse(body, headers=self.admin._admin_headers())

    # ---------- overview and map ----------

    async def api_overview(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        return self._ok(await asyncio.to_thread(self._overview))

    def _overview(self) -> dict[str, Any]:
        settings = self.store.get_topic_settings()
        version = self.store.active_topic_version()
        open_proposals = self.store.list_topic_proposals(("testing", "pending", "outdated", "failed"))
        return {
            "enabled": self.store.topics_enabled(),
            "key_set": bool(self.read_key()),
            "codex_configured": self.runtime.codex is not None,
            "settings": settings,
            "defaults": default_topic_settings(),
            "active_version": version,
            "versions": self.store.list_topic_versions(),
            "counts": self.store.topic_overview_counts(
                version["version_id"] if version else None, settings["include_sensitive"]
            ),
            "pool_count": len(self.runtime.pool_chapters()),
            "pending_proposals": sum(1 for p in open_proposals if p["status"] == "pending"),
            "open_proposals": len(open_proposals),
            "last_find": self.store.last_topic_run("find"),
            "last_refine": self.store.last_topic_run("refine"),
            "last_proposing": self.store.latest_proposing_run(),
            "active_run": self.store.active_topic_run(),
            "running": self.runtime.running(),
            "job": self.runtime.job,
            "cost_per_chapter_usd": round(self.runtime.cost_per_chapter(), 6),
            "runs": self.store.list_topic_runs(10),
        }

    async def api_get_version(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        version_id, id_error = _int_param(request, "version_id")
        if id_error:
            return id_error
        version = self.store.get_topic_version(version_id)
        if version is None:
            return self._error("Unknown map version.", 404, "version_unknown")
        return self._ok({"version": version})

    async def api_create_version(self, request: Request) -> Response:
        payload, session, error = await self._mutation(request, allowed={"topics", "note", "parent_id", "origin"})
        if error:
            return error
        origin = payload.get("origin", "manual")
        if origin not in ("import", "manual"):
            return self._error("origin must be import or manual.", 400, "payload_invalid")
        parent_id = payload.get("parent_id")
        if parent_id is not None and (isinstance(parent_id, bool) or not isinstance(parent_id, int)):
            return self._error("parent_id must be an integer.", 400, "payload_invalid")
        if self.runtime.running():
            return self._error("Wait for the running classification before changing the map.", 409, "run_active")

        def create() -> dict[str, Any]:
            version = self.store.create_topic_version(
                payload.get("topics"), origin=origin, actor=session["username"],
                note=payload.get("note"), parent_id=parent_id,
            )
            verb = "Imported" if origin == "import" else "Saved"
            note = f" Note: {version['note']}" if version["note"] else ""
            self.store.add_topic_journal(
                "owner", f"map.{origin}",
                f"{verb} map v{version['version_id']} with {version['topic_count']} topics and made it active.{note}",
                version_id=version["version_id"], data={"parent_id": parent_id},
            )
            return {"version": version}

        return self._call(create)

    async def api_stop(self, request: Request) -> Response:
        _, session, error = await self._mutation(request, allowed=set(), allow_empty=True)
        if error:
            return error
        return self._call(lambda: self.runtime.stop(actor=session["username"]))

    async def api_recolor(self, request: Request) -> Response:
        _, session, error = await self._mutation(request, allowed=set(), allow_empty=True)
        if error:
            return error
        return self._call(lambda: {"version": self.runtime.recolor(actor=session["username"])})

    async def api_start_over(self, request: Request) -> Response:
        _, session, error = await self._mutation(request, allowed=set(), allow_empty=True)
        if error:
            return error
        return self._call(lambda: self.runtime.start_over(actor=session["username"]))

    async def api_activate_version(self, request: Request) -> Response:
        _, _, error = await self._mutation(request, allowed=set(), allow_empty=True)
        if error:
            return error
        version_id, id_error = _int_param(request, "version_id")
        if id_error:
            return id_error
        if self.runtime.running():
            return self._error("Wait for the running classification before changing the map.", 409, "run_active")

        return self._call(lambda: {"version": self.runtime.restore(version_id, actor="owner")})

    async def api_assignments(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        return self._ok(await asyncio.to_thread(self._assignments))

    def _assignments(self) -> dict[str, Any]:
        version = self.store.active_topic_version()
        current = self.store.topic_current_assignments(version["version_id"]) if version else {}
        rows = []
        for chapter in self.store.topic_chapters():
            item = current.get(chapter["strand_id"])
            second = None
            if item:
                ranked = sorted(
                    ((key, value) for key, value in item["probabilities"].items() if key != item["topic_key"]),
                    key=lambda pair: pair[1], reverse=True,
                )
                if ranked and ranked[0][1] >= SECOND_TOPIC_MIN:
                    second = {"key": ranked[0][0], "p": ranked[0][1]}
            rows.append({
                **chapter,
                "topic_key": item["topic_key"] if item else None,
                "confidence": item["confidence"] if item else None,
                "probabilities": item["probabilities"] if item else {},
                "second": second,
                "run_id": item["run_id"] if item else None,
                "changed": bool(item) and item["chapter_title"] != chapter["title"],
            })
        return {"version": version, "pool_key": POOL_KEY, "chapters": rows}

    # ---------- runs ----------

    async def api_estimate(self, request: Request) -> Response:
        payload, _, error = await self._mutation(request, allowed={"scope"}, allow_empty=True)
        if error:
            return error
        return self._call(lambda: {"estimate": self.runtime.estimate(payload.get("scope"))})

    async def api_start_run(self, request: Request) -> Response:
        payload, session, error = await self._mutation(request, allowed={"scope"}, allow_empty=True)
        if error:
            return error
        try:
            run = await self.runtime.start_classify(payload.get("scope"), actor=session["username"])
        except TopicError as exc:
            return self._domain_error(exc)
        return JSONResponse({"ok": True, "run": run}, status_code=202, headers=self.admin._no_store_headers())

    async def api_get_run(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        run_id, id_error = _int_param(request, "run_id")
        if id_error:
            return id_error
        run = self.store.get_topic_run(run_id)
        if run is None:
            return self._error("Unknown topics run.", 404, "run_unknown")
        return self._ok({"run": run, "running": self.runtime.running()})

    async def api_export_run(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        run_id, id_error = _int_param(request, "run_id")
        if id_error:
            return id_error
        export = await asyncio.to_thread(run_export, self.store, run_id)
        if export is None:
            return self._error("Unknown topics run.", 404, "run_unknown")
        return JSONResponse(
            export,
            headers={
                **self.admin._no_store_headers(),
                "Content-Disposition": f'attachment; filename="topics-run-{run_id}.json"',
            },
        )

    async def api_run_action(self, request: Request) -> Response:
        _, session, error = await self._mutation(request, allowed=set(), allow_empty=True)
        if error:
            return error
        run_id, id_error = _int_param(request, "run_id")
        if id_error:
            return id_error
        action = request.path_params.get("action")
        try:
            if action == "pause":
                run = self.runtime.request_pause(run_id)
            elif action == "resume":
                run = await self.runtime.resume(run_id, actor=session["username"])
            elif action == "close":
                run = await asyncio.to_thread(self.runtime.close_run, run_id)
            else:
                return self._error("Unknown action.", 404, "action_unknown")
        except TopicError as exc:
            return self._domain_error(exc)
        return self._ok({"run": run})

    # ---------- find, refine, proposals ----------

    async def api_find(self, request: Request) -> Response:
        return await self._start(request, "find")

    async def api_refine(self, request: Request) -> Response:
        return await self._start(request, "refine")

    async def _start(self, request: Request, kind: str) -> Response:
        payload, session, error = await self._mutation(request, allowed={"detail", "note"}, allow_empty=True)
        if error:
            return error
        starter = self.runtime.start_find if kind == "find" else self.runtime.start_refine
        try:
            run = await starter(payload, actor=session["username"])
        except TopicError as exc:
            return self._domain_error(exc)
        return JSONResponse({"ok": True, "run": run}, status_code=202, headers=self.admin._no_store_headers())

    async def api_test_edit(self, request: Request) -> Response:
        payload, session, error = await self._mutation(request, allowed={"topics", "note"})
        if error:
            return error
        try:
            proposal = await self.runtime.start_test_edit(payload.get("topics"), note=payload.get("note"),
                                                          actor=session["username"])
        except TopicError as exc:
            return self._domain_error(exc)
        return JSONResponse({"ok": True, "proposal": proposal}, status_code=202, headers=self.admin._no_store_headers())

    async def api_proposals(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        statuses = None if request.query_params.get("all") == "1" else ("testing", "pending", "outdated", "failed")
        proposals = await asyncio.to_thread(self._with_timing, self.store.list_topic_proposals(statuses))
        return self._ok({"proposals": proposals, "job": self.runtime.job})

    def _with_timing(self, proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """How long the proposing and the test took, and where each chapter landed in a first-map test."""
        for proposal in proposals:
            source = self.store.get_topic_run(proposal["run_id"]) if proposal.get("run_id") else None
            test = self.store.get_topic_run(proposal["test_run_id"]) if proposal.get("test_run_id") else None

            def took(run: dict[str, Any] | None) -> int | None:
                if not run or not run.get("finished_at"):
                    return None
                return max(0, run["finished_at"] - (run.get("started_at") or run["created_at"]))

            proposing, testing = took(source), took(test)
            proposal["timing"] = {
                "proposing_seconds": proposing,
                "testing_seconds": testing,
                "total_seconds": (proposing or 0) + (testing or 0) if proposing is not None or testing is not None else None,
                "proposing_model": (source or {}).get("settings", {}).get("codex_model"),
                "proposing_effort": (source or {}).get("settings", {}).get("codex_effort"),
                "testing_model": (test or {}).get("settings", {}).get("jev_model"),
            }
            if proposal["op"] == "first_map" and proposal["status"] == "pending" and test:
                proposal["landing"] = {
                    str(cid): [item["topic_key"], item["confidence"]]
                    for cid, item in self.store.topic_run_assignments(test["run_id"]).items()
                }
        return proposals

    async def api_proposal_action(self, request: Request) -> Response:
        payload, session, error = await self._mutation(request, allowed={"note", "topics"}, allow_empty=True)
        if error:
            return error
        proposal_id, id_error = _int_param(request, "proposal_id")
        if id_error:
            return id_error
        action = request.path_params.get("action")
        actor = session["username"]
        note = payload.get("note") or ""
        try:
            if action == "accept":
                body = await asyncio.to_thread(self.runtime.accept, proposal_id, actor=actor, note=note)
            elif action == "reject":
                body = await asyncio.to_thread(self.runtime.reject, proposal_id, actor=actor, note=note)
            elif action == "retest":
                body = {"proposal": await self.runtime.retest(proposal_id, actor=actor)}
            elif action == "revise":
                body = {"proposal": await self.runtime.revise(proposal_id, payload.get("topics"), actor=actor)}
            else:
                return self._error("Unknown action.", 404, "action_unknown")
        except TopicError as exc:
            return self._domain_error(exc)
        return self._ok(body)

    async def api_journal(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        return self._ok({"journal": self.store.list_topic_journal(limit=300)})

    # ---------- settings ----------

    async def api_update_settings(self, request: Request) -> Response:
        payload, _, error = await self._mutation(request, allowed=set(default_topic_settings()))
        if error:
            return error

        def update() -> dict[str, Any]:
            settings = self.store.set_topic_settings(payload)
            self.store.add_topic_journal(
                "owner", "settings.update", f"Changed settings: {', '.join(sorted(payload))}.", data=payload
            )
            return {"settings": settings}

        return self._call(update)

    async def api_update_key(self, request: Request) -> Response:
        payload, _, error = await self._mutation(request, allowed={"api_key"})
        if error:
            return error
        value = payload.get("api_key")
        if value is None or value == "":
            self.store.delete_app_setting(TOPIC_OPENROUTER_KEY_SETTING)
            self.store.add_topic_journal("owner", "settings.key", "Removed the OpenRouter API key.")
            return self._ok({"key_set": False})
        if not isinstance(value, str) or len(value.strip()) > OPENROUTER_KEY_MAX_CHARS or not value.strip():
            return self._error("Paste the OpenRouter API key as text.", 400, "key_invalid")
        self.store.set_app_setting(TOPIC_OPENROUTER_KEY_SETTING, self.admin._encrypt_secret(value.strip()))
        self.store.add_topic_journal("owner", "settings.key", "Saved a new OpenRouter API key.")
        return self._ok({"key_set": True})

    # ---------- helpers ----------

    async def _mutation(
        self, request: Request, *, allowed: set[str], allow_empty: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any], Response | None]:
        from app.admin import _json_body

        session, error = self.admin._require_admin_mutation(request)
        if error:
            return {}, {}, error
        payload, parse_error = await _json_body(request, allow_empty=allow_empty)
        if parse_error:
            return {}, {}, parse_error
        unknown = set(payload) - allowed
        if unknown:
            return {}, {}, self._error(f"Unknown fields: {', '.join(sorted(unknown))}.", 400, "payload_invalid")
        return payload, session, None

    def _call(self, action: Callable[[], dict[str, Any]]) -> Response:
        try:
            return self._ok(action())
        except TopicError as exc:
            return self._domain_error(exc)

    def _domain_error(self, exc: TopicError) -> JSONResponse:
        if exc.code in _NOT_FOUND_CODES:
            status = 404
        elif exc.code in _CONFLICT_CODES:
            status = 409
        else:
            status = 422
        return self._error(str(exc), status, exc.code)

    def _ok(self, body: dict[str, Any]) -> JSONResponse:
        return JSONResponse({"ok": True, **body}, headers=self.admin._no_store_headers())

    def _error(self, message: str, status_code: int, code: str) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": message, "code": code},
            status_code=status_code,
            headers=self.admin._no_store_headers(),
        )


def run_export(store: Any, run_id: int) -> dict[str, Any] | None:
    """One run with its map, input, every request and raw answer. Shared by the admin and the CLI."""
    run = store.get_topic_run(run_id)
    if run is None:
        return None
    return {
        "run": run,
        "input": store.get_topic_run_input(run_id),
        "map": store.get_topic_version(run["version_id"]),
        "items": store.topic_run_items(run_id),
        "journal": [entry for entry in store.list_topic_journal(limit=1000) if entry["run_id"] == run_id],
    }


def _int_param(request: Request, name: str) -> tuple[int, Response | None]:
    try:
        return int(request.path_params[name]), None
    except (KeyError, TypeError, ValueError):
        return 0, JSONResponse({"ok": False, "error": f"Invalid {name}.", "code": "id_invalid"}, status_code=400)
