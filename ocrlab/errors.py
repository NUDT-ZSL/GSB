"""Business errors shared by the pipeline, API, and UI."""

from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """A deterministic, serializable domain error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | int | None = None,
        page_id: str | None = None,
        line_no: int | None = None,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.page_id = page_id
        self.line_no = line_no
        self.field = field
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.stage is not None:
            result["stage"] = self.stage
        if self.page_id is not None:
            result["page_id"] = self.page_id
        if self.line_no is not None:
            result["line_no"] = self.line_no
        if self.field is not None:
            result["field"] = self.field
        if self.details:
            result["details"] = self.details
        return result


class SimulatedCrash(RuntimeError):
    """Raised by the recovery demo after a committed stage."""

    def __init__(self, committed_stage: int) -> None:
        super().__init__(f"simulated process termination after stage {committed_stage}")
        self.committed_stage = committed_stage


E_DUPLICATE_PAGE_ID = "E_DUPLICATE_PAGE_ID"
E_IMAGE_SHAPE_MISMATCH = "E_IMAGE_SHAPE_MISMATCH"
E_UNKNOWN_CHARSET_VERSION = "E_UNKNOWN_CHARSET_VERSION"
E_INVALID_LINE_BOX = "E_INVALID_LINE_BOX"
E_INVALID_PIXEL = "E_INVALID_PIXEL"
E_PIXEL_OUT_OF_RANGE = "E_PIXEL_OUT_OF_RANGE"
E_TEMPLATE_SHAPE_MISMATCH = "E_TEMPLATE_SHAPE_MISMATCH"
E_INVALID_THRESHOLD = "E_INVALID_THRESHOLD"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
E_INCONSISTENT = "E_INCONSISTENT"
E_UNATTRIBUTED = "E_UNATTRIBUTED"
E_INVALID_CONFIG = "E_INVALID_CONFIG"
E_INVALID_MANIFEST = "E_INVALID_MANIFEST"
E_INVALID_TEXT = "E_INVALID_TEXT"
E_INVALID_POLICY = "E_INVALID_POLICY"
E_NO_LINES_DETECTED = "E_NO_LINES_DETECTED"
E_CHAR_SEGMENTATION_FAILED = "E_CHAR_SEGMENTATION_FAILED"
E_INJECTION_INVALID = "E_INJECTION_INVALID"
E_INJECTION_NOT_APPLIED = "E_INJECTION_NOT_APPLIED"
E_NOT_FOUND = "E_NOT_FOUND"
E_INTERNAL = "E_INTERNAL"

