"""Admin API for the Contexts workspace.

It reuses the admin login, CSRF check and error helpers of ``AdminHandlers``; the
context rules themselves (locks, positions, snapshots, redaction) live in the store
and in ``app.contexts``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.contexts import (
    assemble_context,
    block_status,
    build_redactor,
    snapshot_file,
    snapshot_session,
)
from app.storage import (
    ContextBlockConflictError,
    ContextBlockSnapshot,
    ContextLockedError,
    ContextNotFoundError,
)

if TYPE_CHECKING:
    from app.admin import AdminHandlers


RESTORE_FIELDS = {
    "source_type",
    "source_key",
    "source_session_id",
    "source_file_id",
    "title",
    "content",
    "source_hash",
    "found_ids",
    "token_count",
}


class ContextAdminHandlers:
    def __init__(self, admin: AdminHandlers) -> None:
        self.admin = admin
        self.store = admin.store

    # ---------- library ----------

    async def api_library(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        kind = request.query_params.get("kind", "sessions")
        if kind == "sessions":
            items = self.store.list_context_library_sessions()
        elif kind == "files":
            items = self.store.list_context_library_files()
        else:
            return self._error("kind must be sessions or files.", 400)
        return self._ok({"kind": kind, "items": items, "groups": self.store.list_session_groups()})

    # ---------- contexts ----------

    async def api_list(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        return self._ok({"contexts": [self._with_status(context) for context in self.store.list_contexts()]})

    async def api_create(self, request: Request) -> Response:
        payload, error = await self._mutation(request, allowed={"name", "color"}, allow_empty=True)
        if error:
            return error
        return self._run(lambda: {"context": self._with_status(self.store.create_context(**payload))})

    async def api_update(self, request: Request) -> Response:
        payload, error = await self._mutation(request, allowed={"name", "color", "is_locked"})
        if error:
            return error
        if "is_locked" in payload and not isinstance(payload["is_locked"], bool):
            return self._error("is_locked must be true or false.", 400)
        context_id = request.path_params["context_id"]
        return self._run(lambda: {"context": self._with_status(self.store.update_context(context_id, **payload))})

    async def api_reorder(self, request: Request) -> Response:
        payload, error = await self._mutation(request, allowed={"context_ids"})
        if error:
            return error
        ids = payload.get("context_ids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            return self._error("context_ids must be a list of context IDs.", 400)
        return self._run(lambda: {"contexts": [self._with_status(c) for c in self.store.reorder_contexts(ids)]})

    async def api_delete(self, request: Request) -> Response:
        _, error = await self._mutation(request, allowed=set(), allow_empty=True)
        if error:
            return error
        context_id = request.path_params["context_id"]
        return self._run(lambda: {"context": self.store.delete_context(context_id)})

    async def api_preview(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        context_id = request.path_params["context_id"]

        def build() -> dict[str, Any]:
            assembled = assemble_context(self.store, context_id, public_base_url=self.admin.settings.public_base_url)
            return {
                "context_id": context_id,
                "markdown": assembled.markdown,
                "token_count": assembled.token_count,
                "redactions": [
                    {"kind": r.kind, "placeholder": r.placeholder, "start": r.start, "end": r.end}
                    for r in assembled.redactions
                ],
            }

        return self._run(build)

    # ---------- blocks ----------

    async def api_add_block(self, request: Request) -> Response:
        payload, error = await self._mutation(
            request, allowed={"source_type", "session_id", "file_id", "position", "restore"}
        )
        if error:
            return error
        context_id = request.path_params["context_id"]
        position = payload.get("position")
        if position is not None and (not isinstance(position, int) or isinstance(position, bool)):
            return self._error("position must be a whole number.", 400)
        if "restore" in payload:
            snapshot = _restore_snapshot(payload["restore"])
            if snapshot is None:
                return self._error("restore must be a block returned by a delete.", 400)
        else:
            snapshot = None

        def add() -> dict[str, Any]:
            nonlocal snapshot
            if snapshot is None:
                snapshot = self._snapshot(payload)
            block = self.store.add_context_block(context_id, snapshot, position=position)
            return {"block": self._block_with_status(block), "context": self._context(context_id)}

        return self._run(add)

    async def api_move_block(self, request: Request) -> Response:
        payload, error = await self._mutation(request, allowed={"context_id", "position", "copy"})
        if error:
            return error
        block_id, path_error = self._block_id(request)
        if path_error:
            return path_error
        target = payload.get("context_id")
        position = payload.get("position")
        copy = payload.get("copy", False)
        if not isinstance(target, str) or not target:
            return self._error("context_id is required.", 400)
        if position is not None and (not isinstance(position, int) or isinstance(position, bool)):
            return self._error("position must be a whole number.", 400)
        if not isinstance(copy, bool):
            return self._error("copy must be true or false.", 400)

        def move() -> dict[str, Any]:
            before = self.store.get_context_block_context(block_id)
            block = self.store.move_context_block(block_id, context_id=target, position=position, copy=copy)
            touched = {target, before} if before else {target}
            return {"block": self._block_with_status(block), "contexts": [self._context(c) for c in sorted(touched)]}

        return self._run(move)

    async def api_refresh_block(self, request: Request) -> Response:
        _, error = await self._mutation(request, allowed=set(), allow_empty=True)
        if error:
            return error
        block_id, path_error = self._block_id(request)
        if path_error:
            return path_error

        def refresh() -> dict[str, Any]:
            current = self.store.get_context_block(block_id)
            source = {"source_type": current["source_type"]}
            if current["source_type"] == "session":
                source["session_id"] = current["source_session_id"]
            else:
                source["file_id"] = current["source_file_id"]
            block = self.store.refresh_context_block(block_id, self._snapshot(source))
            return {"block": self._block_with_status(block), "context": self._context(block["context_id"])}

        return self._run(refresh)

    async def api_delete_block(self, request: Request) -> Response:
        _, error = await self._mutation(request, allowed=set(), allow_empty=True)
        if error:
            return error
        block_id, path_error = self._block_id(request)
        if path_error:
            return path_error

        def delete() -> dict[str, Any]:
            removed = self.store.delete_context_block(block_id)
            return {"block": removed, "context": self._context(removed["context_id"])}

        return self._run(delete)

    # ---------- helpers ----------

    def _snapshot(self, payload: dict[str, Any]) -> ContextBlockSnapshot:
        redactor = build_redactor(self.store, public_base_url=self.admin.settings.public_base_url)
        source_type = payload.get("source_type")
        if source_type == "session":
            session_id = payload.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("session_id is required for a session block")
            return snapshot_session(
                self.store, session_id, timezone_name=self.admin._display_timezone_name(), redactor=redactor
            )
        if source_type == "file":
            file_id = payload.get("file_id")
            if not isinstance(file_id, int) or isinstance(file_id, bool):
                raise ValueError("file_id is required for a file block")
            return snapshot_file(self.store, file_id, redactor=redactor)
        raise ValueError("source_type must be session or file")

    def _context(self, context_id: str) -> dict[str, Any] | None:
        context = self.store.get_context(context_id)
        return self._with_status(context) if context else None

    def _with_status(self, context: dict[str, Any]) -> dict[str, Any]:
        context["blocks"] = [self._block_with_status(block) for block in context["blocks"]]
        return context

    def _block_with_status(self, block: dict[str, Any]) -> dict[str, Any]:
        block["status"] = block_status(self.store, block)
        return block

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
            return {}, self._error(f"Unknown fields: {', '.join(sorted(unknown))}.", 400)
        return payload, None

    def _run(self, action) -> Response:
        try:
            return self._ok(action())
        except ContextLockedError as exc:
            return self._error(str(exc), 409)
        except ContextBlockConflictError as exc:
            return self._error(str(exc), 409)
        except ContextNotFoundError as exc:
            return self._error(str(exc), 404)
        except ValueError as exc:
            return self.admin._value_error(exc)

    @staticmethod
    def _block_id(request: Request) -> tuple[int, Response | None]:
        try:
            return int(request.path_params["block_id"]), None
        except (KeyError, TypeError, ValueError):
            return 0, ContextAdminHandlers._error("Invalid block_id.", 400)

    def _ok(self, body: dict[str, Any]) -> JSONResponse:
        return JSONResponse({"ok": True, **body}, headers=self.admin._no_store_headers())

    @staticmethod
    def _error(message: str, status_code: int) -> JSONResponse:
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def _restore_snapshot(value: Any) -> ContextBlockSnapshot | None:
    """Rebuild the snapshot of a block the admin just deleted, for Undo."""
    if not isinstance(value, dict) or not RESTORE_FIELDS <= set(value):
        return None
    try:
        snapshot = ContextBlockSnapshot(
            source_type=str(value["source_type"]),
            source_key=str(value["source_key"]),
            source_session_id=value["source_session_id"],
            source_file_id=value["source_file_id"],
            title=str(value["title"]),
            content=str(value["content"]),
            source_hash=str(value["source_hash"]),
            found_ids={str(k): [str(i) for i in v] for k, v in dict(value["found_ids"]).items()},
            token_count=int(value["token_count"]),
        )
    except (TypeError, ValueError):
        return None
    expected_key = (
        f"session:{snapshot.source_session_id}" if snapshot.source_type == "session" else f"file:{snapshot.source_file_id}"
    )
    if snapshot.source_key != expected_key:
        return None
    return snapshot
