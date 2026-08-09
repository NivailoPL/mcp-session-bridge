from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


try:
    BRIDGE_VERSION = version("mcp-session-bridge")
except PackageNotFoundError:
    BRIDGE_VERSION = "0.4.1"
