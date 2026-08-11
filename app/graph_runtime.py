from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from contextlib import suppress
from typing import Any, Protocol

from app.codex_app_server import (
    CodexIncompatibleVersionError,
    CodexPolicyViolationError,
    CodexProtocolError,
    CodexTimeoutError,
    CodexUnavailableError,
)
from app.graph_extractor import (
    GRAPH_EXTRACTION_OUTPUT_SCHEMA,
    GraphExtractionError,
    validate_extraction_result,
)
from app.graph_config import validate_graph_draft
from app.storage import Store


logger = logging.getLogger(__name__)
MAX_GRAPH_SOURCE_CHARS = 440_000
DEFAULT_GRAPH_LEASE_SECONDS = 360
DEFAULT_GRAPH_HEARTBEAT_INTERVAL_SECONDS = 60.0


class StructuredCodex(Protocol):
    async def extract_structured(
        self,
        message: str,
        *,
        model: str,
        effort: str,
        output_schema: dict[str, Any],
    ) -> dict[str, Any]: ...


class GraphRuntime:
    def __init__(
        self,
        store: Store,
        codex: StructuredCodex,
        *,
        worker_id: str = "bridge-graph",
        lease_seconds: int = DEFAULT_GRAPH_LEASE_SECONDS,
        heartbeat_interval_seconds: float = DEFAULT_GRAPH_HEARTBEAT_INTERVAL_SECONDS,
    ):
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive.")
        if not 0 < heartbeat_interval_seconds < lease_seconds:
            raise ValueError("heartbeat interval must be positive and shorter than the lease.")
        self.store = store
        self.codex = codex
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._active_task: asyncio.Task[None] | None = None
        self._lab_tasks: set[asyncio.Task[None]] = set()
        self._run_lock = asyncio.Lock()

    async def set_enabled(self, enabled: bool) -> dict[str, Any]:
        config = await asyncio.to_thread(self.store.set_graph_enabled, enabled)
        if not enabled and self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._active_task
        if not enabled:
            lab_tasks = tuple(task for task in self._lab_tasks if not task.done())
            for task in lab_tasks:
                task.cancel()
            if lab_tasks:
                await asyncio.gather(*lab_tasks, return_exceptions=True)
        return config

    async def run_once(self, *, now: int | None = None) -> None:
        if self._run_lock.locked():
            return
        async with self._run_lock:
            await asyncio.to_thread(self.store.enqueue_eligible_graph_jobs, now=now)
            lease_owner = f"{self.worker_id}:{uuid.uuid4().hex}"
            claim_task = asyncio.create_task(
                asyncio.to_thread(
                    self.store.claim_graph_job,
                    lease_owner,
                    now=now,
                    lease_seconds=self.lease_seconds,
                )
            )
            try:
                job = await asyncio.shield(claim_task)
            except asyncio.CancelledError:
                job = await claim_task
                if job is not None:
                    await asyncio.shield(
                        asyncio.to_thread(
                            self.store.fail_graph_job,
                            job["job_id"],
                            lease_owner=lease_owner,
                            error_code="worker_interrupted",
                            error_message="Graph worker was interrupted after claiming a job.",
                            retryable=True,
                            now=now,
                        )
                    )
                raise
            if job is None:
                return
            task = asyncio.create_task(
                self._process_job(job, lease_owner=lease_owner, now=now)
            )
            lease_lost = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._heartbeat(job["job_id"], lease_owner, task, lease_lost)
            )
            self._active_task = task
            try:
                await task
            except asyncio.CancelledError:
                if not lease_lost.is_set():
                    raise
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                if self._active_task is task:
                    self._active_task = None

    async def _heartbeat(
        self,
        job_id: int,
        lease_owner: str,
        task: asyncio.Task[None],
        lease_lost: asyncio.Event,
    ) -> None:
        while not task.done():
            await asyncio.sleep(self.heartbeat_interval_seconds)
            if task.done():
                return
            try:
                renewed = await asyncio.to_thread(
                    self.store.renew_graph_job_lease,
                    job_id,
                    lease_owner,
                    lease_seconds=self.lease_seconds,
                )
            except Exception:
                logger.exception(
                    "Graph job lease heartbeat failed", extra={"job_id": job_id}
                )
                renewed = False
            if not renewed:
                lease_lost.set()
                task.cancel()
                return

    async def start_lab_run(
        self,
        session_id: str,
        overrides: dict[str, Any],
        *,
        actor: str,
    ) -> dict[str, Any]:
        run, context = await asyncio.to_thread(self._prepare_lab_run, session_id, overrides, actor, None)
        task = asyncio.create_task(self._execute_lab_run(run, context, now=None))
        self._lab_tasks.add(task)
        task.add_done_callback(self._lab_tasks.discard)
        return run

    async def run_lab_once(
        self,
        session_id: str,
        overrides: dict[str, Any],
        *,
        actor: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        run, context = await asyncio.to_thread(self._prepare_lab_run, session_id, overrides, actor, now)
        await self._execute_lab_run(run, context, now=now)
        completed = await asyncio.to_thread(self.store.get_graph_lab_run, run["lab_run_id"])
        assert completed is not None
        return completed

    def _prepare_lab_run(
        self,
        session_id: str,
        overrides: dict[str, Any],
        actor: str,
        now: int | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        config = self.store.get_graph_config()
        if not config["enabled"]:
            raise ValueError("Graph is off. Enable Graph before running Lab experiments.")
        active = config["active_profile"]
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError("Unknown session_id.")
        group = self.store.get_session_group(session.group_id)
        if group is None:
            raise ValueError("The selected session group is unavailable.")
        if group.is_sensitive and not active["include_sensitive"]:
            raise ValueError("The selected session is excluded by the sensitive-group policy.")
        base = {
            key: active[key]
            for key in (
                "provider", "model", "effort", "prompt", "max_concepts",
                "inactivity_hours", "include_sensitive",
            )
        }
        settings = validate_graph_draft({**base, **overrides})
        latest = self.store.get_latest_exchange(session_id)
        if latest is None:
            raise ValueError("The selected session has no eligible exchanges.")
        run = self.store.create_graph_lab_run(
            session_id,
            latest.exchange_id,
            settings,
            actor,
            now=now,
        )
        context = {
            "session_id": session_id,
            "source_exchange_id": latest.exchange_id,
            "profile": settings,
        }
        return run, context

    async def _execute_lab_run(
        self,
        run: dict[str, Any],
        context: dict[str, Any],
        *,
        now: int | None,
    ) -> None:
        await asyncio.to_thread(self.store.start_graph_lab_run, run["lab_run_id"], now=now)
        try:
            prompt, source_by_exchange = await asyncio.to_thread(self._build_prompt, context)
            schema = copy.deepcopy(GRAPH_EXTRACTION_OUTPUT_SCHEMA)
            schema["properties"]["concepts"]["maxItems"] = context["profile"]["max_concepts"]
            raw = await self.codex.extract_structured(
                prompt,
                model=context["profile"]["model"],
                effort=context["profile"]["effort"],
                output_schema=schema,
            )
            validated = validate_extraction_result(
                raw,
                source_by_exchange,
                max_concepts=context["profile"]["max_concepts"],
            )
            await asyncio.to_thread(
                self.store.complete_graph_lab_run,
                run["lab_run_id"],
                validated,
                now=now,
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.fail_graph_lab_run,
                run["lab_run_id"],
                error_code="interrupted",
                error_message="Lab run was interrupted.",
                now=now,
            )
            raise
        except Exception:
            await asyncio.to_thread(
                self.store.fail_graph_lab_run,
                run["lab_run_id"],
                error_code="invalid_experiment",
                error_message="The Lab result failed validation or execution.",
                now=now,
            )

    async def _process_job(
        self,
        job: dict[str, Any],
        *,
        lease_owner: str,
        now: int | None,
    ) -> None:
        try:
            context = await asyncio.to_thread(self.store.get_graph_job_context, job["job_id"])
            if context is None:
                raise RuntimeError("Graph job context is unavailable.")
            prompt, source_by_exchange = await asyncio.to_thread(
                self._build_prompt,
                context,
            )
            schema = copy.deepcopy(GRAPH_EXTRACTION_OUTPUT_SCHEMA)
            schema["properties"]["concepts"]["maxItems"] = context["profile"]["max_concepts"]
            raw = await self.codex.extract_structured(
                prompt,
                model=context["profile"]["model"],
                effort=context["profile"]["effort"],
                output_schema=schema,
            )
            validated = validate_extraction_result(
                raw,
                source_by_exchange,
                max_concepts=context["profile"]["max_concepts"],
            )
            await asyncio.to_thread(
                self.store.publish_graph_extraction,
                job["job_id"],
                validated,
                lease_owner=lease_owner,
                now=now,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                asyncio.to_thread(
                    self.store.fail_graph_job,
                    job["job_id"],
                    lease_owner=lease_owner,
                    error_code="worker_interrupted",
                    error_message="Graph worker was interrupted before completion.",
                    retryable=True,
                    now=now,
                )
            )
            raise
        except GraphExtractionError as exc:
            await asyncio.to_thread(
                self.store.fail_graph_job,
                job["job_id"],
                lease_owner=lease_owner,
                error_code=exc.code,
                error_message=str(exc),
                retryable=exc.retryable,
                now=now,
            )
        except CodexPolicyViolationError as exc:
            await asyncio.to_thread(
                self.store.fail_graph_job,
                job["job_id"],
                lease_owner=lease_owner,
                error_code="codex_policy_violation",
                error_message=str(exc),
                retryable=False,
                now=now,
            )
        except CodexIncompatibleVersionError as exc:
            await asyncio.to_thread(
                self.store.fail_graph_job,
                job["job_id"],
                lease_owner=lease_owner,
                error_code="codex_version_incompatible",
                error_message=str(exc),
                retryable=False,
                now=now,
            )
        except CodexProtocolError as exc:
            await asyncio.to_thread(
                self.store.fail_graph_job,
                job["job_id"],
                lease_owner=lease_owner,
                error_code="codex_protocol_error",
                error_message=str(exc),
                retryable=True,
                now=now,
            )
        except ValueError as exc:
            await asyncio.to_thread(
                self.store.fail_graph_job,
                job["job_id"],
                lease_owner=lease_owner,
                error_code="invalid_graph_input",
                error_message=str(exc),
                retryable=False,
                now=now,
            )
        except (CodexUnavailableError, CodexTimeoutError):
            await asyncio.to_thread(
                self.store.fail_graph_job,
                job["job_id"],
                lease_owner=lease_owner,
                error_code="provider_unavailable",
                error_message="Codex is temporarily unavailable.",
                retryable=True,
                now=now,
            )
        except Exception:
            await asyncio.to_thread(
                self.store.fail_graph_job,
                job["job_id"],
                lease_owner=lease_owner,
                error_code="worker_failure",
                error_message="Unexpected Graph worker failure.",
                retryable=True,
                now=now,
            )

    def _build_prompt(self, context: dict[str, Any]) -> tuple[str, dict[int, str]]:
        exchanges = [
            exchange
            for exchange in self.store.list_exchanges(context["session_id"])
            if exchange.exchange_id <= context["source_exchange_id"]
        ]
        blocks: list[tuple[int, str]] = []
        total = 0
        for exchange in reversed(exchanges):
            assistant_text = "" if exchange.assistant_masked_at is not None else exchange.assistant_response
            assistant = "" if not assistant_text else f"\n\nMODEL:\n{assistant_text}"
            block = f"Exchange {exchange.exchange_id}\nUSER:\n{exchange.user_message}{assistant}"
            if blocks and total + len(block) > MAX_GRAPH_SOURCE_CHARS:
                break
            if len(block) > MAX_GRAPH_SOURCE_CHARS:
                raise GraphExtractionError(
                    "A single exchange exceeds the Graph source limit.",
                    code="source_exchange_too_large",
                    retryable=False,
                )
            blocks.append((exchange.exchange_id, block))
            total += len(block)
        blocks.reverse()
        if not blocks:
            raise GraphExtractionError(
                "The session has no eligible source exchanges.",
                code="source_transcript_empty",
                retryable=False,
            )
        included_ids = {exchange_id for exchange_id, _ in blocks}
        source_by_exchange = {
            exchange.exchange_id: (
                exchange.user_message
                if exchange.assistant_masked_at is not None
                else f"{exchange.user_message}\n{exchange.assistant_response}"
            )
            for exchange in exchanges
            if exchange.exchange_id in included_ids
        }
        transcript = "\n\n---\n\n".join(block for _, block in blocks)
        prompt = (
            f"{context['profile']['prompt']}\n\n"
            "Return only the structured result. Evidence quotes must be literal substrings "
            "of the named exchange.\n\nSESSION TRANSCRIPT\n\n"
            f"{transcript}"
        )
        return prompt, source_by_exchange
