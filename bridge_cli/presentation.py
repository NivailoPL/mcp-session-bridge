from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TextIO


INDYGO = "\x1b[38;2;99;102;241m"
RESET = "\x1b[0m"
BOLD = "\x1b[1m"


@dataclass(frozen=True)
class TerminalTheme:
    enabled: bool

    @classmethod
    def detect(cls, stream: TextIO | None = None, *, force_plain: bool = False) -> "TerminalTheme":
        target = stream or sys.stdout
        is_tty = bool(getattr(target, "isatty", lambda: False)())
        disabled = force_plain or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb"
        return cls(enabled=is_tty and not disabled)

    def heading(self, value: str) -> str:
        if not self.enabled:
            return value
        return f"{INDYGO}{BOLD}{value}{RESET}"
