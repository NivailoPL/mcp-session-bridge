from __future__ import annotations

import math
import secrets
from collections.abc import Callable
from typing import Any

from app.tool_output import TOOL_OUTPUT_MODE_MAXIMUM_COMPATIBILITY


TRANSCRIPT_CHUNK_MAX_CHARS_SETTING = "transcript.chunk_max_chars"
TRANSCRIPT_CHUNK_MAX_LINES_SETTING = "transcript.chunk_max_lines"
MIN_TRANSCRIPT_CHUNK_CHARS = 1_000
MAX_TRANSCRIPT_CHUNK_CHARS = 250_000
MIN_TRANSCRIPT_CHUNK_LINES = 1
MAX_TRANSCRIPT_CHUNK_LINES = 10_000
MIN_PROBE_TARGET_CHARS = 4_096
MAX_PROBE_TARGET_CHARS = 250_000
PROBE_BLOCK_SIZE = 1_024
PROBE_SAFETY_MARGIN = 0.25
PROBE_PROFILES = {"transcript_markdown", "transport_canary"}
CHECKPOINT_ORDER = ("FIRST", "Q1", "MID", "Q3", "FINAL")

_TRANSCRIPT_FILLER = (
    "### USER\n"
    "This synthetic transcript paragraph checks whether one complete MCP tool result reaches the model. "
    "The wording resembles ordinary conversation while each block carries an unpredictable integrity marker.\n\n"
    "### MODEL\n"
    "I will read the payload exactly as delivered, avoid reconstructing missing text, and report only marker values "
    "that are actually visible in this tool result.\n\n"
)


