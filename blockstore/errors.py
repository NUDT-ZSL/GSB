"""Stable business errors used by the library and CLI."""

from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """An expected, user-facing failure with a stable machine-readable code."""

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
        result: dict[str, Any] = {
            "ok": False,
            "error": {"code": self.code, "message": self.message},
        }
        if self.details:
            result["error"]["details"] = self.details
        return result
