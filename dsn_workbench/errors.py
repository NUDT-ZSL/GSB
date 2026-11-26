from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_PARALLEL_MISMATCH = "E_PARALLEL_MISMATCH"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_COVERAGE_MISMATCH = "E_COVERAGE_MISMATCH"
E_TOKEN_OVERLAP = "E_TOKEN_OVERLAP"
E_TAG_INVALID = "E_TAG_INVALID"
E_CHECKPOINT_CORRUPT = "E_CHECKPOINT_CORRUPT"
E_STATE_TRANSITION = "E_STATE_TRANSITION"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_TARGET_EXISTS = "E_TARGET_EXISTS"


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def error_dict(code: str, message: str, **details: Any) -> dict[str, Any]:
    return WorkbenchError(code, message, details).to_dict()

