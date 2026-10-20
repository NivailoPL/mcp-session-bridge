"""Admin API for the Connectors settings tab.

It lists the harnesses the owner has signed in over OAuth, disconnects them, and
controls the optional connection lock and its window (see ``app.connectors``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.admin import _json_body
from app.connectors import (
    ConnectionLock,
    RedirectUriError,
    redirect_kind,
    redirect_label,
)

if TYPE_CHECKING:
    from app.admin import AdminHandlers


class ConnectorAdminHandlers:
    def __init__(self, admin: AdminHandlers, lock: ConnectionLock) -> None:
        self.admin = admin
        self.store = admin.store
        self.lock = lock

    async def api_overview(self, request: Request) -> Response:
        _, error = self.admin._require_admin(request)
        if error:
            return error
        return self._overview()

    async def api_update_lock(self, request: Request) -> Response:
        _, error = self.admin._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return self.admin._json_error("enabled must be true or false.", status_code=400)
        self.lock.set_enabled(enabled)
        return self._overview()

    async def api_open_window(self, request: Request) -> Response:
        _, error = self.admin._require_admin_mutation(request)
        if error:
            return error
        payload, parse_error = await _json_body(request)
        if parse_error:
            return parse_error
        if not self.lock.enabled():
            return self.admin._json_error("Turn on Lock new connections first.", status_code=409)
        minutes = payload.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            return self.admin._json_error("minutes must be a whole number.", status_code=400)
        try:
            self.lock.open_window(minutes)
        except ValueError as exc:
            return self.admin._json_error(str(exc), status_code=400)
        return self._overview()

    async def api_close_window(self, request: Request) -> Response:
        _, error = self.admin._require_admin_mutation(request)
        if error:
            return error
        self.lock.close_window()
        return self._overview()

    async def api_disconnect(self, request: Request) -> Response:
        _, error = self.admin._require_admin_mutation(request)
        if error:
            return error
        client_id = str(request.path_params.get("client_id", ""))
        if not self.store.disconnect_client(client_id):
            return self.admin._json_error("Unknown connector.", status_code=404)
        return self._overview()

    def _overview(self) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                **self.lock.state(),
                "clients": [_client_payload(client) for client in self.store.list_connected_clients()],
            },
            headers=self.admin._no_store_headers(),
        )


def _client_payload(client: dict[str, Any]) -> dict[str, Any]:
    destinations = []
    for uri in client["redirect_uris"]:
        try:
            kind = redirect_kind(uri)
        except RedirectUriError:
            continue
        destination = {"label": redirect_label(uri), "kind": kind}
        if destination not in destinations:
            destinations.append(destination)
    kinds = {destination["kind"] for destination in destinations}
    return {
        "client_id": client["client_id"],
        "name": client["client_name"],
        "destinations": destinations,
        "kind": "web" if "web" in kinds else "app" if "app" in kinds else "local",
        "approved_at": client["approved_at"],
        "last_active_at": client["last_active_at"],
        "signed_in": client["has_live_token"],
    }
