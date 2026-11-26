from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    INPUT_NOT_FOUND = "CAP-1000-INPUT-NOT-FOUND"
    VALIDATION_SCENARIO = "CAP-1001-VALIDATION-SCENARIO"
    VALIDATION_TRACE = "CAP-1002-VALIDATION-TRACE"
    VALIDATION_METRIC_DEFINITION = "CAP-1003-VALIDATION-METRIC-DEFINITION"
    VALIDATION_V1 = "CAP-1004-VALIDATION-THRESHOLD-V1"
    VALIDATION_V2_SHARD_HASH = "CAP-1005-VALIDATION-V2-SHARD-HASH"
    VALIDATION_V2_GENERATION = "CAP-1006-VALIDATION-V2-GENERATION-MONOTONIC"
    VALIDATION_V2_METADATA = "CAP-1007-VALIDATION-V2-METADATA"
    VALIDATION_PATCH = "CAP-1008-VALIDATION-PATCH"
    PRECISION_BUDGET_EXCEEDED = "CAP-2001-PRECISION-BUDGET-EXCEEDED"
    LOCK_BUSY = "CAP-3001-LOCK-BUSY"
    CRASH_INJECTED = "CAP-9001-CRASH-INJECTED"
    INTERNAL = "CAP-9999-INTERNAL"


@dataclass
class CapacityError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "details": self.details,
        }
