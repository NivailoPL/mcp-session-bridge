from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.storage import ExchangeRecord, SessionRecord
from app.time_format import format_response_timestamp, format_timestamp_iso, resolve_timezone_name


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    start_line: int
    end_line: int
    start_char: int
    end_char: int

    @property
    def line_count(self) -> int:
        if not self.text:
            return 0
        return self.text.count("\n") + (0 if self.text.endswith("\n") else 1)

    @property
    def char_count(self) -> int:
        return len(self.text)


def render_session_overview(
    session: SessionRecord,
    exchanges: list[ExchangeRecord],
    max_lines: int,
    max_chars: int,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    display_timezone_name = resolve_timezone_name(timezone_name)
    transcript = render_session_transcript(session, exchanges, timezone_name=display_timezone_name)
    chunks = chunk_text(transcript["transcript_markdown"], max_lines=max_lines, max_chars=max_chars)
    return {
        "session_id": session.session_id,
        "title": session.title,
        "title_is_auto": session.title_is_auto,
        "session_created_at": _format_ts(session.created_at),
        "session_updated_at": _format_ts(session.updated_at),
        "response_display_timezone": display_timezone_name,
        "response_display_format": "HH:MM (weekday, Month D, YYYY)",
        "exchange_count": transcript["exchange_count"],
        "turn_count": transcript["turn_count"],
        "transcript_char_count": transcript["char_count"],
        "transcript_line_count": _line_count(transcript["transcript_markdown"]),
        "transcript_sha256": transcript["sha256"],
        "transcript_chunk_count": len(chunks),
        "chunk_max_lines": max_lines,
        "chunk_max_chars": max_chars,
    }


def render_session_transcript_chunk(
    session: SessionRecord,
    exchanges: list[ExchangeRecord],
    chunk_index: int,
    max_lines: int,
    max_chars: int,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    transcript = render_session_transcript(session, exchanges, timezone_name=timezone_name)
    chunks = chunk_text(transcript["transcript_markdown"], max_lines=max_lines, max_chars=max_chars)
    if chunk_index < 1 or chunk_index > len(chunks):
        raise ValueError(f"chunk_index must be between 1 and {len(chunks)}")

    chunk = chunks[chunk_index - 1]
    next_chunk_index = chunk_index + 1 if chunk_index < len(chunks) else None
    return {
        "session_id": session.session_id,
        "title": session.title,
        "exchange_count": transcript["exchange_count"],
        "turn_count": transcript["turn_count"],
        "transcript_sha256": transcript["sha256"],
        "transcript_char_count": transcript["char_count"],
        "transcript_line_count": _line_count(transcript["transcript_markdown"]),
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "has_more": next_chunk_index is not None,
        "next_chunk_index": next_chunk_index,
        "chunk_max_lines": max_lines,
        "chunk_max_chars": max_chars,
        "chunk_start_line": chunk.start_line,
        "chunk_end_line": chunk.end_line,
        "chunk_start_char": chunk.start_char,
        "chunk_end_char": chunk.end_char,
        "chunk_line_count": chunk.line_count,
        "chunk_char_count": chunk.char_count,
        "transcript_markdown": chunk.text,
    }


def render_session_transcript(
    session: SessionRecord,
    exchanges: list[ExchangeRecord],
    timezone_name: str | None = None,
) -> dict[str, Any]:
    display_timezone_name = resolve_timezone_name(timezone_name)
    turns = _transcript_turns(exchanges, timezone_name=display_timezone_name)
    markdown = _render_transcript_markdown(session, exchanges, turns, timezone_name=display_timezone_name)
    return {
        "session_id": session.session_id,
        "title": session.title,
        "exchange_count": len(exchanges),
        "turn_count": len(turns),
        "char_count": len(markdown),
        "sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "turn_sequence": [turn["speaker"] for turn in turns],
        "turns": turns,
        "transcript_markdown": markdown,
    }


def chunk_text(text: str, max_lines: int, max_chars: int) -> list[TextChunk]:
    if max_lines < 1:
        raise ValueError("max_lines must be at least 1")
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")

    segments = _split_lines_for_chunking(text, max_chars=max_chars)
    if not segments:
        return [TextChunk(index=1, text="", start_line=1, end_line=1, start_char=0, end_char=0)]

    chunks: list[TextChunk] = []
    current: list[dict[str, Any]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        text_value = "".join(segment["text"] for segment in current)
        chunks.append(
            TextChunk(
                index=len(chunks) + 1,
                text=text_value,
                start_line=current[0]["line"],
                end_line=current[-1]["line"],
                start_char=current[0]["start_char"],
                end_char=current[-1]["end_char"],
            )
        )
        current = []
        current_chars = 0

    for segment in segments:
        segment_chars = len(segment["text"])
        if current and (len(current) >= max_lines or current_chars + segment_chars > max_chars):
            flush()
        current.append(segment)
        current_chars += segment_chars

    flush()
    return chunks


def _split_lines_for_chunking(text: str, max_chars: int) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    char_cursor = 0
    line_number = 1
    for line in text.splitlines(keepends=True):
        if not line:
            continue
        offset = 0
        while offset < len(line):
            part = line[offset : offset + max_chars]
            start_char = char_cursor + offset
            segments.append(
                {
                    "text": part,
                    "line": line_number,
                    "start_char": start_char,
                    "end_char": start_char + len(part),
                }
            )
            offset += len(part)
        char_cursor += len(line)
        line_number += 1
    return segments


def _transcript_turns(exchanges: list[ExchangeRecord], timezone_name: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for exchange in exchanges:
        created_at = _format_ts(exchange.created_at)
        assistant_created_at = _format_ts(exchange.assistant_created_at)
        assistant_created_at_display = format_response_timestamp(
            exchange.assistant_created_at,
            timezone_name=timezone_name,
        )
        turns.append(
            {
                "turn": len(turns) + 1,
                "speaker": "USER",
                "exchange_id": exchange.exchange_id,
                "created_at": created_at,
                "created_at_display": None,
                "chars": len(exchange.user_message),
                "content": exchange.user_message,
            }
        )
        turns.append(
            {
                "turn": len(turns) + 1,
                "speaker": exchange.model_name,
                "exchange_id": exchange.exchange_id,
                "created_at": assistant_created_at,
                "created_at_display": assistant_created_at_display,
                "chars": len(exchange.assistant_response),
                "content": exchange.assistant_response,
            }
        )
    return turns


def _render_transcript_markdown(
    session: SessionRecord,
    exchanges: list[ExchangeRecord],
    turns: list[dict[str, Any]],
    timezone_name: str,
) -> str:
    parts = [
        "# MCP Session Bridge Session Transcript",
        "",
        "## Metadata",
        "",
        f"- session_id: `{session.session_id}`",
        f"- title: {session.title}",
        f"- exchange_count: {len(exchanges)}",
        f"- turn_count: {len(turns)}",
        f"- session_created_at: {_format_ts(session.created_at)}",
        f"- session_updated_at: {_format_ts(session.updated_at)}",
        f"- response_display_timezone: {timezone_name}",
        "- response_display_format: HH:MM (weekday, Month D, YYYY)",
        "",
        "## Turn Sequence",
        "",
    ]

    if turns:
        parts.extend(turn["speaker"] for turn in turns)
    else:
        parts.append("No turns saved yet.")

    parts.extend(["", "## Transcript", ""])
    if not turns:
        parts.extend(["No exchanges saved yet.", ""])
    else:
        for turn in turns:
            created_at_display = turn["created_at_display"]
            heading = f"### {turn['speaker']} - {created_at_display}" if created_at_display else f"### {turn['speaker']}"
            display_line = f"<!-- created_at_display={created_at_display} -->" if created_at_display else ""
            parts.extend(
                [
                    heading,
                    "",
                    f"<!-- turn={turn['turn']} exchange_id={turn['exchange_id']} created_at={turn['created_at']} chars={turn['chars']} -->",
                    display_line,
                    "",
                    turn["content"],
                    "",
                ]
            )

    return "\n".join(parts).strip() + "\n"


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def _format_ts(timestamp: int) -> str:
    return format_timestamp_iso(timestamp)
