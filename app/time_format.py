from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

DISPLAY_TIMEZONE_NAME = "Europe/Warsaw"
DISPLAY_TIMEZONE = ZoneInfo(DISPLAY_TIMEZONE_NAME)

_WEEKDAYS = [
    "poniedziałek",
    "wtorek",
    "środa",
    "czwartek",
    "piątek",
    "sobota",
    "niedziela",
]

_MONTHS_GENITIVE = [
    "stycznia",
    "lutego",
    "marca",
    "kwietnia",
    "maja",
    "czerwca",
    "lipca",
    "sierpnia",
    "września",
    "października",
    "listopada",
    "grudnia",
]


def format_timestamp_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def format_response_timestamp(timestamp: int) -> str:
    local = datetime.fromtimestamp(timestamp, tz=DISPLAY_TIMEZONE)
    weekday = _WEEKDAYS[local.weekday()]
    month = _MONTHS_GENITIVE[local.month - 1]
    return f"{local:%H:%M} ({weekday}, {local.day} {month} {local.year})"
