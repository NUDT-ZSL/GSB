from __future__ import annotations

from typing import Any


class AppError(Exception):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def fail(code: str, message: str, details: Any | None = None) -> None:
    raise AppError(code, message, details)
