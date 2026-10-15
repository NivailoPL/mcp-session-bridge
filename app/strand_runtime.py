"""Runs Codex strand proposals for one session, only when the owner asks.

There is no scheduler or background loop here: every run starts from an admin
request for one session and reads the whole transcript in one Codex turn.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any, Protocol

from app.codex_app_server import CodexAppServerError, CodexTimeoutError, CodexUnavailableError
from app.storage import Store
from app.strands import (
    STRAND_OUTPUT_SCHEMA,
    StrandError,
    build_strand_prompt,
    validate_strand_result,
)

logger = logging.getLogger(__name__)


class StructuredCodex(Protocol):
    async def extract_structured(
        self,
        message: str,
        *,
        model: str,
        effort: str,
        output_schema: dict[str, Any],
    ) -> dict[str, Any]: ...


class StrandRuntime:
    def __init__(self, store: Store, codex: StructuredCodex | None):
        self.store = store
        self.codex = codex
        self._tasks: set[asyncio.Task[None]] = set()

    def _prepare(
        self, session_id: str, *, actor: str, confirm_sensitive: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        session = self.store.get_session(session_id)
        if session is None:
            raise StrandError("Unknown session.", code="session_unknown")
        group = self.store.get_session_group(session.group_id)
        if group is not None and group.is_sensitive and not confirm_sensitive:
            raise StrandError(
                "This session is in a sensitive group. Confirm to send its transcript to Codex.",
                code="sensitive_unconfirmed",
            )
        exchanges = self.store.list_exchanges(session_id)
        settings = self.store.get_strand_settings()
        prompt = build_strand_prompt(settings["prompt"], exchanges)
        run = self.store.create_strand_run(
            session_id,
            source_exchange_id=exchanges[-1].exchange_id,
            exchange_count=len(exchanges),
            settings=settings,
            actor=actor,
        )
        positions = {exchange.exchange_id: index for index, exchange in enumerate(exchanges)}
        return run, {"prompt": prompt, "settings": settings, "positions": positions}

    async def start_run(self, session_id: str, *, actor: str, confirm_sensitive: bool = False) -> dict[str, Any]:
        """Validate and queue a run, then execute it in the background."""
        if self.codex is None:
            raise StrandError("Codex App Server is not configured.", code="codex_unavailable")
        run, context = await asyncio.to_thread(
            self._prepare, session_id, actor=actor, confirm_sensitive=confirm_sensitive
        )
        task = asyncio.create_task(self._execute(run["run_id"], context))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def run_now(self, session_id: str, *, actor: str, confirm_sensitive: bool = False) -> dict[str, Any]:
        """Like ``start_run`` but waits for the result (tests and scripts)."""
        if self.codex is None:
            raise StrandError("Codex App Server is not configured.", code="codex_unavailable")
        run, context = await asyncio.to_thread(
            self._prepare, session_id, actor=actor, confirm_sensitive=confirm_sensitive
        )
        await self._execute(run["run_id"], context)
        finished = await asyncio.to_thread(self.store.get_strand_run, run["run_id"])
        assert finished is not None
        return finished

    async def _execute(self, run_id: int, context: dict[str, Any]) -> None:
        assert self.codex is not None
        await asyncio.to_thread(self.store.start_strand_run, run_id)
        try:
            raw = await self.codex.extract_structured(
                context["prompt"],
                model=context["settings"]["model"],
                effort=context["settings"]["effort"],
                output_schema=copy.deepcopy(STRAND_OUTPUT_SCHEMA),
            )
            proposals = validate_strand_result(raw, context["positions"])
            await asyncio.to_thread(self.store.complete_strand_run, run_id, proposals)
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.fail_strand_run, run_id, error_code="interrupted", error_message="The run was interrupted."
            )
            raise
        except StrandError as exc:
            await asyncio.to_thread(self.store.fail_strand_run, run_id, error_code=exc.code, error_message=str(exc))
        except (CodexUnavailableError, CodexTimeoutError):
            await asyncio.to_thread(
                self.store.fail_strand_run,
                run_id,
                error_code="codex_unavailable",
                error_message="Codex is unavailable or timed out. Nothing was changed.",
            )
        except (CodexAppServerError, ValueError):
            await asyncio.to_thread(
                self.store.fail_strand_run,
                run_id,
                error_code="codex_failed",
                error_message="Codex returned no usable result. Nothing was changed.",
            )
        except Exception:
            logger.exception("Strand run %s failed", run_id)
            await asyncio.to_thread(
                self.store.fail_strand_run,
                run_id,
                error_code="run_failed",
                error_message="The run failed. Nothing was changed.",
            )

    async def close(self) -> None:
        tasks = tuple(task for task in self._tasks if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
