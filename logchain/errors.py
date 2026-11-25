from __future__ import annotations

from typing import Any


class ChainError(Exception):
    """Business error with a stable machine-readable code."""

    exit_code = 2

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class ValidationError(ChainError):
    pass


class ConflictError(ChainError):
    exit_code = 3


class RecoveryError(ChainError):
    exit_code = 4
