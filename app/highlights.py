"""Highlights: verbatim quotes the owner marks inside one turn of an exchange.

The admin sends the text of a browser selection, which comes from rendered
markdown, so the quote is matched against the stored turn text after a tolerant
normalization that drops markup and collapses whitespace. A highlight keeps its
own copy of the quote; its source status is computed on read, so later edits,
exclusion or masking of the exchange never delete what the owner marked.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

HIGHLIGHT_SIDES = ("user", "model")
HIGHLIGHT_MIN_CHARS = 3
HIGHLIGHT_MAX_CHARS = 2_000

STATUS_OK = "ok"
STATUS_SOURCE_CHANGED = "source_changed"
STATUS_SOURCE_EXCLUDED = "source_excluded"
STATUS_SOURCE_MASKED = "source_masked"
STATUS_SOURCE_DELETED = "source_deleted"

_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
_MARKUP = re.compile(r"[*_`#>|~]")
_WHITESPACE = re.compile(r"\s+")


class HighlightError(ValueError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def normalize_for_match(text: str) -> str:
    """Reduce markdown or rendered text to comparable plain words."""
    value = unicodedata.normalize("NFKC", text)
    value = _LINK.sub(r"\1", value)
    value = _LIST_MARKER.sub("", value)
    value = _ESCAPE.sub(r"\1", value)
    value = _MARKUP.sub("", value)
    value = _WHITESPACE.sub(" ", value)
    return value.strip().casefold()


def clean_quote(quote: object) -> str:
    """Validate a submitted quote and return it with outer whitespace trimmed."""
    if not isinstance(quote, str):
        raise HighlightError("quote must be a string.", code="quote_invalid")
    cleaned = quote.strip()
    if len(normalize_for_match(cleaned)) < HIGHLIGHT_MIN_CHARS:
        raise HighlightError("Select at least a few characters to highlight.", code="quote_too_short")
    if len(cleaned) > HIGHLIGHT_MAX_CHARS:
        raise HighlightError(
            f"A highlight can hold at most {HIGHLIGHT_MAX_CHARS} characters.", code="quote_too_long"
        )
    return cleaned


def validate_side(side: object) -> str:
    if side not in HIGHLIGHT_SIDES:
        raise HighlightError("side must be user or model.", code="side_invalid")
    return str(side)


def quote_found(quote: str, source_text: str) -> bool:
    return normalize_for_match(quote) in normalize_for_match(source_text)


def quote_hash(quote: str) -> str:
    return hashlib.sha256(normalize_for_match(quote).encode("utf-8")).hexdigest()


def source_status(
    quote: str,
    *,
    side: str,
    exists: bool,
    deleted: bool = False,
    masked: bool = False,
    source_text: str = "",
) -> str:
    if not exists:
        return STATUS_SOURCE_DELETED
    if deleted:
        return STATUS_SOURCE_EXCLUDED
    if side == "model" and masked:
        return STATUS_SOURCE_MASKED
    if not quote_found(quote, source_text):
        return STATUS_SOURCE_CHANGED
    return STATUS_OK
