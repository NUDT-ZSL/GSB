from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """An expected, machine-readable business error."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise BusinessError(code, message, details)

