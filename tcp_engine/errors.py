from __future__ import annotations

from typing import Any


class EngineError(Exception):
    """A business error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


E_BAD_JSON = "E_BAD_JSON"
E_BAD_LINK = "E_BAD_LINK"
E_BAD_SCRIPT = "E_BAD_SCRIPT"
E_BAD_CONGESTION = "E_BAD_CONGESTION"
E_BAD_OBSERVATION = "E_BAD_OBSERVATION"
E_BAD_SEGMENT = "E_BAD_SEGMENT"
E_BAD_TRANSITION = "E_BAD_TRANSITION"
E_BAD_ACK = "E_BAD_ACK"
E_TIMEOUT_IDLE = "E_TIMEOUT_IDLE"
E_TIMEOUT_NOT_DUE = "E_TIMEOUT_NOT_DUE"
E_RETRY_EXHAUSTED = "E_RETRY_EXHAUSTED"
E_CLOCK_REWIND = "E_CLOCK_REWIND"
E_DIGEST_MISMATCH = "E_DIGEST_MISMATCH"
E_TRACE_INCOMPLETE = "E_TRACE_INCOMPLETE"
E_TRACE_CORRUPT = "E_TRACE_CORRUPT"
E_CONFLICT = "E_CONFLICT"
E_RUN_NOT_FOUND = "E_RUN_NOT_FOUND"
E_RUN_INCOMPLETE = "E_RUN_INCOMPLETE"
E_NOTHING_TO_RESUME = "E_NOTHING_TO_RESUME"
E_INCONSISTENCY = "E_INCONSISTENCY"
E_DB = "E_DB"

