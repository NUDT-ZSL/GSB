from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
    E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
    E_TIE_POLICY_UNSPECIFIED = "E_TIE_POLICY_UNSPECIFIED"
    E_MASK_PRIORITY_INVALID = "E_MASK_PRIORITY_INVALID"
    E_SEED_UNSPECIFIED = "E_SEED_UNSPECIFIED"
    E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
    E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    E_STAGE_BUSY = "E_STAGE_BUSY"
    E_SNAPSHOT_CHECKSUM_MISMATCH = "E_SNAPSHOT_CHECKSUM_MISMATCH"
    E_SNAPSHOT_CONFLICT = "E_SNAPSHOT_CONFLICT"
    E_DUP_CASE = "E_DUP_CASE"
    E_SHAPE_MISMATCH = "E_SHAPE_MISMATCH"
    E_SEMANTIC_INCOMPLETE = "E_SEMANTIC_INCOMPLETE"
    E_INCONSISTENT = "E_INCONSISTENT"
    E_CONFIG_INVALID = "E_CONFIG_INVALID"
    E_RNG_UNSPECIFIED = "E_RNG_UNSPECIFIED"
    E_IO = "E_IO"


@dataclass
class DiagnosticError(Exception):
    code: ErrorCode
    message: str
    field: str | None = None
    line: int | None = None
    case_id: str | None = None
    stage: str | None = None
    candidate_index: int | None = None
    details: dict[str, Any] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "error_code": self.code.value,
            "message": self.message,
        }
        for name in ("field", "line", "case_id", "stage", "candidate_index"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.details:
            result["details"] = self.details
        return result

