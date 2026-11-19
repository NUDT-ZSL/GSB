"""Stable business error codes and serializable exceptions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    TOPOLOGY_DUPLICATE_SERVICE = "FG-1001"
    TOPOLOGY_UNKNOWN_DEPENDENCY = "FG-1002"
    EVENT_SEQ_NOT_INCREASING = "FG-1003"
    EVENT_CLOCK_INVALID = "FG-1004"
    EVENT_RESULT_INVALID = "FG-1005"
    EVENT_LATENCY_INVALID = "FG-1006"
    EVENT_FIELD_INVALID = "FG-1007"
    TOPOLOGY_INVALID = "FG-1008"
    EVENT_SCHEMA_INVALID = "FG-1009"
    POLICY_QPS_INVALID = "FG-1101"
    POLICY_BUCKET_INVALID = "FG-1102"
    POLICY_WINDOW_INVALID = "FG-1103"
    POLICY_RATIO_INVALID = "FG-1104"
    POLICY_INVALID = "FG-1105"
    CLOCK_ROLLBACK = "FG-2001"
    ZERO_VALID_REQUESTS = "FG-2002"
    SAMPLE_DIGEST_MISMATCH = "FG-2003"
    MODE_SWITCH_FAILED = "FG-2004"
    EVENT_SEQ_CONFLICT = "FG-2005"
    STATE_INCONSISTENT = "FG-2006"
    DB_BUSY = "FG-3001"
    NOT_FOUND = "FG-3002"


@dataclass
class FlowGuardError(Exception):
    """A business error with a stable code and diagnostic fields."""

    code: str
    message: str
    fields: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 3

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "fields": self.fields,
        }


def validation_error(
    code: str, message: str, **fields: Any
) -> FlowGuardError:
    return FlowGuardError(code=code, message=message, fields=fields, exit_code=2)
