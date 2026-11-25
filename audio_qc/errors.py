"""Typed error carrying a traceable business code."""

from __future__ import annotations

from typing import Any

from .codes import BusinessCode


class QCError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
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


def inconsistent(first_mismatch: dict[str, Any], message: str = "cross-module result mismatch") -> QCError:
    return QCError(
        BusinessCode.E_INCONSISTENT,
        message,
        {"first_mismatch": first_mismatch},
    )
