from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


CHECK_STATES = frozenset(
    {"pass", "action_required", "waiting", "failed", "not_checked", "skipped"}
)
UPDATE_STATES = frozenset({"current", "available", "unknown"})
OVERALL_STATES = frozenset({"healthy", "attention", "failed", "incomplete", "unknown"})


@dataclass(frozen=True)
class CheckResult:
    id: str
    label: str
    state: str
    message: str
    remediation: str | None = None

    def __post_init__(self) -> None:
        if self.state not in CHECK_STATES:
            raise ValueError(f"Unknown check state: {self.state}")

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True)
class UpdateStatus:
    state: str
    current: str
    latest: str | None = None
    last_checked: str | None = None
    release_url: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.state not in UPDATE_STATES:
            raise ValueError(f"Unknown update state: {self.state}")

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True)
class StatusReport:
    overall: str
    version: dict[str, Any]
    update: UpdateStatus
    checks: list[CheckResult]
    installation: dict[str, Any]
    last_operation: dict[str, Any] | None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.overall not in OVERALL_STATES:
            raise ValueError(f"Unknown overall state: {self.overall}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "overall": self.overall,
            "version": self.version,
            "update": self.update.to_dict(),
            "checks": [check.to_dict() for check in self.checks],
            "installation": self.installation,
            "last_operation": self.last_operation,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)
