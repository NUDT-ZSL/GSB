from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """A deterministic, user-facing business rule violation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def error(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> BusinessError:
    return BusinessError(code, message, details=details)
