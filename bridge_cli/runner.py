from __future__ import annotations

import subprocess
from typing import Protocol


class Runner(Protocol):
    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            check=check,
            capture_output=True,
            text=True,
        )
