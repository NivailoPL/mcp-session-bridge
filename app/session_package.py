from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from app.context_packs import ContextPack
from app.storage import ExchangeRecord, SessionRecord


def render_session_package(
    session: SessionRecord,
    context_pack: ContextPack,
    exchanges: list[ExchangeRecord],
) -> dict[str, Any]:
    markdown = _render_markdown(session, context_pack, exchanges)
    return {
        "session_id": session.session_id,
        "title": session.title,
        "context_pack_id": session.context_pack_id,
        "context_pack_name": context_pack.name,
        "context_file_count": len(context_pack.files),
        "exchange_count": len(exchanges),
        "char_count": len(markdown),
        "sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "package_markdown": markdown,
    }


def _render_markdown(
    session: SessionRecord,
    context_pack: ContextPack,
    exchanges: list[ExchangeRecord],
) -> str:
    parts = [
        "# WW-MCP Session Package",
        "",
        "## Package Metadata",
        "",
        f"- session_id: `{session.session_id}`",
        f"- title: {session.title}",
        f"- context_pack_id: `{session.context_pack_id}`",
        f"- context_pack_name: {context_pack.name}",
        f"- context_pack_sha256: `{context_pack.content_hash}`",
        f"- session_created_at: {_format_ts(session.created_at)}",
        f"- session_updated_at: {_format_ts(session.updated_at)}",
        f"- context_file_count: {len(context_pack.files)}",
        f"- exchange_count: {len(exchanges)}",
        "",
        "## Model Protocol",
        "",
        "1. Przeczytaj cały pakiet przed odpowiedzią.",
        "2. Zacznij odpowiedź od formuły: `Odpowiada model <nazwa modelu>`.",
        "3. Odnoś się do wypowiedzi Wojtka i innych modeli po ich nazwach.",
        "4. Przed pokazaniem finalnej odpowiedzi zapisz pełną wymianę przez `save_exchange`.",
        "5. Nie wykonuj automatycznych podsumowań ani compaction, chyba że Wojtek wyraźnie o to poprosi.",
        "",
        "## Group Instructions",
        "",
        context_pack.instructions,
        "",
        "## Context Pack Files",
        "",
    ]

    for index, file in enumerate(context_pack.files, start=1):
        parts.extend(
            [
                f"### Context File {index}: {file.title}",
                "",
                f"- path: `{file.path}`",
                f"- chars: {file.chars}",
                f"- sha256: `{file.sha256}`",
                "",
                f"<!-- BEGIN CONTEXT FILE: {file.path} -->",
                "",
                file.content,
                "",
                f"<!-- END CONTEXT FILE: {file.path} -->",
                "",
            ]
        )

    parts.extend(["## Conversation Transcript", ""])
    if not exchanges:
        parts.extend(["No exchanges saved yet.", ""])
    else:
        for index, exchange in enumerate(exchanges, start=1):
            parts.extend(
                [
                    f"### Exchange {index}: {exchange.model_name}",
                    "",
                    f"- exchange_id: {exchange.exchange_id}",
                    f"- created_at: {_format_ts(exchange.created_at)}",
                    "",
                    "#### Wojtek",
                    "",
                    exchange.user_message,
                    "",
                    f"#### {exchange.model_name}",
                    "",
                    exchange.assistant_response,
                    "",
                ]
            )

    return "\n".join(parts).strip() + "\n"


def _format_ts(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
