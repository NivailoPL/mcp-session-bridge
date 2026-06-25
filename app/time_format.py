from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DISPLAY_TIMEZONE_NAME = "UTC"
DISPLAY_TIMEZONE_NAME = DEFAULT_DISPLAY_TIMEZONE_NAME
DISPLAY_TIMEZONE_SETTING_KEY = "display_timezone"

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


def resolve_timezone_name(timezone_name: str | None) -> str:
    resolved = (timezone_name or DEFAULT_DISPLAY_TIMEZONE_NAME).strip() or DEFAULT_DISPLAY_TIMEZONE_NAME
    try:
        ZoneInfo(resolved)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {resolved}") from exc
    return resolved


def format_response_timestamp(timestamp: int, timezone_name: str | None = None) -> str:
    local = datetime.fromtimestamp(timestamp, tz=ZoneInfo(resolve_timezone_name(timezone_name)))
    weekday = _WEEKDAYS[local.weekday()]
    month = _MONTHS[local.month - 1]
    return f"{local:%H:%M} ({weekday}, {month} {local.day}, {local.year})"
