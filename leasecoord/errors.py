from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """A deterministic, machine-readable business failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def fail(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    raise BusinessError(code, message, details=details)
