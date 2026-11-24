from __future__ import annotations

from typing import Any


class AnimRecalcError(Exception):
    """Business error with a stable ``E_xxx`` code."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


class DigestMismatchError(AnimRecalcError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("E_DIGEST", message, details)


class InterruptedAtChunk(Exception):
    """Raised internally when a handled stop is requested at a chunk boundary."""

    def __init__(self, completed_chunks: int | None = None):
        super().__init__("graceful interruption at chunk boundary")
        self.completed_chunks = completed_chunks


EXIT_OK = 0
EXIT_ERROR = 2
EXIT_INTERRUPTED = 75
