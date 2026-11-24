"""Exception type carrying a stable business code."""

from __future__ import annotations

from typing import Any

from .codes import Code


class AuditError(Exception):
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class ArgumentAuditError(AuditError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(Code.E_ARGS, message, details)
