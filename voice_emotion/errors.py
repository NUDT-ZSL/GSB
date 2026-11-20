"""Structured business errors used across the component."""

from __future__ import annotations

from typing import Any


E_SAMPLE_RATE = "E_SAMPLE_RATE"
E_FRAME_CONFIG = "E_FRAME_CONFIG"
E_FRAME_COUNT = "E_FRAME_COUNT"
E_DUPLICATE_DIALOG = "E_DUPLICATE_DIALOG"
E_ENROLLMENT_MISSING = "E_ENROLLMENT_MISSING"
E_SPEAKER_UNKNOWN = "E_SPEAKER_UNKNOWN"
E_INTERVAL_INVALID = "E_INTERVAL_INVALID"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_CACHE_BUSY = "E_CACHE_BUSY"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_INCONSISTENT = "E_INCONSISTENT"
E_INPUT_INVALID = "E_INPUT_INVALID"


class BusinessError(RuntimeError):
    """An expected, machine-readable failure.

    ``details`` deliberately contains only JSON-compatible values so callers can
    persist and forward it without inspecting a traceback.
    """

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:
        if not self.details:
            return f"{self.code}: {self.message}"
        return f"{self.code}: {self.message} ({self.details})"


def raise_error(code: str, message: str, **details: Any) -> None:
    raise BusinessError(code, message, **details)
