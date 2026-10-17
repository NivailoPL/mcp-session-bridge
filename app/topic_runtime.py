"""Runs Jev classification over chapters, only when the owner asks.

There is no scheduler here. A run starts from an admin request, sends each
chapter in scope to Jev on the active map, and records every request and raw
answer. The owner can pause it at any time; a run that hits repeated provider
errors pauses itself, and a restart leaves it paused. Resume continues with the
chapters that are not done yet.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from typing import Any, Protocol

from app.jev_client import JevClient, JevError
from app.storage import Store
from app.topics import (
    JEV_DEFAULT_COST_PER_CHAPTER,
    POOL_KEY,
    TopicError,
    jev_criteria,
    jev_state,
    validate_scope,
)

logger = logging.getLogger(__name__)

CLASSIFY_CHUNK = 24
CLASSIFY_CONCURRENCY = 6
MAX_CONSECUTIVE_FAILURES = 3
WEAK_CONFIDENCE = 0.4


class Classifier(Protocol):
    async def classify(self, state: dict[str, Any], criteria: dict[str, str]) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class TopicRuntime:
    def __init__(
        self,
        store: Store,
        key_reader: Callable[[], str],
        jev_factory: Callable[[str, str], Classifier] | None = None,
    ) -> None:
        self.store = store
        self.key_reader = key_reader
        self.jev_factory = jev_factory or (lambda key, model: JevClient(key, model))
        self._task: asyncio.Task[None] | None = None
        self._pause_requested = False

    # ---------- planning ----------

    def select(self, scope_payload: Any) -> dict[str, Any]:
        """Which chapters a run with this scope would send, and which it would skip."""
        version = self.store.active_topic_version()
        if version is None:
            raise TopicError("Import a topic map first.", code="map_missing")
        settings = self.store.get_topic_settings()
        keys = {topic["key"] for topic in version["topics"]}
        scope = validate_scope(scope_payload, keys)
        current = self.store.topic_current_assignments(version["version_id"])
        chosen = []
        for chapter in self.store.topic_chapters():
            item = current.get(chapter["strand_id"])
            if scope["mode"] == "unclassified" and item is not None and item["chapter_title"] == chapter["title"]:
                continue
            if scope["mode"] == "topic" and (item is None or item["topic_key"] != scope["topic_key"]):
                continue
            chosen.append(chapter)
        send = [c for c in chosen if settings["include_sensitive"] or not c["sensitive"]]
        skipped = [c for c in chosen if c not in send]
        return {"version": version, "settings": settings, "scope": scope, "send": send, "skipped": skipped}

    def estimate(self, scope_payload: Any) -> dict[str, Any]:
        plan = self.select(scope_payload)
        per_chapter = self.store.average_topic_cost() or JEV_DEFAULT_COST_PER_CHAPTER
        return {
            "scope": plan["scope"],
            "version_id": plan["version"]["version_id"],
            "send_count": len(plan["send"]),
            "skipped_sensitive": len(plan["skipped"]),
            "cost_per_chapter_usd": round(per_chapter, 6),
            "estimated_cost_usd": round(per_chapter * len(plan["send"]), 4),
            "measured": self.store.average_topic_cost() is not None,
        }

    # ---------- control ----------

    def _require_ready(self) -> str:
        if not self.store.topics_enabled():
            raise TopicError("Token-spending features are switched off in Settings.", code="feature_off")
        key = self.key_reader()
        if not key:
            raise TopicError("Add the OpenRouter API key in Topics settings first.", code="key_missing")
        if self.running():
            raise TopicError("A topics run is already in progress.", code="run_active")
        return key

    async def start_classify(self, scope_payload: Any, *, actor: str) -> dict[str, Any]:
        key = self._require_ready()
        plan = await asyncio.to_thread(self.select, scope_payload)
        if not plan["send"]:
            raise TopicError("No chapters in this scope need classifying.", code="nothing_to_do")
        active = await asyncio.to_thread(self.store.active_topic_run)
        if active is not None and active["status"] == "paused":
            raise TopicError(
                f"Run {active['run_id']} is paused. Resume or close it before starting another.", code="run_paused"
            )
        version = plan["version"]
        criteria = jev_criteria(version["topics"])
        run = await asyncio.to_thread(
            lambda: self.store.create_topic_run(
                kind="classify",
                version_id=version["version_id"],
                scope=plan["scope"],
                settings=plan["settings"],
                chapters=plan["send"],
                skipped=plan["skipped"],
                input_data={"criteria": criteria, "instructions_source": "app.topics.JEV_INSTRUCTIONS"},
                actor=actor,
            )
        )
        await asyncio.to_thread(
            self.store.add_topic_journal,
            "owner",
            "classify.start",
            f"Started classifying {len(plan['send'])} chapters on map v{version['version_id']}"
            + (f" ({len(plan['skipped'])} sensitive skipped)." if plan["skipped"] else "."),
            run_id=run["run_id"],
            version_id=version["version_id"],
            data={"scope": plan["scope"]},
        )
        self._launch(run["run_id"], key)
        return run

    async def resume(self, run_id: int, *, actor: str) -> dict[str, Any]:
        key = self._require_ready()
        run = await asyncio.to_thread(self.store.get_topic_run, run_id)
        if run is None:
            raise TopicError("Unknown topics run.", code="run_unknown")
        if run["status"] != "paused":
            raise TopicError("Only a paused run can be resumed.", code="run_not_paused")
        active = await asyncio.to_thread(self.store.active_topic_version)
        if active is None or active["version_id"] != run["version_id"]:
            raise TopicError("The active map changed since this run. Close it and start a new one.", code="map_changed")
        retried = await asyncio.to_thread(self.store.reset_failed_topic_items, run_id)
        run = await asyncio.to_thread(self.store.set_topic_run_status, run_id, "queued")
        await asyncio.to_thread(
            self.store.add_topic_journal, "owner", "classify.resume",
            f"Resumed run {run_id}" + (f", retrying {retried} failed chapters." if retried else "."),
            run_id=run_id, version_id=run["version_id"],
        )
        self._launch(run_id, key)
        return run

    def close_run(self, run_id: int) -> dict[str, Any]:
        """Close a paused run as it is; its finished chapters keep their topics."""
        run = self.store.get_topic_run(run_id)
        if run is None:
            raise TopicError("Unknown topics run.", code="run_unknown")
        if run["status"] != "paused":
            raise TopicError("Only a paused run can be closed.", code="run_not_paused")
        run = self.store.set_topic_run_status(
            run_id, "completed", error_code=run["error_code"], error_message=run["error_message"],
            metrics=self._metrics(run_id),
        )
        self.store.add_topic_journal(
            "owner", "classify.close",
            f"Closed run {run_id} with {run['done_count']} of {run['total_count']} chapters done.",
            run_id=run_id, version_id=run["version_id"],
        )
        return run

    def request_pause(self, run_id: int) -> dict[str, Any]:
        run = self.store.get_topic_run(run_id)
        if run is None:
            raise TopicError("Unknown topics run.", code="run_unknown")
        if run["status"] not in ("queued", "running"):
            raise TopicError("This run is not running.", code="run_not_running")
        self._pause_requested = True
        return run

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def recover(self) -> None:
        for run_id in self.store.recover_topic_runs():
            self.store.add_topic_journal(
                "system", "classify.interrupted", f"Run {run_id} was cut off by a restart and is paused.", run_id=run_id
            )

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _launch(self, run_id: int, key: str) -> None:
        self._pause_requested = False
        self._task = asyncio.create_task(self._execute(run_id, key))

    # ---------- execution ----------

    async def _execute(self, run_id: int, key: str) -> None:
        run = await asyncio.to_thread(self.store.set_topic_run_status, run_id, "running")
        version = await asyncio.to_thread(self.store.get_topic_version, run["version_id"])
        assert version is not None
        criteria = jev_criteria(version["topics"])
        jev_model = run["settings"]["jev_model"]
        jev = self.jev_factory(key, jev_model)
        semaphore = asyncio.Semaphore(CLASSIFY_CONCURRENCY)
        stop: tuple[str, str] | None = None
        consecutive = 0
        try:
            chapters = {c["strand_id"]: c for c in await asyncio.to_thread(self.store.topic_chapters)}
            pending = [sid for sid in await asyncio.to_thread(self.store.pending_topic_items, run_id) if sid in chapters]
            for start in range(0, len(pending), CLASSIFY_CHUNK):
                if self._pause_requested:
                    stop = ("paused_by_owner", "Paused by the owner.")
                    break
                chunk = [chapters[sid] for sid in pending[start : start + CLASSIFY_CHUNK]]
                results = await asyncio.gather(*(self._one(jev, semaphore, c, criteria) for c in chunk))
                for chapter, (answer, error) in zip(chunk, results):
                    await asyncio.to_thread(self._record, run_id, chapter, jev_model, answer, error)
                    if error is None:
                        consecutive = 0
                        continue
                    consecutive += 1
                    if error.code == "jev_auth":
                        stop = ("jev_auth", "OpenRouter refused the API key. Fix it in settings, then resume.")
                    elif consecutive >= MAX_CONSECUTIVE_FAILURES and stop is None:
                        stop = ("provider_errors", f"Paused after {consecutive} provider errors in a row: {error}")
                if stop is not None:
                    break
            run = await asyncio.to_thread(self.store.get_topic_run, run_id)
            assert run is not None
            if stop is None and run["failed_count"]:
                stop = ("some_failed", f"{run['failed_count']} chapters failed. Resume to retry them.")
            metrics = await asyncio.to_thread(self._metrics, run_id)
            if stop is None:
                await asyncio.to_thread(self.store.set_topic_run_status, run_id, "completed", metrics=metrics)
                await asyncio.to_thread(
                    self.store.add_topic_journal, "jev", "classify.finish", _finish_message(run, metrics),
                    run_id=run_id, version_id=run["version_id"], data=metrics,
                )
            else:
                await asyncio.to_thread(
                    self.store.set_topic_run_status, run_id, "paused",
                    error_code=stop[0], error_message=stop[1], metrics=metrics,
                )
                actor = "owner" if stop[0] == "paused_by_owner" else "system"
                await asyncio.to_thread(
                    self.store.add_topic_journal, actor, "classify.pause",
                    f"Run {run_id} paused at {run['done_count']}/{run['total_count']}: {stop[1]}",
                    run_id=run_id, version_id=run["version_id"],
                )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.set_topic_run_status, run_id, "paused",
                error_code="interrupted", error_message="Bridge stopped during the run. Resume to finish it.",
            )
            raise
        except Exception:
            logger.exception("Topics run %s failed", run_id)
            await asyncio.to_thread(
                self.store.set_topic_run_status, run_id, "paused",
                error_code="run_failed", error_message="The run stopped on an unexpected error. Resume to retry.",
            )
        finally:
            await jev.close()

    async def _one(
        self, jev: Classifier, semaphore: asyncio.Semaphore, chapter: dict[str, Any], criteria: dict[str, str]
    ) -> tuple[dict[str, Any] | None, JevError | None]:
        async with semaphore:
            try:
                return await jev.classify(jev_state(chapter), criteria), None
            except JevError as exc:
                return None, exc

    def _record(
        self,
        run_id: int,
        chapter: dict[str, Any],
        model: str,
        answer: dict[str, Any] | None,
        error: JevError | None,
    ) -> None:
        # The criteria are stored once on the run; each item keeps the state it sent.
        request = {"state": jev_state(chapter), "model": model}
        if error is not None:
            self.store.record_topic_item(run_id, chapter["strand_id"], request=request, response=None,
                                         error=f"{error.code}: {error}")
            return
        assert answer is not None
        self.store.record_topic_item(
            run_id, chapter["strand_id"], request=request, response=answer["raw"],
            topic_key=answer["choice"], confidence=answer["confidence"],
            probabilities=answer["probabilities"], cost_usd=answer["cost_usd"],
        )

    def _metrics(self, run_id: int) -> dict[str, Any]:
        items = [item for item in self.store.topic_run_items(run_id) if item["status"] == "done"]
        confidences = [item["confidence"] for item in items if item["confidence"] is not None]
        topics = Counter(item["topic_key"] for item in items)
        return {
            "done": len(items),
            "per_topic": dict(topics.most_common()),
            "pool": topics.get(POOL_KEY, 0),
            "average_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
            "weak_count": sum(1 for value in confidences if value < WEAK_CONFIDENCE),
            "cost_usd": round(sum(item["cost_usd"] or 0 for item in items), 6),
        }


def _finish_message(run: dict[str, Any], metrics: dict[str, Any]) -> str:
    average = metrics["average_confidence"]
    return (
        f"Classified {metrics['done']} chapters on map v{run['version_id']}: "
        f"{metrics['pool']} unsorted, average confidence {average if average is not None else '-'}, "
        f"{metrics['weak_count']} weak, cost ${metrics['cost_usd']:.4f}."
    )
