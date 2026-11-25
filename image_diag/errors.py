from __future__ import annotations

from typing import Any


E_BLOCK_MISMATCH = "E_BLOCK_MISMATCH"
E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
E_THRESHOLD_INVALID = "E_THRESHOLD_INVALID"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CONFLICT = "E_SNAPSHOT_CONFLICT"
E_SNAPSHOT_CHECKSUM = "E_SNAPSHOT_CHECKSUM"
E_SNAPSHOT_VERSION_AHEAD = "E_SNAPSHOT_VERSION_AHEAD"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SAMPLE_INVALID = "E_SAMPLE_INVALID"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_CHECKPOINT_INVALID = "E_CHECKPOINT_INVALID"
E_NOT_FOUND = "E_NOT_FOUND"
E_BAD_REQUEST = "E_BAD_REQUEST"


class BusinessError(Exception):
    """An expected business failure with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }
