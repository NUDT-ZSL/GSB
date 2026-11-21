from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """A stable business error that is safe to expose to CLI users."""

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
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload


def fail(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    raise BusinessError(code, message, details)

