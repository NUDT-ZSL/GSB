from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_PRECISION_UNKNOWN = "E_PRECISION_UNKNOWN"
E_DUPLICATE_SAMPLE_ID = "E_DUPLICATE_SAMPLE_ID"
E_FEATURE_DIM = "E_FEATURE_DIM"
E_SAMPLE_NOT_FOUND = "E_SAMPLE_NOT_FOUND"
E_LATENCY_BUDGET = "E_LATENCY_BUDGET"
E_INCONSISTENT = "E_INCONSISTENT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_INPUT_INVALID = "E_INPUT_INVALID"


@dataclass
class BusinessError(Exception):
    """A stable, machine-readable business error."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def fail(code: str, message: str, **details: Any) -> None:
    raise BusinessError(code=code, message=message, details=details)
