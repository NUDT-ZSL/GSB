"""Stable business errors used by the lab."""

from __future__ import annotations

from typing import Any


E_PARSE = "E_PARSE"
E_DATA = "E_DATA"
E_STATS = "E_STATS"
E_VERSION = "E_VERSION"
E_SNAPSHOT = "E_SNAPSHOT"
E_SKEW = "E_SKEW"
E_REPLAY = "E_REPLAY"


class LabError(Exception):
    """A user-facing error with a stable business code."""

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
