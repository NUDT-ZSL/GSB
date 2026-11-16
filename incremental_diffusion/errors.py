"""Business errors used at the CLI and diagnostic-report boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_PATCH_OUT_OF_RANGE = "E_PATCH_OUT_OF_RANGE"
E_NOISE_MISMATCH = "E_NOISE_MISMATCH"
E_SNAPSHOT_VERSION_CONFLICT = "E_SNAPSHOT_VERSION_CONFLICT"
E_STAGE_FAILURE = "E_STAGE_FAILURE"


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    stage: str | None = None
    exception_type: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.stage is not None:
            value["stage"] = self.stage
        if self.exception_type is not None:
            value["exception_type"] = self.exception_type
        if self.details:
            value["details"] = self.details
        return value


class GracefulInterrupt(Exception):
    """Raised only after the active stage has been committed."""

    def __init__(self, signal_name: str) -> None:
        super().__init__(signal_name)
        self.signal_name = signal_name

