"""Business-level errors with stable machine-readable codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: list[dict[str, Any]] = field(default_factory=list)
    exit_code: int = 2

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.details:
            result["error"]["details"] = self.details
        return result


class ValidationError(BusinessError):
    def __init__(self, details: list[dict[str, Any]]) -> None:
        first = details[0]["code"] if details else "E_VALIDATION"
        super().__init__(
            code=first,
            message="fixture validation failed",
            details=details,
            exit_code=2,
        )


class StateError(BusinessError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: list[dict[str, Any]] | None = None,
        exit_code: int = 3,
    ) -> None:
        super().__init__(code, message, details or [], exit_code)