def build_output_probe(
    run_id: str,
    target_chars: int,
    content_profile: str,
    *,
    canary_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    if not MIN_PROBE_TARGET_CHARS <= target_chars <= MAX_PROBE_TARGET_CHARS:
        raise ValueError(
            f"target_chars must be between {MIN_PROBE_TARGET_CHARS} and {MAX_PROBE_TARGET_CHARS}"
        )
    if content_profile not in PROBE_PROFILES:
        raise ValueError(f"content_profile must be one of: {', '.join(sorted(PROBE_PROFILES))}")
    if not run_id or len(run_id) > 96:
        raise ValueError("run_id must be between 1 and 96 characters")

    make_canary = canary_factory or (lambda: secrets.token_hex(8).upper())
    provisional_header = _probe_header(run_id, target_chars, content_profile, 1)
    available = target_chars - len(provisional_header)
    if available < 512:
        raise ValueError("target_chars is too small for the probe envelope")

    block_count = max(1, available // PROBE_BLOCK_SIZE)
    header = _probe_header(run_id, target_chars, content_profile, block_count)
    available = target_chars - len(header)
    block_sizes = [PROBE_BLOCK_SIZE] * block_count
    block_sizes[-1] += available - sum(block_sizes)
    checkpoint_indexes = _checkpoint_indexes(block_count)

    blocks: list[dict[str, Any]] = []
    rendered: list[str] = [header]
    offset = len(header)
    for index, block_size in enumerate(block_sizes, start=1):
        canary = make_canary()
        labels = [label for label in CHECKPOINT_ORDER if checkpoint_indexes[label] == index]
        checkpoint_value = ",".join(labels) if labels else "-"
        prefix = (
            f"BEGIN|BLOCK={index:06d}|OFFSET={offset:09d}|"
            f"CHECKPOINT={checkpoint_value}|CANARY={canary}\n"
        )
        suffix = f"\nEND|BLOCK={index:06d}|CANARY={canary}\n"
        filler_length = block_size - len(prefix) - len(suffix)
        if filler_length < 1:
            raise ValueError("target_chars produced a block too small for integrity markers")
        filler = _probe_filler(content_profile, filler_length)
        text = f"{prefix}{filler}{suffix}"
        rendered.append(text)
        blocks.append(
            {
                "index": index,
                "offset": offset,
                "char_count": len(text),
                "canary": canary,
                "checkpoint_labels": labels,
            }
        )
        offset += len(text)

    payload = "".join(rendered)
    if len(payload) != target_chars:
        raise RuntimeError(f"probe payload length drifted: expected {target_chars}, got {len(payload)}")
    return {
        "payload": payload,
        "payload_char_count": len(payload),
        "block_count": block_count,
        "block_size": PROBE_BLOCK_SIZE,
        "blocks": blocks,
    }


def classify_probe_observation(
    blocks: list[dict[str, Any]],
    observed_checkpoint_canaries: list[str],
    last_complete_block_index: int,
    last_complete_canary: str,
) -> dict[str, Any]:
    if not blocks:
        raise ValueError("probe run has no block manifest")
    if len(observed_checkpoint_canaries) > 20:
        raise ValueError("observed_checkpoint_canaries accepts at most 20 values")

    submitted = {_normalize_canary(value) for value in observed_checkpoint_canaries if value.strip()}
    all_canaries = {str(block["canary"]).upper() for block in blocks}
    checkpoints = {
        label: str(block["canary"]).upper()
        for block in blocks
        for label in block["checkpoint_labels"]
    }
    matched = [label for label in CHECKPOINT_ORDER if checkpoints.get(label) in submitted]
    unknown_canaries = sorted(submitted - all_canaries)

    last_valid = False
    if 1 <= last_complete_block_index <= len(blocks):
        expected = str(blocks[last_complete_block_index - 1]["canary"]).upper()
        last_valid = secrets.compare_digest(expected, _normalize_canary(last_complete_canary))

    matched_set = set(matched)
    if matched_set == set(CHECKPOINT_ORDER) and last_complete_block_index == len(blocks) and last_valid:
        result_class = "complete"
    elif unknown_canaries or not last_valid:
        result_class = "verification_failed"
    elif "FIRST" not in matched_set and matched_set:
        result_class = "head_removed"
    elif "FIRST" in matched_set and "FINAL" not in matched_set:
        result_class = "tail_truncated"
    elif {"FIRST", "FINAL"} <= matched_set and matched_set != set(CHECKPOINT_ORDER):
        result_class = "middle_removed"
    else:
        result_class = "non_contiguous"

    return {
        "result_class": result_class,
        "matched_checkpoints": matched,
        "unknown_canary_count": len(unknown_canaries),
        "last_complete_valid": last_valid,
    }


def recommended_chunk_chars(verified_payload_chars: int) -> int:
    safe = math.floor(verified_payload_chars * (1 - PROBE_SAFETY_MARGIN) / 1_000) * 1_000
    return max(MIN_TRANSCRIPT_CHUNK_CHARS, min(safe, MAX_TRANSCRIPT_CHUNK_CHARS))


def transcript_chunk_limits(store: Any, default_chars: int, default_lines: int) -> tuple[int, int]:
    chars = _stored_int(
        store.get_app_setting(TRANSCRIPT_CHUNK_MAX_CHARS_SETTING),
        default_chars,
        MIN_TRANSCRIPT_CHUNK_CHARS,
        MAX_TRANSCRIPT_CHUNK_CHARS,
    )
    lines = _stored_int(
        store.get_app_setting(TRANSCRIPT_CHUNK_MAX_LINES_SETTING),
        default_lines,
        MIN_TRANSCRIPT_CHUNK_LINES,
        MAX_TRANSCRIPT_CHUNK_LINES,
    )
    return chars, lines


def validate_transcript_chunk_limits(chars: Any, lines: Any) -> tuple[int, int]:
    if isinstance(chars, bool) or not isinstance(chars, int):
        raise ValueError("chunk_max_chars must be an integer")
    if isinstance(lines, bool) or not isinstance(lines, int):
        raise ValueError("chunk_max_lines must be an integer")
    if not MIN_TRANSCRIPT_CHUNK_CHARS <= chars <= MAX_TRANSCRIPT_CHUNK_CHARS:
        raise ValueError(
            f"chunk_max_chars must be between {MIN_TRANSCRIPT_CHUNK_CHARS} and {MAX_TRANSCRIPT_CHUNK_CHARS}"
        )
    if not MIN_TRANSCRIPT_CHUNK_LINES <= lines <= MAX_TRANSCRIPT_CHUNK_LINES:
        raise ValueError(
            f"chunk_max_lines must be between {MIN_TRANSCRIPT_CHUNK_LINES} and {MAX_TRANSCRIPT_CHUNK_LINES}"
        )
    return chars, lines


def public_probe_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        key: run.get(key)
        for key in (
            "run_id",
            "harness_label",
            "content_profile",
            "target_chars",
            "payload_char_count",
            "server_result_json_chars",
            "block_count",
            "block_size",
            "tool_output_mode",
            "status",
            "result_class",
            "matched_checkpoints",
            "last_complete_block_index",
            "noticed_truncation",
            "notes",
            "created_by",
            "created_at",
            "reported_at",
        )
    } | {
        "recommended_chunk_chars": (
            recommended_chunk_chars(int(run["payload_char_count"]))
            if run.get("result_class") == "complete"
            else None
        )
    }


def probe_recommendations(
    runs: list[dict[str, Any]],
    *,
    tool_output_mode: str | None = None,
) -> list[dict[str, Any]]:
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for run in runs:
        if run.get("result_class") != "complete":
            continue
        run_mode = str(run.get("tool_output_mode") or TOOL_OUTPUT_MODE_MAXIMUM_COMPATIBILITY)
        if tool_output_mode is not None and run_mode != tool_output_mode:
            continue
        key = (str(run["harness_label"]), str(run["content_profile"]), run_mode)
        if key not in best or int(run["payload_char_count"]) > int(best[key]["payload_char_count"]):
            best[key] = run
    return [
        {
            "harness_label": harness_label,
            "content_profile": content_profile,
            "tool_output_mode": run_mode,
            "largest_verified_payload_chars": int(run["payload_char_count"]),
            "recommended_chunk_chars": recommended_chunk_chars(int(run["payload_char_count"])),
            "run_id": run["run_id"],
        }
        for (harness_label, content_profile, run_mode), run in sorted(best.items())
    ]


def _probe_header(run_id: str, target_chars: int, content_profile: str, block_count: int) -> str:
    return (
        "MCP OUTPUT INTEGRITY PROBE\n"
        f"RUN_ID={run_id}\n"
        f"TARGET_CHARS={target_chars:09d}\n"
        f"CONTENT_PROFILE={content_profile}\n"
        f"TOTAL_BLOCKS={block_count:06d}\n"
        "INSTRUCTIONS=Read the delivered tool result without summarizing it. Immediately call "
        "submit_output_probe_observation with every visible CHECKPOINT canary and the index and canary "
        "of the last fully closed block. Do not infer or reconstruct missing canaries.\n"
        "PAYLOAD_BEGIN\n"
    )


def _checkpoint_indexes(block_count: int) -> dict[str, int]:
    return {
        "FIRST": 1,
        "Q1": max(1, math.ceil(block_count * 0.25)),
        "MID": max(1, math.ceil(block_count * 0.50)),
        "Q3": max(1, math.ceil(block_count * 0.75)),
        "FINAL": block_count,
    }


def _probe_filler(content_profile: str, length: int) -> str:
    if content_profile == "transcript_markdown":
        return (_TRANSCRIPT_FILLER * math.ceil(length / len(_TRANSCRIPT_FILLER)))[:length]
    pieces: list[str] = []
    remaining = length
    while remaining:
        piece = secrets.token_urlsafe(min(48, remaining))
        piece = piece[:remaining]
        pieces.append(piece)
        remaining -= len(piece)
    return "".join(pieces)


def _normalize_canary(value: str) -> str:
    normalized = value.strip().upper()
    for separator in (":", "="):
        if separator in normalized:
            normalized = normalized.rsplit(separator, 1)[-1].strip()
    return normalized


def _stored_int(raw: str | None, fallback: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw) if raw is not None else fallback
    except (TypeError, ValueError):
        return fallback
    return value if minimum <= value <= maximum else fallback


SUITE_START_CHARS = 32_000
SUITE_MAX_STEPS = 7
SUITE_CONFIRM_RATIO = 0.95
SUITE_MIN_GAP_CHARS = 4_000
SUITE_MIN_GAP_RATIO = 0.10
STEP_OUTCOMES = {
    "complete", "truncated", "altered", "rejected", "no_report", "verification_failed", "spilled",
}
SUITE_DELIVERY_EXTRA_STEPS = 4
DELIVERY_REPORTS = ("context", "file", "no_file", "file_unreachable", "unsure")
# Rough chars per token, for the panel's token view only. Prose: 100,000 chars hit claude.ai's
# 25,000-token limit (GPT tokenizers give ~5). Random canary filler: ~1.5 on GPT tokenizers.
CHARS_PER_TOKEN = {"transcript_markdown": 4.0, "transport_canary": 1.5}
RECALL_BLOCK_COUNT = 3


def step_outcome(result_class: str | None, *, harness_error: str = "", reported: bool = True) -> str:
    """Map one probe observation to the outcome the suite algorithm works with."""
    if not reported:
        return "no_report"
    if harness_error.strip():
        return "rejected"
    return {
        "complete": "complete",
        "tail_truncated": "truncated",
        "verification_failed": "verification_failed",
    }.get(str(result_class), "altered")


def cut_point_from_observation(
    blocks: list[dict[str, Any]],
    result_class: str,
    last_complete_block_index: int,
    last_complete_valid: bool,
) -> int | None:
    """Payload chars that reached the model before the harness cut the tail, or None."""
    if result_class != "tail_truncated" or not last_complete_valid:
        return None
    if not 1 <= last_complete_block_index <= len(blocks):
        return None
    block = blocks[last_complete_block_index - 1]
    return int(block["offset"]) + int(block["char_count"])


def suite_bounds(steps: list[dict[str, Any]]) -> tuple[int | None, int | None, dict[str, Any] | None]:
    """Return (lo, hi, step that set hi): the largest whole pass and the smallest failing bound."""
    lo: int | None = None
    hi: int | None = None
    hi_step: dict[str, Any] | None = None
    for step in steps:
        target = int(step["target_chars"])
        if step["outcome"] == "complete":
            lo = target if lo is None else max(lo, target)
            continue
        bound = int(step["cut_at_chars"]) if step.get("cut_at_chars") else target
        if hi is None or bound < hi:
            hi, hi_step = bound, step
    return lo, hi, hi_step


def gap_is_tight(lo: int, hi: int) -> bool:
    return hi - lo < max(SUITE_MIN_GAP_CHARS, lo * SUITE_MIN_GAP_RATIO)


def round_down_thousand(value: float) -> int:
    return int(value // 1_000) * 1_000


def next_probe_step(
    steps: list[dict[str, Any]],
    *,
    start: int = SUITE_START_CHARS,
    floor: int = MIN_PROBE_TARGET_CHARS,
    ceiling: int = MAX_PROBE_TARGET_CHARS,
    max_steps: int = SUITE_MAX_STEPS,
) -> int | None:
    """Pick the next probe size for a suite, or None when the suite has its answer.

    `steps` are the finished steps in order, each {"target_chars", "outcome", "cut_at_chars"}.
    """
    if not steps:
        return start
    lo, hi, _ = suite_bounds(steps)
    last = steps[-1]
    if len(steps) >= max_steps:
        return None
    if lo is not None and lo >= ceiling:
        return None
    if lo is None and hi is not None and hi <= floor:
        return None
    if lo is not None and hi is not None and gap_is_tight(lo, hi):
        return None
    if last["outcome"] == "truncated" and hi is not None:
        confirm = max(floor, round_down_thousand(hi * SUITE_CONFIRM_RATIO))
        if lo is None or confirm > lo:
            return confirm
    if lo is None:
        return max(floor, round_down_thousand(hi / 2))
    if hi is None:
        return min(ceiling, lo * 2)
    middle = round_down_thousand((lo + hi) / 2)
    return middle if middle > lo else None


def limit_verdict(steps: list[dict[str, Any]], *, ceiling: int = MAX_PROBE_TARGET_CHARS) -> dict[str, Any]:
    """Summarize steps of one limit search as a verdict enum plus the numbers the report needs."""
    lo, hi, hi_step = suite_bounds(steps)
    if not steps:
        verdict = "no_steps"
    elif lo is not None and lo >= ceiling:
        verdict = "no_limit"
    elif lo is None:
        verdict = "fails_small"
    elif hi is None:
        verdict = "passes_up_to"
    else:
        # Name the limit after how the first failing size failed.
        verdict = {
            "truncated": "truncates",
            "altered": "drops_middle",
            "rejected": "rejects",
            "spilled": "leaves_context",
        }.get(hi_step["outcome"] if hi_step else "", "fails_above")
    return {
        "verdict": verdict,
        "largest_complete_chars": lo,
        "smallest_failing_chars": hi,
        "failure_outcome": hi_step["outcome"] if hi_step else None,
        "recommended_chunk_chars": recommended_chunk_chars(lo) if lo is not None else None,
        "cumulative_chars": sum(int(step["target_chars"]) for step in steps),
        "step_count": len(steps),
    }


def effective_outcome(run: dict[str, Any]) -> str | None:
    """The outcome under current rules.

    A full recall after reading the result from a file was once stored as complete; it does
    not prove what the harness puts in context, so it now reads as spilled everywhere.
    """
    outcome = run.get("outcome")
    if (
        outcome == "complete"
        and (run.get("phase") or "context") == "context"
        and run.get("recall_blocks")
        and run.get("delivery") == "file"
    ):
        return "spilled"
    return outcome


def suite_steps(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Finished suite runs as the step dicts the planner reads; unfinished runs are skipped."""
    return [
        {
            "target_chars": int(run["target_chars"]),
            "outcome": effective_outcome(run),
            "cut_at_chars": run.get("cut_at_chars"),
            "phase": run.get("phase") or "context",
        }
        for run in runs
        if run.get("outcome") in STEP_OUTCOMES
    ]


def context_view(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Steps of the context search: whole delivery that the model can also recall from memory."""
    return [step for step in steps if step.get("phase", "context") == "context"]


def delivery_view(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Steps judged by delivery alone.

    A spilled result still arrived whole, so it counts as delivered. Other failures
    count only from the delivery phase: in the context phase the model was told not
    to reach for files, so a cut there says nothing about what a file could carry.
    """
    view = []
    for step in steps:
        if step["outcome"] == "spilled":
            view.append(step | {"outcome": "complete"})
        elif step["outcome"] == "complete" or step.get("phase") == "delivery":
            view.append(step)
    return view


DELIVERY_AFTER = {"spilled", "truncated"}


def plan_next_step(
    steps: list[dict[str, Any]],
    *,
    start: int = SUITE_START_CHARS,
    ceiling: int = MAX_PROBE_TARGET_CHARS,
) -> tuple[int, str] | None:
    """Next (size, phase) of a suite, or None when both limits are known.

    Phase "context" finds how much the model really holds in context. When that
    limit ended in a spill or a cut (a harness may show only the start and keep the
    rest in a file the model never opened), phase "delivery" asks the model to use
    any file or tool it has and finds how much one tool call can carry. It opens at
    the ceiling, since a whole delivery there settles the question in one step.
    """
    context = context_view(steps)
    target = next_probe_step(context, start=start)
    if target is not None:
        return target, "context"
    _, _, hi_step = suite_bounds(context)
    if hi_step is None or hi_step["outcome"] not in DELIVERY_AFTER:
        return None
    if len(steps) >= SUITE_MAX_STEPS + SUITE_DELIVERY_EXTRA_STEPS:
        return None
    delivery = delivery_view(steps)
    if not any(step.get("phase") == "delivery" for step in steps):
        lo, _, _ = suite_bounds(delivery)
        return None if lo is not None and lo >= ceiling else (ceiling, "delivery")
    target = next_probe_step(delivery, max_steps=SUITE_MAX_STEPS + SUITE_DELIVERY_EXTRA_STEPS)
    return (target, "delivery") if target is not None else None


def suite_verdict(steps: list[dict[str, Any]], *, ceiling: int = MAX_PROBE_TARGET_CHARS) -> dict[str, Any]:
    """Context verdict at the top level, plus what one tool call can deliver."""
    verdict = limit_verdict(context_view(steps), ceiling=ceiling)
    delivered = limit_verdict(delivery_view(steps), ceiling=ceiling)
    cut_in_context = verdict["smallest_failing_chars"]
    if (
        verdict["verdict"] in ("truncates", "leaves_context")
        and cut_in_context is not None
        and (delivered["largest_complete_chars"] or 0) > cut_in_context
    ):
        # The tail was missing from context, yet a delivery check got past it: the harness
        # shows the start in context and keeps the rest in a file.
        verdict = verdict | {"verdict": "context_then_file"}
    return verdict | {
        "step_count": len(steps),
        "cumulative_chars": sum(int(step["target_chars"]) for step in steps),
        "largest_delivered_chars": delivered["largest_complete_chars"],
        "smallest_undelivered_chars": delivered["smallest_failing_chars"],
        "delivery_verdict": delivered["verdict"],
    }


def pick_recall_blocks(blocks: list[dict[str, Any]], count: int = RECALL_BLOCK_COUNT) -> list[int]:
    """Blocks for the memory check, chosen after delivery, in payload order.

    The last one is always the final non-checkpoint block: a harness that shows
    only the start of a large result in context (and keeps the rest in a file)
    loses the end first. The others are random, one per earlier slice.
    """
    candidates = [int(block["index"]) for block in blocks if not block["checkpoint_labels"]]
    if len(candidates) < count:
        return []
    tail = candidates[-1]
    earlier = candidates[:-1]
    rng = secrets.SystemRandom()
    size = len(earlier) / (count - 1)
    picks = [rng.choice(earlier[int(i * size):int((i + 1) * size)]) for i in range(count - 1)]
    return picks + [tail]


def recall_hits(blocks: list[dict[str, Any]], asked: list[int], answers: list[str]) -> list[bool]:
    """For each asked block, in order, whether the model named its canary correctly."""
    by_index = {int(block["index"]): str(block["canary"]).upper() for block in blocks}
    hits = []
    for position, index in enumerate(asked):
        answer = _normalize_canary(answers[position]) if position < len(answers) else ""
        hits.append(bool(answer) and secrets.compare_digest(answer, by_index.get(index, "")))
    return hits


def score_recall(blocks: list[dict[str, Any]], asked: list[int], answers: list[str]) -> int:
    """How many asked blocks the model named correctly, in order."""
    return sum(recall_hits(blocks, asked, answers))


SUITE_CONTEXT_SUSPECT_CHARS = 150_000
SUITE_STALE_SECONDS = 24 * 60 * 60
_PUBLIC_STEP_FIELDS = (
    "run_id", "step_index", "target_chars", "payload_char_count", "status", "outcome",
    "result_class", "matched_checkpoints", "cut_at_chars", "harness_error",
    "cumulative_chars", "tool_output_mode", "created_at", "reported_at",
    "phase", "recall_blocks", "recall_score", "recall_hits", "delivery",
)


def harness_test_prompt(suite_id: str) -> str:
    return (
        "Use MCP Session Bridge to measure how large a tool result this harness can receive. "
        "Do this in a fresh chat if you can. "
        "If next_harness_probe is not in your tool list, stop and tell me to refresh this connector's tools; "
        "do not use run_output_probe or any other tool instead. "
        f'Call next_harness_probe with suite_id="{suite_id}". '
        "If it returns done=true, stop and tell me only the verdict and recommended_chunk_chars. "
        "Otherwise read probe_payload without summarizing it, then immediately call report_harness_probe "
        "with the same suite_id, every CHECKPOINT canary you can actually see, and the index and canary "
        "of the last block with a complete END marker, and delivery: \"context\" if the payload is directly "
        "in your context, \"file\" if you read it through a file or another tool, \"no_file\" if part was "
        "cut and the harness kept no full copy, \"file_unreachable\" if a full copy exists but you cannot "
        "open it, \"unsure\" otherwise. "
        "Steps marked phase=delivery are different: if the harness saved the full result to a file or "
        "offers a tool to read it, use that and report canaries from the full result. "
        "When report_harness_probe asks for a memory check, answer it with answer_recall_check from your "
        "context alone: no tools, no files, empty strings for blocks you cannot see. "
        "If next_harness_probe failed or returned an error "
        "instead of a payload, call report_harness_probe with the error text as harness_error and an empty "
        "canary list. Never guess or reconstruct a canary. Repeat until a tool answers done=true."
    )


def public_harness_suite(suite: dict[str, Any], *, now: int) -> dict[str, Any]:
    """A suite for the admin UI: no canaries or block manifests, plus derived report fields."""
    steps = []
    in_chat = 0  # chars that stayed in the conversation; spilled results sit in a file
    for run in suite["steps"]:
        step = {key: run.get(key) for key in _PUBLIC_STEP_FIELDS}
        step["outcome"] = effective_outcome(run)
        # Only the context search can be skewed by a full chat; delivery steps measure the harness.
        step["context_suspect"] = (
            (run.get("phase") or "context") == "context"
            and run.get("outcome") not in (None, "complete")
            and in_chat > SUITE_CONTEXT_SUSPECT_CHARS
        )
        step["chars_in_chat_before"] = in_chat
        if run.get("outcome") == "truncated":
            in_chat += int(run.get("cut_at_chars") or 0)
        elif effective_outcome(run) in ("complete", "altered") and (run.get("phase") or "context") == "context":
            in_chat += int(run["target_chars"])
        steps.append(step)
    # Recomputed on read, so suites stored before a verdict was renamed show the current name.
    verdict = suite_verdict(suite_steps(suite["steps"]))
    status = suite["status"]
    if status == "running" and now - int(suite["updated_at"]) > SUITE_STALE_SECONDS:
        status = "unfinished"
    return {
        key: suite[key]
        for key in (
            "suite_id", "harness_label", "content_profile", "mode", "start_chars",
            "created_at", "updated_at",
        )
    } | {"status": status, "verdict": verdict, "steps": steps}


def safe_chunk_recommendation(recommendations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The weakest harness decides the chunk size, counting only transcript-like probes."""
    relevant = [item for item in recommendations if item["content_profile"] == "transcript_markdown"]
    if not relevant:
        return None
    return min(relevant, key=lambda item: item["recommended_chunk_chars"])
