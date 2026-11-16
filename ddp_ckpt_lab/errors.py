"""Business errors with stable machine-readable codes."""

from __future__ import annotations

from typing import Any


BUSINESS_CODES = (
    "E_INIT",
    "E_CHECKPOINT",
    "E_RESTORE",
    "E_CONFIG",
    "E_DIVERGE",
    "E_REPLAY",
)


class BusinessError(Exception):
    """An expected failure that must be surfaced with a stable business code."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        if code not in BUSINESS_CODES:
            raise ValueError(f"unknown business code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
