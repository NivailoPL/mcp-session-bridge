"""Remove dereferenceable bridge identifiers from text handed to a model as a context.

A context is meant to work as an information sandbox: a model reading it must not
find, at least not trivially, a handle it could pass back to Bridge tools to reach
the sources. The only such handles are session IDs (every file and transcript tool
needs one), context IDs (``get_context`` takes one) and links into this Bridge's
own host. Everything else in the text - titles, model names, group names - is left
as it is on purpose.

Replacements are numbered per redaction run, so the same identifier gets the same
placeholder throughout one context and the numbering cannot be matched across
contexts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import quote, urlsplit


SESSION_PLACEHOLDER = "SESSION"
CONTEXT_PLACEHOLDER = "CONTEXT"
URL_PLACEHOLDER = "URL"

# Session IDs are minted as ``YYYYMMDD-HHMMSS-<slug>-<6 hex>`` (see
# ``_new_session_id``). Matching the shape catches IDs of sessions that no longer
# exist or were never in this database.
SESSION_ID_SHAPE = r"\d{8}-\d{6}-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{6}"
CONTEXT_ID_SHAPE = r"ctx_[A-Za-z0-9]{6,}"

# Known IDs shorter than this are not matched literally: a two-letter test ID or a
# legacy one-word ID would otherwise erase ordinary words from the text.
MIN_LITERAL_ID_LENGTH = 12

_ID_CHAR = r"A-Za-z0-9"
# A path runs to the next space or bracket, but never ends on sentence punctuation:
# "see https://host/admin." keeps its full stop.
_URL_TAIL = r"""(?:[^\s<>"'`()\[\]{}]*[^\s<>"'`()\[\]{}.,;:!?])?"""


@dataclass(frozen=True)
class Redaction:
    kind: str
    original: str
    placeholder: str
    start: int
    end: int


@dataclass
class RedactionResult:
    text: str
    redactions: list[Redaction] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.redactions)


class Redactor:
    """Builds one pattern for a set of known identifiers and the Bridge hosts."""

    def __init__(
        self,
        *,
        session_ids: Iterable[str] = (),
        context_ids: Iterable[str] = (),
        hosts: Iterable[str] = (),
    ) -> None:
        self.session_ids = _literal_ids(session_ids)
        self.context_ids = _literal_ids(context_ids)
        self.hosts = sorted({host for host in (_host(value) for value in hosts) if host}, key=len, reverse=True)
        self._pattern = self._build_pattern(include_urls=True)
        # IDs inside a link are found too: the link is replaced whole, but what it
        # pointed at is still worth remembering.
        self._id_pattern = self._build_pattern(include_urls=False)

    def find(self, text: str) -> dict[str, list[str]]:
        """Identifiers present in ``text``, by kind. Used to remember what a snapshot held."""
        found: dict[str, set[str]] = {SESSION_PLACEHOLDER: set(), CONTEXT_PLACEHOLDER: set()}
        for match in self._id_pattern.finditer(text):
            kind = match.lastgroup
            if kind == "session":
                found[SESSION_PLACEHOLDER].add(_normalize_session(match.group()))
            elif kind == "context":
                found[CONTEXT_PLACEHOLDER].add(match.group())
        return {kind: sorted(values) for kind, values in found.items() if values}

    def redact(self, text: str) -> RedactionResult:
        numbering: dict[tuple[str, str], str] = {}
        counters = {SESSION_PLACEHOLDER: 0, CONTEXT_PLACEHOLDER: 0, URL_PLACEHOLDER: 0}
        redactions: list[Redaction] = []
        parts: list[str] = []
        cursor = 0
        out_length = 0

        for match in self._pattern.finditer(text):
            kind = {"url": URL_PLACEHOLDER, "session": SESSION_PLACEHOLDER, "context": CONTEXT_PLACEHOLDER}[match.lastgroup]
            original = match.group()
            key = (kind, _normalize(kind, original))
            placeholder = numbering.get(key)
            if placeholder is None:
                counters[kind] += 1
                placeholder = f"[{kind}-{counters[kind]}]"
                numbering[key] = placeholder
            before = text[cursor:match.start()]
            parts.append(before)
            out_length += len(before)
            redactions.append(Redaction(kind, original, placeholder, out_length, out_length + len(placeholder)))
            parts.append(placeholder)
            out_length += len(placeholder)
            cursor = match.end()
        parts.append(text[cursor:])
        return RedactionResult("".join(parts), redactions)

    def _build_pattern(self, *, include_urls: bool) -> re.Pattern[str]:
        alternatives: list[str] = []
        if include_urls and self.hosts:
            hosts = "|".join(re.escape(host) for host in self.hosts)
            # A link into this Bridge, with or without a scheme. It goes first so an ID
            # inside the link disappears together with the link.
            alternatives.append(
                rf"(?P<url>(?:https?://)?(?<![{_ID_CHAR}.-])(?:{hosts})(?::\d+)?(?:/{_URL_TAIL})?)"
            )
        session_literals = _literal_alternatives(self.session_ids)
        session_parts = [SESSION_ID_SHAPE]
        if session_literals:
            session_parts.insert(0, session_literals)
        alternatives.append(
            rf"(?P<session>(?<![{_ID_CHAR}])(?:{'|'.join(session_parts)})(?![{_ID_CHAR}]))"
        )
        context_parts = [CONTEXT_ID_SHAPE]
        context_literals = _literal_alternatives(self.context_ids)
        if context_literals:
            context_parts.insert(0, context_literals)
        alternatives.append(
            rf"(?P<context>(?<![{_ID_CHAR}_])(?:{'|'.join(context_parts)})(?![{_ID_CHAR}]))"
        )
        return re.compile("|".join(alternatives), re.IGNORECASE)


def _literal_ids(values: Iterable[str]) -> list[str]:
    ids = {value.strip() for value in values if value and len(value.strip()) >= MIN_LITERAL_ID_LENGTH}
    return sorted(ids, key=len, reverse=True)


def _literal_alternatives(ids: list[str]) -> str:
    """Each ID as written and percent-encoded, longest first so no ID shadows a longer one."""
    forms: set[str] = set()
    for value in ids:
        forms.add(value)
        forms.add(quote(value, safe=""))
    return "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))


def _host(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower()


def _normalize_session(value: str) -> str:
    return value.lower()


def _normalize(kind: str, value: str) -> str:
    if kind == URL_PLACEHOLDER:
        return re.sub(r"^https?://", "", value.lower()).rstrip("/")
    return value.lower()
