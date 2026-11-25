from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class ErrorCode:
    FORMAT_UNSUPPORTED = "E_FORMAT_UNSUPPORTED"
    FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
    GT_SEMANTIC_CONFLICT = "E_GT_SEMANTIC_CONFLICT"
    SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    INCONSISTENT = "E_INCONSISTENT"
    PLAN_INVALID = "E_PLAN_INVALID"
    BUDGET_EXHAUSTED = "E_BUDGET_EXHAUSTED"


@dataclass
class AuditError(Exception):
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }
