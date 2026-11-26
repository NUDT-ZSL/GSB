from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
E_PENDING_SLOT_MISSING = "E_PENDING_SLOT_MISSING"
E_CONFIDENCE_ABOVE_THRESHOLD = "E_CONFIDENCE_ABOVE_THRESHOLD"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_DUPLICATE_SESSION = "E_DUPLICATE_SESSION"
E_SLOT_KEY_INVALID = "E_SLOT_KEY_INVALID"
E_CONFIDENCE_OUT_OF_RANGE = "E_CONFIDENCE_OUT_OF_RANGE"
E_CONFIG_INVALID = "E_CONFIG_INVALID"


@dataclass
class EngineError(Exception):
    code: str
    message: str
    field: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def __str__(self) -> str:
        location = f" ({self.field})" if self.field else ""
        return f"{self.code}{location}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            error["field"] = self.field
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


def fail(
    code: str,
    message: str,
    *,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    raise EngineError(code, message, field=field, details=details or {})

