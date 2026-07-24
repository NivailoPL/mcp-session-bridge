from __future__ import annotations

import math
import secrets
from collections.abc import Callable
from typing import Any


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


def probe_recommendations(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for run in runs:
        if run.get("result_class") != "complete":
            continue
        key = (str(run["harness_label"]), str(run["content_profile"]))
        if key not in best or int(run["payload_char_count"]) > int(best[key]["payload_char_count"]):
            best[key] = run
    return [
        {
            "harness_label": harness_label,
            "content_profile": content_profile,
            "largest_verified_payload_chars": int(run["payload_char_count"]),
            "recommended_chunk_chars": recommended_chunk_chars(int(run["payload_char_count"])),
            "run_id": run["run_id"],
        }
        for (harness_label, content_profile), run in sorted(best.items())
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
