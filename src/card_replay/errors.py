from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    SEQ_GAP = "E_SEQ_GAP"
    RNG_DRIFT = "E_RNG_DRIFT"
    TRIGGER_DEPTH = "E_TRIGGER_DEPTH"
    RESOURCE_OVERFLOW = "E_RESOURCE_OVERFLOW"
    BUDGET_DENIED = "E_BUDGET_DENIED"
    MALFORMED_EVENT = "E_MALFORMED_EVENT"
    COST_MISMATCH = "E_COST_MISMATCH"
    CONFIG_INVALID = "E_CONFIG_INVALID"


@dataclass(frozen=True)
class Violation:
    code: ErrorCode
    message: str
    seq: int | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
        }
        if self.seq is not None:
            result["seq"] = self.seq
        if self.context:
            result["context"] = self.context
        return result


class ConfigError(ValueError):
    """Raised for an invalid rules configuration."""

    def __init__(self, message: str, *, context: dict[str, Any] | None = None):
        super().__init__(message)
        self.violation = Violation(
            ErrorCode.CONFIG_INVALID,
            message,
            context=context or {},
        )


class EventRejected(Exception):
    """Internal signal that a top-level event must be rejected."""

    def __init__(self, violation: Violation):
        super().__init__(violation.message)
        self.violation = violation


class TriggerChainRejected(Exception):
    """Internal signal that the whole trigger chain must roll back."""

    def __init__(self, violation: Violation):
        super().__init__(violation.message)
        self.violation = violation
