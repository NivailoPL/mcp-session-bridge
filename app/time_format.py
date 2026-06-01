from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

DISPLAY_TIMEZONE_NAME = "UTC"
DISPLAY_TIMEZONE = ZoneInfo(DISPLAY_TIMEZONE_NAME)

_WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def format_timestamp_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def format_response_timestamp(timestamp: int) -> str:
    local = datetime.fromtimestamp(timestamp, tz=DISPLAY_TIMEZONE)
    weekday = _WEEKDAYS[local.weekday()]
    month = _MONTHS[local.month - 1]
    return f"{local:%H:%M} ({weekday}, {month} {local.day}, {local.year})"
