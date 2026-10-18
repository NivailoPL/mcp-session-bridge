"""Runs every Topics step, only when the owner asks.

There is no scheduler here. One job runs at a time:

- Sort new / sort again: the sorting model (Jev) places chapters on the active map.
- Find topics: the proposing model (Codex) reads Unsorted; on an empty map that is
  every chapter, so it proposes the whole first map.
- Refine topics: the proposing model reviews sorted topics and proposes splits,
  carved-out topics or clearer descriptions.
- Test: before a proposal or a manual edit is shown, the sorting model sorts every
  chapter on the candidate map and the result is compared with the intent.

Every run keeps its input and raw output; accepting a tested proposal makes its
candidate the active map and adopts the test's sorting, so nothing is sorted twice.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.codex_app_server import CodexAppServerError, CodexTimeoutError, CodexUnavailableError
from app.jev_client import JevClient, JevError
from app.storage import Store
from app.topic_changes import build_candidate, evaluate_test
from app.topic_prompts import (
    DETAIL_LEVELS,
    FIND_SCHEMA,
    FIRST_MAP_SCHEMA,
    NOTE_MAX_CHARS,
    REFINE_SCHEMA,
    build_find_prompt,
    build_first_map_prompt,
    build_refine_prompt,
    validate_find,
    validate_first_map,
    validate_refine,
)
from app.topics import (
    JEV_DEFAULT_COST_PER_CHAPTER,
    POOL_KEY,
    TopicError,
    jev_criteria,
    jev_state,
    validate_scope,
    validate_topic_map,
)

logger = logging.getLogger(__name__)

CLASSIFY_CHUNK = 24
CLASSIFY_CONCURRENCY = 6
MAX_CONSECUTIVE_FAILURES = 3
WEAK_CONFIDENCE = 0.4


class Classifier(Protocol):
    async def classify(self, state: dict[str, Any], criteria: dict[str, str]) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class StructuredCodex(Protocol):
    async def extract_structured(
        self, message: str, *, model: str, effort: str, output_schema: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


class TopicRuntime:
    def __init__(
        self,
        store: Store,
        key_reader: Callable[[], str],
        jev_factory: Callable[[str, str], Classifier] | None = None,
        codex: StructuredCodex | None = None,
    ) -> None:
        self.store = store
        self.key_reader = key_reader
        self.jev_factory = jev_factory or (lambda key, model: JevClient(key, model))
        self.codex = codex
        self._task: asyncio.Task[None] | None = None
        self._pause_requested = False
        self.job: dict[str, Any] | None = None

    # ---------- planning ----------

    def eligible_chapters(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        settings = self.store.get_topic_settings()
        chapters = self.store.topic_chapters()
        send = [c for c in chapters if settings["include_sensitive"] or not c["sensitive"]]
        skipped = [c for c in chapters if c not in send]
        return send, skipped

    def select(self, scope_payload: Any) -> dict[str, Any]:
        """Which chapters a run with this scope would send, and which it would skip."""
        version = self.store.active_topic_version()
        if version is None:
            raise TopicError("There are no topics yet. Find topics first.", code="map_missing")
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

    def cost_per_chapter(self) -> float:
        return self.store.average_topic_cost() or JEV_DEFAULT_COST_PER_CHAPTER

    def estimate(self, scope_payload: Any) -> dict[str, Any]:
        plan = self.select(scope_payload)
        per_chapter = self.cost_per_chapter()
        sessions = {c["session_id"] for c in plan["send"]}
        return {
            "scope": plan["scope"],
            "version_id": plan["version"]["version_id"],
            "send_count": len(plan["send"]),
            "session_count": len(sessions),
            "skipped_sensitive": len(plan["skipped"]),
            "cost_per_chapter_usd": round(per_chapter, 6),
            "estimated_cost_usd": round(per_chapter * len(plan["send"]), 4),
            "test_cost_usd": round(per_chapter * len(self.eligible_chapters()[0]), 4),
            "measured": self.store.average_topic_cost() is not None,
            "model": plan["settings"]["jev_model"],
        }

    # ---------- control ----------

    def _require_key(self) -> str:
        if not self.store.topics_enabled():
            raise TopicError("Token-spending features are switched off in Settings.", code="feature_off")
        key = self.key_reader()
        if not key:
            raise TopicError("Add the OpenRouter key in Settings › Topics first.", code="key_missing")
        if self.running():
            raise TopicError("Another Topics step is running. Wait for it or pause it.", code="run_active")
        return key

    def _require_codex(self) -> None:
        if self.codex is None:
            raise TopicError("The Codex App Server is not configured.", code="codex_unavailable")

    async def _no_paused_run(self) -> None:
        active = await asyncio.to_thread(self.store.active_topic_run)
        if active is not None and active["status"] == "paused":
            raise TopicError(
                f"Run {active['run_id']} is paused. Resume or close it before starting another.", code="run_paused"
            )

    async def start_classify(self, scope_payload: Any, *, actor: str) -> dict[str, Any]:
        key = self._require_key()
        plan = await asyncio.to_thread(self.select, scope_payload)
        if not plan["send"]:
            raise TopicError("No chapters in this scope need sorting.", code="nothing_to_do")
        await self._no_paused_run()
        version = plan["version"]
        run = await asyncio.to_thread(
            lambda: self.store.create_topic_run(
                kind="classify",
                version_id=version["version_id"],
                scope=plan["scope"],
                settings=plan["settings"],
                chapters=plan["send"],
                skipped=plan["skipped"],
                input_data={"criteria": jev_criteria(version["topics"])},
                actor=actor,
            )
        )
        await asyncio.to_thread(
            self.store.add_topic_journal,
            "owner",
            "classify.start",
            f"Started sorting {len(plan['send'])} chapters on map v{version['version_id']}"
            + (f" ({len(plan['skipped'])} sensitive skipped)." if plan["skipped"] else "."),
            run_id=run["run_id"],
            version_id=version["version_id"],
            data={"scope": plan["scope"]},
        )
        self._launch("classify", lambda: self._classify_job(run["run_id"], key))
        return run

    async def resume(self, run_id: int, *, actor: str) -> dict[str, Any]:
        key = self._require_key()
        run = await asyncio.to_thread(self.store.get_topic_run, run_id)
        if run is None:
            raise TopicError("Unknown topics run.", code="run_unknown")
        if run["status"] != "paused" or run["kind"] != "classify":
            raise TopicError("Only a paused sorting run can be resumed.", code="run_not_paused")
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
        self._launch("classify", lambda: self._classify_job(run_id, key))
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
                "system", "run.interrupted", f"Run {run_id} was cut off by a restart and is paused.", run_id=run_id
            )
        for proposal in self.store.list_topic_proposals(("testing",)):
            self.store.update_topic_proposal(proposal["proposal_id"], status="failed")

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _launch(self, kind: str, factory: Callable[[], Awaitable[None]]) -> None:
        self._pause_requested = False
        self.job = {"kind": kind, "started_at": int(time.time()), "step": "starting"}

        async def runner() -> None:
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Topics job %s failed", kind)
            finally:
                self.job = None

        self._task = asyncio.create_task(runner())

    # ---------- sorting ----------

    async def _classify_job(self, run_id: int, key: str) -> None:
        stop = await self._sort(run_id, key)
        run = await asyncio.to_thread(self.store.get_topic_run, run_id)
        assert run is not None
        if stop is None:
            await asyncio.to_thread(
                self.store.add_topic_journal, "jev", "classify.finish", _finish_message(run, run["metrics"] or {}),
                run_id=run_id, version_id=run["version_id"], data=run["metrics"],
            )
        else:
            actor = "owner" if stop[0] == "paused_by_owner" else "system"
            await asyncio.to_thread(
                self.store.add_topic_journal, actor, "classify.pause",
                f"Run {run_id} paused at {run['done_count']}/{run['total_count']}: {stop[1]}",
                run_id=run_id, version_id=run["version_id"],
            )

    async def _sort(self, run_id: int, key: str) -> tuple[str, str] | None:
        """Sort every pending chapter of a run; return why it stopped early, or None when complete."""
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
                        stop = ("jev_auth", "OpenRouter refused the API key. Fix it in Settings, then resume.")
                    elif consecutive >= MAX_CONSECUTIVE_FAILURES and stop is None:
                        stop = ("provider_errors", f"Paused after {consecutive} provider errors in a row: {error}")
                if self.job is not None:
                    self.job["progress"] = {"done": min(len(pending), start + CLASSIFY_CHUNK), "total": len(pending)}
                if stop is not None:
                    break
            run = await asyncio.to_thread(self.store.get_topic_run, run_id)
            assert run is not None
            if stop is None and run["failed_count"]:
                stop = ("some_failed", f"{run['failed_count']} chapters failed. Resume to retry them.")
            metrics = await asyncio.to_thread(self._metrics, run_id)
            if stop is None:
                await asyncio.to_thread(self.store.set_topic_run_status, run_id, "completed", metrics=metrics)
            else:
                await asyncio.to_thread(
                    self.store.set_topic_run_status, run_id, "paused",
                    error_code=stop[0], error_message=stop[1], metrics=metrics,
                )
            return stop
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
            return ("run_failed", "The run stopped on an unexpected error.")
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
        self, run_id: int, chapter: dict[str, Any], model: str, answer: dict[str, Any] | None, error: JevError | None
    ) -> None:
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

    # ---------- proposals: find, refine, edit ----------

    def _guidance(self, payload: dict[str, Any]) -> tuple[str, str]:
        detail = payload.get("detail") or "balanced"
        if detail not in DETAIL_LEVELS:
            raise TopicError("Detail must be broad, balanced or fine.", code="payload_invalid")
        note = payload.get("note") or ""
        if not isinstance(note, str) or len(note) > NOTE_MAX_CHARS:
            raise TopicError(f"A note is text of at most {NOTE_MAX_CHARS} characters.", code="payload_invalid")
        return detail, note.strip()

    def pool_chapters(self) -> list[dict[str, Any]]:
        """Unsorted: every eligible chapter on an empty map, else those sorted into the pool."""
        send, _ = self.eligible_chapters()
        version = self.store.active_topic_version()
        if version is None:
            return send
        current = self.store.topic_current_assignments(version["version_id"])
        return [c for c in send if (current.get(c["strand_id"]) or {}).get("topic_key") == POOL_KEY]

    async def start_find(self, payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
        key = self._require_key()
        self._require_codex()
        await self._no_paused_run()
        detail, note = self._guidance(payload)
        version = await asyncio.to_thread(self.store.active_topic_version)
        pool = await asyncio.to_thread(self.pool_chapters)
        if not pool:
            raise TopicError("Unsorted is empty: there is nothing to find topics in.", code="nothing_to_do")
        rejected = await asyncio.to_thread(self.store.rejected_topic_proposals)
        settings = await asyncio.to_thread(self.store.get_topic_settings)
        if version is None:
            prompt = build_first_map_prompt(pool, detail=detail, note=note, rejected=rejected)
            schema = FIRST_MAP_SCHEMA
        else:
            prompt = build_find_prompt(version["topics"], pool, detail=detail, note=note, rejected=rejected)
            schema = FIND_SCHEMA
        run = await asyncio.to_thread(
            lambda: self.store.create_topic_run(
                kind="find", version_id=version["version_id"] if version else None, scope={"detail": detail},
                settings=settings, chapters=[], skipped=[], actor=actor,
                input_data={"prompt": prompt, "note": note, "detail": detail, "pool": len(pool),
                            "first_map": version is None},
            )
        )
        await asyncio.to_thread(
            self.store.add_topic_journal, "owner", "find.start",
            f"Asked for topics in Unsorted ({len(pool)} chapters, {detail})" + (" to build the first map." if version is None else "."),
            run_id=run["run_id"], version_id=version["version_id"] if version else None,
        )
        self._launch("find", lambda: self._find_job(run["run_id"], key, prompt, schema, pool, version, settings))
        return run

    async def start_refine(self, payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
        key = self._require_key()
        self._require_codex()
        await self._no_paused_run()
        detail, note = self._guidance(payload)
        version = await asyncio.to_thread(self.store.active_topic_version)
        if version is None:
            raise TopicError("There are no topics to refine yet. Find topics first.", code="map_missing")
        by_topic = await asyncio.to_thread(self._chapters_by_topic, version)
        if not by_topic:
            raise TopicError("No chapters are sorted on this map yet.", code="nothing_to_do")
        rejected = await asyncio.to_thread(self.store.rejected_topic_proposals)
        settings = await asyncio.to_thread(self.store.get_topic_settings)
        prompt = build_refine_prompt(version["topics"], by_topic, detail=detail, note=note, rejected=rejected)
        run = await asyncio.to_thread(
            lambda: self.store.create_topic_run(
                kind="refine", version_id=version["version_id"], scope={"detail": detail}, settings=settings,
                chapters=[], skipped=[], actor=actor, input_data={"prompt": prompt, "note": note, "detail": detail},
            )
        )
        await asyncio.to_thread(
            self.store.add_topic_journal, "owner", "refine.start",
            f"Asked to refine map v{version['version_id']} ({detail}).",
            run_id=run["run_id"], version_id=version["version_id"],
        )
        self._launch("refine", lambda: self._refine_job(run["run_id"], key, prompt, version, by_topic, settings))
        return run

    def _chapters_by_topic(self, version: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        current = self.store.topic_current_assignments(version["version_id"])
        send, _ = self.eligible_chapters()
        out: dict[str, list[dict[str, Any]]] = {}
        for chapter in send:
            item = current.get(chapter["strand_id"])
            if item is None or item["topic_key"] == POOL_KEY:
                continue
            out.setdefault(item["topic_key"], []).append({**chapter, "confidence": item["confidence"] or 0})
        return out

    async def _ask_codex(self, run_id: int, prompt: str, schema: dict[str, Any], settings: dict[str, Any]) -> Any:
        assert self.codex is not None
        await asyncio.to_thread(self.store.set_topic_run_status, run_id, "running")
        if self.job is not None:
            self.job.update({
                "step": "proposing", "step_started_at": int(time.time()), "model": settings["codex_model"],
                "effort": settings["codex_effort"], "timeout_minutes": settings["codex_timeout_minutes"],
            })
        try:
            raw = await self.codex.extract_structured(
                prompt, model=settings["codex_model"], effort=settings["codex_effort"], output_schema=copy.deepcopy(schema),
                timeout_seconds=settings["codex_timeout_minutes"] * 60,
            )
        except CodexTimeoutError:
            await self._fail_run(
                run_id, "codex_timeout",
                f"Codex did not answer within {settings['codex_timeout_minutes']} minutes. Raise the time limit or lower "
                "the reasoning effort, then try again. Nothing changed.",
            )
            return None
        except CodexUnavailableError:
            await self._fail_run(run_id, "codex_unavailable", "Codex is unavailable. Check Settings › Codex. Nothing changed.")
            return None
        except (CodexAppServerError, ValueError) as exc:
            await self._fail_run(run_id, "codex_failed", f"Codex returned no usable result: {exc}")
            return None
        await asyncio.to_thread(self.store.set_topic_run_output, run_id, raw)
        return raw

    async def _fail_run(self, run_id: int, code: str, message: str) -> None:
        run = await asyncio.to_thread(self.store.set_topic_run_status, run_id, "failed", error_code=code,
                                      error_message=message)
        await asyncio.to_thread(self.store.add_topic_journal, "system", f"{run['kind']}.fail", message, run_id=run_id)

    async def _find_job(
        self, run_id: int, key: str, prompt: str, schema: dict[str, Any], pool: list[dict[str, Any]],
        version: dict[str, Any] | None, settings: dict[str, Any],
    ) -> None:
        raw = await self._ask_codex(run_id, prompt, schema, settings)
        if raw is None:
            return
        try:
            if version is None:
                result = validate_first_map(raw, pool)
                specs = [{
                    "op": "first_map",
                    "summary": f"First map: {len(result['topics'])} topics",
                    "payload": {"topics": [dict(t, keep=True) for t in result["topics"]], "reason": result["reason"],
                                "dropped": result["dropped"]},
                }]
                metrics = {"topics": len(result["topics"]), "dropped": len(result["dropped"])}
            else:
                result = validate_find(raw, pool, {t["key"] for t in version["topics"]})
                specs = [{
                    "op": "new_topic",
                    "summary": f"New topic: {item['name']}",
                    "payload": {"target": None, "family": item["family"], "reason": item["reason"],
                                "parts": [{k: item[k] for k in ("key", "name", "description", "chapter_ids", "family")}]},
                } for item in result["new_topics"]]
                metrics = {"new_topics": len(result["new_topics"]), "waiting": result["waiting"]}
        except TopicError as exc:
            await self._fail_run(run_id, exc.code, str(exc))
            return
        await asyncio.to_thread(self.store.set_topic_run_status, run_id, "completed", metrics=metrics)
        await asyncio.to_thread(
            self.store.add_topic_journal, "codex", "find.finish",
            f"Proposed {len(specs)} change{'s' if len(specs) != 1 else ''} from Unsorted.", run_id=run_id, data=metrics,
        )
        await self._create_and_test(run_id, specs, key, source="find")

    async def _refine_job(
        self, run_id: int, key: str, prompt: str, version: dict[str, Any],
        by_topic: dict[str, list[dict[str, Any]]], settings: dict[str, Any],
    ) -> None:
        raw = await self._ask_codex(run_id, prompt, REFINE_SCHEMA, settings)
        if raw is None:
            return
        try:
            operations = validate_refine(raw, version["topics"], by_topic)
        except TopicError as exc:
            await self._fail_run(run_id, exc.code, str(exc))
            return
        names = {t["key"]: t["name"] for t in version["topics"]}
        specs = []
        for op in operations:
            target = names.get(op["target"], op["target"])
            summary = {
                "split": f"Split {target} into {', '.join(p['name'] for p in op['parts'])}",
                "new_topic": f"New topic {op['parts'][0]['name']} out of {target}" if op["parts"] else "",
                "redescribe": f"Clearer description for {target}",
            }[op["op"]]
            specs.append({"op": op["op"], "summary": summary, "payload": op})
        await asyncio.to_thread(self.store.set_topic_run_status, run_id, "completed",
                                metrics={"operations": len(specs)})
        await asyncio.to_thread(
            self.store.add_topic_journal, "codex", "refine.finish",
            f"Proposed {len(specs)} refinement{'s' if len(specs) != 1 else ''}." if specs else "The map needs no change.",
            run_id=run_id,
        )
        await self._create_and_test(run_id, specs, key, source="refine")

    async def _create_and_test(self, run_id: int | None, specs: list[dict[str, Any]], key: str, *, source: str) -> None:
        version = await asyncio.to_thread(self.store.active_topic_version)
        for spec in specs:
            proposal = await asyncio.to_thread(
                lambda spec=spec: self.store.create_topic_proposal(
                    run_id=run_id, base_version_id=version["version_id"] if version else None,
                    op=spec["op"], source=source, summary=spec["summary"], payload=spec["payload"],
                )
            )
            await self._test_proposal(proposal["proposal_id"], key)

    async def start_test_edit(self, topics: Any, *, note: Any, actor: str) -> dict[str, Any]:
        """Test a map edited by hand before saving it."""
        key = self._require_key()
        await self._no_paused_run()
        normalized = [t for t in validate_topic_map(topics) if t["key"] != POOL_KEY]
        version = await asyncio.to_thread(self.store.active_topic_version)
        proposal = await asyncio.to_thread(
            lambda: self.store.create_topic_proposal(
                run_id=None, base_version_id=version["version_id"] if version else None, op="edit", source="owner",
                summary=(note or "Edited by hand")[:300] if isinstance(note, str) else "Edited by hand",
                payload={"topics": normalized, "note": note if isinstance(note, str) else ""},
            )
        )
        await asyncio.to_thread(
            self.store.add_topic_journal, "owner", "edit.test", f"Testing a hand edit of the map: {proposal['summary']}.",
        )
        self._launch("test", lambda: self._test_proposal(proposal["proposal_id"], key))
        return proposal

    async def revise(self, proposal_id: int, topics: Any, *, actor: str) -> dict[str, Any]:
        """Change a first-map proposal (unchecked topics, names, descriptions) and test it again."""
        key = self._require_key()
        proposal = await asyncio.to_thread(self.store.get_topic_proposal, proposal_id)
        if proposal is None:
            raise TopicError("Unknown proposal.", code="proposal_unknown")
        if proposal["op"] != "first_map" or proposal["status"] not in ("pending", "failed", "outdated"):
            raise TopicError("Only an open first-map proposal can be revised.", code="proposal_closed")
        if not isinstance(topics, list):
            raise TopicError("Send the proposal's topics.", code="payload_invalid")
        by_key = {t["key"]: t for t in proposal["payload"]["topics"]}
        revised = []
        for item in topics:
            original = by_key.get(item.get("key")) if isinstance(item, dict) else None
            if original is None:
                continue
            revised.append({**original, **{k: item[k] for k in ("name", "family", "description", "keep") if k in item}})
        if not any(t.get("keep", True) for t in revised):
            raise TopicError("Keep at least one topic.", code="payload_invalid")
        validate_topic_map([t for t in revised if t.get("keep", True)])
        payload = {**proposal["payload"], "topics": revised}
        kept = sum(1 for t in revised if t.get("keep", True))
        await asyncio.to_thread(
            lambda: self.store.update_topic_proposal(proposal_id, payload=payload, status="testing",
                                                    summary=f"First map: {kept} topics")
        )
        self._launch("test", lambda: self._test_proposal(proposal_id, key))
        return await asyncio.to_thread(self.store.get_topic_proposal, proposal_id)

    async def retest(self, proposal_id: int, *, actor: str) -> dict[str, Any]:
        key = self._require_key()
        proposal = await asyncio.to_thread(self.store.get_topic_proposal, proposal_id)
        if proposal is None:
            raise TopicError("Unknown proposal.", code="proposal_unknown")
        if proposal["status"] not in ("outdated", "failed", "pending"):
            raise TopicError("This proposal is already decided.", code="proposal_closed")
        self._launch("test", lambda: self._test_proposal(proposal_id, key))
        return proposal

    async def _test_proposal(self, proposal_id: int, key: str) -> None:
        if self.job is not None:
            self.job.update({
                "step": "testing", "proposal_id": proposal_id, "step_started_at": int(time.time()),
                "model": self.store.get_topic_settings()["jev_model"], "effort": None,
            })
        proposal = await asyncio.to_thread(self.store.get_topic_proposal, proposal_id)
        assert proposal is not None
        active = await asyncio.to_thread(self.store.active_topic_version)
        active_topics = active["topics"] if active else []
        try:
            candidate_topics = build_candidate(active_topics, proposal["op"], proposal["payload"])
            candidate = await asyncio.to_thread(
                lambda: self.store.create_topic_version(
                    candidate_topics, origin="proposal", actor="system", note=proposal["summary"][:300],
                    parent_id=active["version_id"] if active else None, candidate=True,
                )
            )
        except (TopicError, KeyError, ValueError) as exc:
            await asyncio.to_thread(lambda: self.store.update_topic_proposal(
                proposal_id, status="failed", test={"error": str(exc)}))
            return
        send, skipped = await asyncio.to_thread(self.eligible_chapters)
        settings = await asyncio.to_thread(self.store.get_topic_settings)
        run = await asyncio.to_thread(
            lambda: self.store.create_topic_run(
                kind="test", version_id=candidate["version_id"], scope={"mode": "all"}, settings=settings,
                chapters=send, skipped=skipped, actor="system", proposal_id=proposal_id,
                input_data={"criteria": jev_criteria(candidate["topics"])},
            )
        )
        await asyncio.to_thread(lambda: self.store.update_topic_proposal(
            proposal_id, status="testing", candidate_version_id=candidate["version_id"], test_run_id=run["run_id"],
            base_version_id=active["version_id"] if active else None,
        ))
        stop = await self._sort(run["run_id"], key)
        if stop is not None:
            await asyncio.to_thread(
                self.store.set_topic_run_status, run["run_id"], "failed", error_code=stop[0], error_message=stop[1]
            )
            await asyncio.to_thread(lambda: self.store.update_topic_proposal(
                proposal_id, status="failed", test={"error": stop[1]}))
            await asyncio.to_thread(
                self.store.add_topic_journal, "system", "test.fail", f"Test of “{proposal['summary']}” stopped: {stop[1]}",
                run_id=run["run_id"],
            )
            return
        chapters = {c["strand_id"]: c for c in send}
        before = await asyncio.to_thread(self.store.topic_current_assignments, active["version_id"]) if active else {}
        before = {cid: item for cid, item in before.items() if cid in chapters}
        after = await asyncio.to_thread(self.store.topic_run_assignments, run["run_id"])
        test = evaluate_test(
            op=proposal["op"], payload=proposal["payload"], active_topics=active_topics,
            candidate=candidate_topics, before=before, after=after, chapters=chapters,
        )
        run_now = await asyncio.to_thread(self.store.get_topic_run, run["run_id"])
        test["cost_usd"] = run_now["cost_usd"] if run_now else None
        await asyncio.to_thread(lambda: self.store.update_topic_proposal(proposal_id, status="pending", test=test))
        await asyncio.to_thread(
            self.store.add_topic_journal, "jev", "test.finish",
            f"Tested “{proposal['summary']}”: {test['verdict']}, {test['landed']}/{test['intended']} landed as intended, "
            f"{test['pulled_count']} pulled from elsewhere.",
            run_id=run["run_id"], data={"proposal_id": proposal_id},
        )

    # ---------- decisions ----------

    def accept(self, proposal_id: int, *, actor: str, note: str = "") -> dict[str, Any]:
        if self.running():
            raise TopicError("Wait for the running step to finish.", code="run_active")
        proposal = self.store.get_topic_proposal(proposal_id)
        if proposal is None:
            raise TopicError("Unknown proposal.", code="proposal_unknown")
        if proposal["status"] != "pending" or not proposal["candidate_version_id"]:
            raise TopicError("Only a tested, open proposal can be accepted.", code="proposal_closed")
        active = self.store.active_topic_version()
        if (active["version_id"] if active else None) != proposal["base_version_id"]:
            self.store.update_topic_proposal(proposal_id, status="outdated")
            raise TopicError("The map changed since this test. Test it again first.", code="proposal_outdated")
        version = self.store.activate_topic_version(proposal["candidate_version_id"])
        self.store.adopt_topic_run(proposal["test_run_id"])
        accepted = self.store.update_topic_proposal(
            proposal_id, status="accepted", decision_note=note or None, decided_at=int(time.time())
        )
        outdated = self.store.outdate_topic_proposals(version["version_id"])
        self.store.add_topic_journal(
            "owner", "proposal.accept",
            f"Accepted “{proposal['summary']}”: map v{version['version_id']} is active"
            + (f"; {outdated} other proposal{'s' if outdated != 1 else ''} need a new test." if outdated else "."),
            version_id=version["version_id"], data={"proposal_id": proposal_id},
        )
        return {"proposal": accepted, "version": version}

    def reject(self, proposal_id: int, *, actor: str, note: str = "") -> dict[str, Any]:
        proposal = self.store.get_topic_proposal(proposal_id)
        if proposal is None:
            raise TopicError("Unknown proposal.", code="proposal_unknown")
        if proposal["status"] in ("accepted", "rejected"):
            raise TopicError("This proposal is already decided.", code="proposal_closed")
        if not isinstance(note, str) or len(note) > NOTE_MAX_CHARS:
            raise TopicError("A note is short text.", code="payload_invalid")
        rejected = self.store.update_topic_proposal(
            proposal_id, status="rejected", decision_note=note.strip() or None, decided_at=int(time.time())
        )
        self.store.add_topic_journal(
            "owner", "proposal.reject",
            f"Rejected “{proposal['summary']}”" + (f": {note.strip()}" if note.strip() else ". It will not be proposed again."),
            data={"proposal_id": proposal_id},
        )
        return {"proposal": rejected}

    def restore(self, version_id: int, *, actor: str) -> dict[str, Any]:
        if self.running():
            raise TopicError("Wait for the running step to finish.", code="run_active")
        target = self.store.get_topic_version(version_id)
        if target is None or target["is_candidate"]:
            raise TopicError("Unknown map version.", code="version_unknown")
        version = self.store.activate_topic_version(version_id)
        outdated = self.store.outdate_topic_proposals(version_id)
        self.store.add_topic_journal(
            "owner", "map.restore", f"Made map v{version_id} active again"
            + (f"; {outdated} proposals need a new test." if outdated else "."), version_id=version_id,
        )
        return version


def _finish_message(run: dict[str, Any], metrics: dict[str, Any]) -> str:
    average = metrics.get("average_confidence")
    return (
        f"Sorted {metrics.get('done', 0)} chapters on map v{run['version_id']}: "
        f"{metrics.get('pool', 0)} unsorted, average confidence {average if average is not None else '-'}, "
        f"{metrics.get('weak_count', 0)} weak, cost ${metrics.get('cost_usd', 0):.4f}."
    )
