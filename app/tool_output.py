from __future__ import annotations

from typing import Any


TOOL_OUTPUT_MODE_SETTING = "mcp.tool_output_mode"
TOOL_OUTPUT_RESTART_PENDING_SETTING = "mcp.tool_output_restart_pending_mode"
TOOL_OUTPUT_MODE_OPTIMIZED = "optimized"
TOOL_OUTPUT_MODE_MAXIMUM_COMPATIBILITY = "maximum_compatibility"
DEFAULT_TOOL_OUTPUT_MODE = TOOL_OUTPUT_MODE_OPTIMIZED
TOOL_OUTPUT_MODES = {
    TOOL_OUTPUT_MODE_OPTIMIZED,
    TOOL_OUTPUT_MODE_MAXIMUM_COMPATIBILITY,
}


def validate_tool_output_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in TOOL_OUTPUT_MODES:
        allowed = ", ".join(sorted(TOOL_OUTPUT_MODES))
        raise ValueError(f"mode must be one of: {allowed}")
    return value


def configured_tool_output_mode(store: Any) -> str:
    stored = store.get_app_setting(TOOL_OUTPUT_MODE_SETTING)
    if stored is None:
        return DEFAULT_TOOL_OUTPUT_MODE
    try:
        return validate_tool_output_mode(stored)
    except ValueError:
        return DEFAULT_TOOL_OUTPUT_MODE


def large_tool_structured_output(mode: str) -> bool:
    return validate_tool_output_mode(mode) == TOOL_OUTPUT_MODE_MAXIMUM_COMPATIBILITY
