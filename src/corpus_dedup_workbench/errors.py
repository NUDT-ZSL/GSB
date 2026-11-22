from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

E_SIG_LEN_INVALID = "E_SIG_LEN_INVALID"
E_DOC_TOO_SHORT = "E_DOC_TOO_SHORT"
E_LATENCY_BUDGET = "E_LATENCY_BUDGET"
E_INCONSISTENT = "E_INCONSISTENT"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_DOC_NOT_FOUND = "E_DOC_NOT_FOUND"
E_DATA_INVALID = "E_DATA_INVALID"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    stage: str = "unknown"
    details: dict[str, Any] = field(default_factory=dict)
    completed_stages: list[dict[str, Any]] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "stage": self.stage,
                "completed_stages": self.completed_stages,
                "details": self.details,
            },
        }


def error_envelope(error: WorkbenchError) -> dict[str, Any]:
    return error.to_dict()

