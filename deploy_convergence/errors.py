"""Business errors with stable codes and diagnostic details."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_CODES = {
    "E_BAD_MANIFEST",
    "E_BAD_PARAM_TYPE",
    "E_DIGEST_MISMATCH",
    "E_GENERATION_CONFLICT",
    "E_DEPENDENCY_CYCLE",
    "E_DEPENDENCY_MISSING",
    "E_CLOCK_ROLLBACK",
    "E_VERSION_ROLLBACK",
    "E_MIGRATION",
    "E_STATE_CORRUPT",
    "E_IO",
}


@dataclass
class EngineError(Exception):
    code: str
    message: str
    stage: str = "unknown"
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown business error code: {self.code}")
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "stage": self.stage,
                "details": self.details,
            },
        }
