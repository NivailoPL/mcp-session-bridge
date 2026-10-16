"""Admin API for highlights and strands inside the session view.

It reuses the admin login, CSRF check and JSON helpers of ``AdminHandlers``.
Strand runs start only from an explicit admin request; nothing here schedules
work on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.highlights import HighlightError
from app.strands import StrandError, default_strand_settings

if TYPE_CHECKING:
    from app.admin import AdminHandlers
    from app.strand_runtime import StrandRuntime

_NOT_FOUND_CODES = {"session_unknown", "strand_unknown", "run_unknown", "exchange_unknown"}
_CONFLICT_CODES = {"pending_exists", "run_active", "sensitive_unconfirmed", "feature_off", "already_complete"}


class AnchorAdminHandlers:
    def __init__(self, admin: AdminHandlers, runtime: StrandRuntime) -> None:
        self.admin = admin
        self.store = admin.store
        self.runtime = runtime

    # ---------- highlights ----------

    async def api_list_highlights(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        session_id = request.path_params["session_id"]
        if self.store.get_session(session_id) is None:
            return self._error("Unknown session.", 404, "session_unknown")
        return self._ok({"highlights": self.store.list_highlights(session_id)})

    async def api_create_highlight(self, request: Request) -> Response:
        payload, error = await self._mutation(request, allowed={"exchange_id", "side", "quote"})
        if error:
            return error
        session_id = request.path_params["session_id"]
        if self.store.get_session(session_id) is None:
            return self._error("Unknown session.", 404, "session_unknown")
        return self._run(
            lambda: {
                "highlight": self.store.create_highlight(
                    session_id, payload.get("exchange_id"), payload.get("side"), payload.get("quote")
                )
            }
        )

    async def api_delete_highlight(self, request: Request) -> Response:
        _, error = await self._mutation(request, allowed=set(), allow_empty=True)
        if error:
            return error
        highlight_id, id_error = _int_param(request, "highlight_id")
        if id_error:
            return id_error
        if not self.store.delete_highlight(highlight_id):
            return self._error("Unknown highlight.", 404, "highlight_unknown")
        return self._ok({"deleted": highlight_id})

    # ---------- strands ----------

    async def api_list_strands(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        session_id = request.path_params["session_id"]
        if self.store.get_session(session_id) is None:
            return self._error("Unknown session.", 404, "session_unknown")
        return self._ok(self._session_strands(session_id))

    async def api_create_strand(self, request: Request) -> Response:
        payload, error = await self._mutation(request, allowed={"title", "ranges"})
        if error:
            return error
        session_id = request.path_params["session_id"]
        return self._run(
            lambda: {"strand": self.store.create_strand(session_id, payload.get("title"), payload.get("ranges"))}
        )

    async def api_update_strand(self, request: Request) -> Response:
        payload, error = await self._mutation(request, allowed={"title", "ranges"})
        if error:
            return error
        strand_id, id_error = _int_param(request, "strand_id")
        if id_error:
            return id_error
        return self._run(
            lambda: {
                "strand": self.store.update_strand(strand_id, title=payload.get("title"), ranges=payload.get("ranges"))
            }
        )

    async def api_delete_strand(self, request: Request) -> Response:
        _, error = await self._mutation(request, allowed=set(), allow_empty=True)
        if error:
            return error
        strand_id, id_error = _int_param(request, "strand_id")
        if id_error:
            return id_error
        if not self.store.delete_strand(strand_id):
            return self._error("Unknown strand.", 404, "strand_unknown")
        return self._ok({"deleted": strand_id})

    async def api_accept_strands(self, request: Request) -> Response:
        return await self._pending_action(request, self.store.accept_strands, "accepted")

    async def api_discard_strands(self, request: Request) -> Response:
        return await self._pending_action(request, self.store.discard_pending_strands, "discarded")

    async def _pending_action(self, request: Request, action: Callable[..., int], key: str) -> Response:
        payload, error = await self._mutation(request, allowed={"strand_id"}, allow_empty=True)
        if error:
            return error
        strand_id = payload.get("strand_id")
        if strand_id is not None and (isinstance(strand_id, bool) or not isinstance(strand_id, int)):
            return self._error("strand_id must be an integer.", 400, "strand_invalid")
        session_id = request.path_params["session_id"]
        count = action(session_id, strand_id)
        return self._ok({key: count, **self._session_strands(session_id)})

    # ---------- Codex runs ----------

    async def api_start_strand_run(self, request: Request) -> Response:
        session, error = self.admin._require_admin_mutation(request)
        if error:
            return error
        from app.admin import _json_body

        payload, parse_error = await _json_body(request, allow_empty=True)
        if parse_error:
            return parse_error
        if not set(payload) <= {"confirm_sensitive", "redo"}:
            return self._error("Only confirm_sensitive and redo are accepted.", 400, "payload_invalid")
        confirm = payload.get("confirm_sensitive", False)
        redo = payload.get("redo", False)
        if not isinstance(confirm, bool) or not isinstance(redo, bool):
            return self._error("confirm_sensitive and redo must be true or false.", 400, "payload_invalid")
        session_id = request.path_params["session_id"]
        try:
            run = await self.runtime.start_run(
                session_id, actor=session["username"], confirm_sensitive=confirm, redo=redo
            )
        except (StrandError, HighlightError) as exc:
            return self._domain_error(exc)
        return JSONResponse({"ok": True, "run": run}, status_code=202, headers=self.admin._no_store_headers())

    async def api_get_strand_run(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        run_id, id_error = _int_param(request, "run_id")
        if id_error:
            return id_error
        run = self.store.get_strand_run(run_id)
        if run is None:
            return self._error("Unknown Codex run.", 404, "run_unknown")
        return self._ok({"run": run})

    async def api_revert_strand_run(self, request: Request) -> Response:
        _, error = await self._mutation(request, allowed=set(), allow_empty=True)
        if error:
            return error
        run_id, id_error = _int_param(request, "run_id")
        if id_error:
            return id_error
        return self._run(lambda: {"reverted": self.store.revert_strand_run(run_id)})

    # ---------- settings ----------

    async def api_get_strand_settings(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        return self._ok({"settings": self.store.get_strand_settings(), "defaults": default_strand_settings()})

    async def api_update_strand_settings(self, request: Request) -> Response:
        payload, error = await self._mutation(request, allowed={"model", "effort", "prompt"})
        if error:
            return error
        return self._run(lambda: {"settings": self.store.set_strand_settings(payload)})

    async def api_get_token_features(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        return self._ok({"features": self.store.get_token_features()})

    async def api_update_token_features(self, request: Request) -> Response:
        payload, error = await self._mutation(request, allowed={"enabled", "strands"})
        if error:
            return error
        return self._run(lambda: {"features": self.store.set_token_features(payload)})

    # ---------- helpers ----------

    def _session_strands(self, session_id: str) -> dict[str, Any]:
        return {
            "strands": self.store.list_strands(session_id),
            "latest_run": self.store.latest_strand_run(session_id),
            "codex_configured": self.runtime.codex is not None,
            "strands_enabled": self.store.get_token_features()["strands_active"],
            "chapter_state": self.store.chapter_state(session_id),
        }

    async def _mutation(
        self, request: Request, *, allowed: set[str], allow_empty: bool = False
    ) -> tuple[dict[str, Any], Response | None]:
        from app.admin import _json_body

        _, error = self.admin._require_admin_mutation(request)
        if error:
            return {}, error
        payload, parse_error = await _json_body(request, allow_empty=allow_empty)
        if parse_error:
            return {}, parse_error
        unknown = set(payload) - allowed
        if unknown:
            return {}, self._error(f"Unknown fields: {', '.join(sorted(unknown))}.", 400, "payload_invalid")
        return payload, None

    def _run(self, action: Callable[[], dict[str, Any]]) -> Response:
        try:
            return self._ok(action())
        except (StrandError, HighlightError) as exc:
            return self._domain_error(exc)

    def _domain_error(self, exc: StrandError | HighlightError) -> JSONResponse:
        if exc.code in _NOT_FOUND_CODES:
            status = 404
        elif exc.code in _CONFLICT_CODES:
            status = 409
        elif exc.code == "codex_unavailable":
            status = 503
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


def _int_param(request: Request, name: str) -> tuple[int, Response | None]:
    try:
        return int(request.path_params[name]), None
    except (KeyError, TypeError, ValueError):
        return 0, JSONResponse({"ok": False, "error": f"Invalid {name}.", "code": "id_invalid"}, status_code=400)
