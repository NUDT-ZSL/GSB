"""Stable business error codes."""

from __future__ import annotations

from typing import Any


class EngineError(Exception):
    """An expected business error with a stable code and machine-readable detail."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_code": self.code,
            "message": self.message,
            "details": self.details,
        }


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_STREAM_BATCH_MISMATCH = "E_STREAM_BATCH_MISMATCH"
E_WINDOW_RECOUNT = "E_WINDOW_RECOUNT"
E_SPEAKER_LABEL_INVALID = "E_SPEAKER_LABEL_INVALID"
E_SEGMENT_BOUNDARY = "E_SEGMENT_BOUNDARY"
E_CHUNK_ORDER = "E_CHUNK_ORDER"
E_CHUNK_GAP = "E_CHUNK_GAP"
E_CHUNK_RANGE = "E_CHUNK_RANGE"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
