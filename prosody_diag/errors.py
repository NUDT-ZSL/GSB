from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any


@dataclass
class DiagnosticError(Exception):
    """Structured business error with a stable machine-readable code."""

    code: str
    message: str
    field: str | None = None
    details: dict[str, Any] = dc_field(default_factory=dict)

    def __str__(self) -> str:
        suffix = f" [{self.code}]"
        if self.field:
            suffix += f" field={self.field}"
        return f"{self.message}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "error": self.message,
        }
        if self.field is not None:
            payload["field"] = self.field
        if self.details:
            payload["details"] = self.details
        return payload


E_FRAME_COUNT_MISMATCH = "E_FRAME_COUNT_MISMATCH"
E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_SEMANTIC_LOSS = "E_SEMANTIC_LOSS"
E_CONFLICT_OVERFLOW = "E_CONFLICT_OVERFLOW"
E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_DUPLICATE_UTTERANCE_ID = "E_DUPLICATE_UTTERANCE_ID"
E_INVALID_FRAME_CONFIG = "E_INVALID_FRAME_CONFIG"
E_PRECISION_PROFILE_UNKNOWN = "E_PRECISION_PROFILE_UNKNOWN"
E_REFERENCE_MISMATCH = "E_REFERENCE_MISMATCH"
E_TREE_VERSION_ROLLBACK = "E_TREE_VERSION_ROLLBACK"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_LEDGER_MISMATCH = "E_LEDGER_MISMATCH"
E_STATE_CORRUPT = "E_STATE_CORRUPT"
E_ROUNDING_FLIP = "E_ROUNDING_FLIP"
