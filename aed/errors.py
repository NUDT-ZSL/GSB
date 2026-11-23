"""Structured business error codes for the AED pipeline."""

from __future__ import annotations


class Code:
    # node-level
    E_FRAME_MISMATCH = "E_FRAME_MISMATCH"
    E_EVENT_TYPE_INVALID = "E_EVENT_TYPE_INVALID"
    E_OVERLAP = "E_OVERLAP"
    E_OUT_OF_RANGE = "E_OUT_OF_RANGE"
    # traceability
    E_STALE_RESULT = "E_STALE_RESULT"
    # snapshot / cache
    E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
    E_STAGE_BUSY = "E_STAGE_BUSY"
    # validation
    E_SAMPLE_RATE_INVALID = "E_SAMPLE_RATE_INVALID"
    E_HOP_EXCEEDS_FRAME = "E_HOP_EXCEEDS_FRAME"
    E_DUPLICATE_DIALOG = "E_DUPLICATE_DIALOG"
    E_TEMPLATE_VERSION_UNKNOWN = "E_TEMPLATE_VERSION_UNKNOWN"
    # resource limits
    E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"


class AEDError(Exception):
    """Business error carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"ok": False, "code": self.code, "message": self.message,
                "details": self.details}
