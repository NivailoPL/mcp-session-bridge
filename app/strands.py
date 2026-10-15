"""Strands: named sets of exchange ranges inside one session.

A strand is an index into the transcript, never a summary of it: a title plus
inclusive exchange ranges. The owner creates strands by hand or asks Codex,
one session at a time and only on demand, to propose them from the whole
transcript. Proposals stay pending until the owner accepts them, and every
strand from one Codex run can be reverted together.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

STRAND_ORIGINS = ("manual", "codex")
STRAND_STATUSES = ("pending", "accepted")
STRAND_TITLE_MAX_CHARS = 120
STRAND_MAX_RANGES = 40
STRAND_MAX_PROPOSALS = 40

STRAND_SETTINGS_KEY = "strands.settings"
# Token-spending features can be switched off one by one or all at once. Both
# default to on so turning the feature into an opt-in never hides existing data.
TOKEN_FEATURES_MASTER_KEY = "features.token_spend.enabled"
TOKEN_FEATURES_STRANDS_KEY = "features.strands.enabled"
STRAND_EFFORTS = ("low", "medium", "high", "xhigh")
STRAND_MODEL_MAX_CHARS = 96
STRAND_PROMPT_MAX_CHARS = 24_000
# The whole transcript goes to Codex in one turn; this leaves room for the
# prompt inside the Codex structured-input limit.
STRAND_SOURCE_MAX_CHARS = 460_000

DEFAULT_STRAND_PROMPT = """Split this conversation into strands.

A strand is one subject the human pursued. Give each strand a short title (at most 80 characters, in the language of the conversation) and the exchange ranges where that subject is discussed, using the exchange numbers shown in the transcript. A subject the human comes back to later gets several ranges in one strand. Short asides that matter still get their own strand.

Follow the human's framing rather than the model's. Do not summarize or judge the content; only name and locate the subjects."""

STRAND_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["strands"],
    "properties": {
        "strands": {
            "type": "array",
            "minItems": 1,
            "maxItems": STRAND_MAX_PROPOSALS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "ranges"],
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": STRAND_TITLE_MAX_CHARS},
                    "ranges": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": STRAND_MAX_RANGES,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["start", "end"],
                            "properties": {
                                "start": {"type": "integer"},
                                "end": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        }
    },
}


class StrandError(ValueError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def default_strand_settings() -> dict[str, str]:
    return {"model": "gpt-5.6-sol", "effort": "medium", "prompt": DEFAULT_STRAND_PROMPT}


def validate_strand_settings(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict) or not set(payload) <= {"model", "effort", "prompt"}:
        raise StrandError("Strand settings accept model, effort and prompt.", code="settings_invalid")
    model = payload.get("model")
    if (
        not isinstance(model, str)
        or not model.strip()
        or len(model.strip()) > STRAND_MODEL_MAX_CHARS
        or not re.fullmatch(r"[A-Za-z0-9._-]+", model.strip())
    ):
        raise StrandError("model must be a valid model identifier.", code="model_invalid")
    effort = payload.get("effort")
    if effort not in STRAND_EFFORTS:
        raise StrandError("effort must be low, medium, high, or xhigh.", code="effort_invalid")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise StrandError("prompt must not be empty.", code="prompt_invalid")
    if len(prompt) > STRAND_PROMPT_MAX_CHARS:
        raise StrandError(
            f"prompt must be {STRAND_PROMPT_MAX_CHARS} characters or fewer.", code="prompt_invalid"
        )
    return {"model": model.strip(), "effort": str(effort), "prompt": prompt.strip()}


def validate_title(title: Any) -> str:
    if not isinstance(title, str) or not title.strip():
        raise StrandError("A strand needs a title.", code="title_invalid")
    cleaned = " ".join(title.split())
    if len(cleaned) > STRAND_TITLE_MAX_CHARS:
        raise StrandError(
            f"A strand title can hold at most {STRAND_TITLE_MAX_CHARS} characters.", code="title_invalid"
        )
    return cleaned


def normalize_ranges(ranges: Any, positions: dict[int, int]) -> list[tuple[int, int]]:
    """Validate inclusive exchange ranges and merge overlapping or touching ones.

    ``positions`` maps each eligible exchange_id of the session to its order in
    the transcript, so ranges follow conversation order, not id arithmetic.
    """
    if not isinstance(ranges, Sequence) or isinstance(ranges, (str, bytes)) or not ranges:
        raise StrandError("A strand needs at least one exchange range.", code="ranges_invalid")
    if len(ranges) > STRAND_MAX_RANGES:
        raise StrandError(f"A strand can have at most {STRAND_MAX_RANGES} ranges.", code="ranges_invalid")
    spans: list[tuple[int, int]] = []
    for item in ranges:
        start, end = _range_ends(item)
        if start not in positions or end not in positions:
            raise StrandError(
                "A range points at an exchange that is not part of this session.", code="range_unknown"
            )
        if positions[start] > positions[end]:
            raise StrandError("A range must start before it ends.", code="range_reversed")
        spans.append((positions[start], positions[end]))
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    by_position = {position: exchange_id for exchange_id, position in positions.items()}
    return [(by_position[start], by_position[end]) for start, end in merged]


def _range_ends(item: Any) -> tuple[int, int]:
    if isinstance(item, dict):
        start = item.get("start", item.get("start_exchange_id"))
        end = item.get("end", item.get("end_exchange_id"))
    elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
        start, end = item
    else:
        raise StrandError("Each range needs a start and an end exchange.", code="ranges_invalid")
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
        raise StrandError("Range ends must be exchange numbers.", code="ranges_invalid")
    return start, end


def build_strand_prompt(prompt: str, exchanges: Sequence[Any]) -> str:
    """Render the whole session with exchange numbers; refuse instead of truncating."""
    blocks: list[str] = []
    for exchange in exchanges:
        assistant_text = "" if exchange.assistant_masked_at is not None else exchange.assistant_response
        assistant = f"\n\nMODEL:\n{assistant_text}" if assistant_text else ""
        blocks.append(f"Exchange {exchange.exchange_id}\nHUMAN:\n{exchange.user_message}{assistant}")
    if not blocks:
        raise StrandError("This session has no exchanges to split.", code="source_empty")
    transcript = "\n\n---\n\n".join(blocks)
    if len(transcript) > STRAND_SOURCE_MAX_CHARS:
        raise StrandError(
            "This session is too long for one Codex run. Nothing was sent or cut.", code="source_too_large"
        )
    return (
        f"{prompt}\n\n"
        "Return only the structured result. Use only exchange numbers that appear below.\n\n"
        f"SESSION TRANSCRIPT\n\n{transcript}"
    )


def validate_strand_result(raw: Any, positions: dict[int, int]) -> list[dict[str, Any]]:
    """Validate a Codex result; any invalid strand rejects the whole run."""
    if not isinstance(raw, dict) or set(raw) != {"strands"} or not isinstance(raw["strands"], list):
        raise StrandError("Codex returned an unexpected result shape.", code="result_invalid")
    strands = raw["strands"]
    if not strands:
        raise StrandError("Codex proposed no strands.", code="result_empty")
    if len(strands) > STRAND_MAX_PROPOSALS:
        raise StrandError("Codex proposed too many strands.", code="result_invalid")
    proposals: list[dict[str, Any]] = []
    for item in strands:
        if not isinstance(item, dict) or set(item) != {"title", "ranges"}:
            raise StrandError("Codex returned an unexpected strand shape.", code="result_invalid")
        proposals.append(
            {"title": validate_title(item["title"]), "ranges": normalize_ranges(item["ranges"], positions)}
        )
    return proposals
