"""Stable business error codes and structured exceptions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
    FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
    SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
    ROUNDTRIP_MISMATCH = "E_ROUNDTRIP_MISMATCH"
    HOMOGRAPHY_SINGULAR = "E_HOMOGRAPHY_SINGULAR"
    GAIN_INVALID = "E_GAIN_INVALID"
    FEATHER_INVALID = "E_FEATHER_INVALID"
    OVERLAP_GAP = "E_OVERLAP_GAP"
    INVALID_FILLED = "E_INVALID_FILLED"
    WEIGHT_SUM = "E_WEIGHT_SUM"
    SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    STAGE_BUSY = "E_STAGE_BUSY"
    INCONSISTENT = "E_INCONSISTENT"
    UNATTRIBUTED = "E_UNATTRIBUTED"


@dataclass
class BusinessError(Exception):
    """A failure carrying a stable code, stage, and field-level location."""

    code: str
    message: str
    stage: str = "unknown"
    field_path: str | None = None
    line: int | None = None
    column: int | None = None
    char_offset: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        location = ""
        if self.field_path:
            location += f" field={self.field_path}"
        if self.line is not None:
            location += f" line={self.line}"
        if self.column is not None:
            location += f" column={self.column}"
        if self.char_offset is not None:
            location += f" offset={self.char_offset}"
        return f"{self.code}: {self.message}{location}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "field_path": self.field_path,
            "line": self.line,
            "column": self.column,
            "char_offset": self.char_offset,
            "details": self.details,
        }


def format_error(
    message: str,
    *,
    field_path: str,
    line: int | None = None,
    column: int | None = None,
    char_offset: int | None = None,
    details: dict[str, Any] | None = None,
) -> BusinessError:
    return BusinessError(
        code=ErrorCode.FORMAT_CORRUPT,
        message=message,
        stage="parse",
        field_path=field_path,
        line=line,
        column=column,
        char_offset=char_offset,
        details=details or {},
    )

