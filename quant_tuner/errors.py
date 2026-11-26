from __future__ import annotations

from typing import Any


E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_BUDGET_EXCEEDED = "E_BUDGET_EXCEEDED"
E_PRECISION_TRADE = "E_PRECISION_TRADE"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_INCONSISTENT = "E_INCONSISTENT"
E_NOT_FOUND = "E_NOT_FOUND"

KNOWN_CODES = {
    E_VERSION_UNSUPPORTED,
    E_FORMAT_CORRUPT,
    E_BUDGET_EXCEEDED,
    E_PRECISION_TRADE,
    E_STAGE_BUSY,
    E_INCONSISTENT,
    E_NOT_FOUND,
}


class TuningError(Exception):
    """Structured engine error with one of the stable E_* codes."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        if code not in KNOWN_CODES:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }
