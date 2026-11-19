from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    E_LATENCY_BUDGET = "E_LATENCY_BUDGET"
    E_CHUNK_MISMATCH = "E_CHUNK_MISMATCH"
    E_ANCHOR_MISMATCH = "E_ANCHOR_MISMATCH"
    E_CONFIG_INVALID = "E_CONFIG_INVALID"
    E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    E_STAGE_BUSY = "E_STAGE_BUSY"
    E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    E_NOT_FOUND = "E_NOT_FOUND"
    E_INVALID_STATE = "E_INVALID_STATE"


class BusinessError(Exception):
    """A stable, machine-readable business error."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            },
        }
