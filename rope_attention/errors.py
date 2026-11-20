"""Stable business error codes used by the component and CLI."""

from __future__ import annotations

from typing import Any


E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_CACHE_CONFLICT = "E_CACHE_CONFLICT"
E_INCONSISTENT = "E_INCONSISTENT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_CONFIG_CASE_MISMATCH = "E_CONFIG_CASE_MISMATCH"
E_SNAPSHOT_CONFLICT = "E_SNAPSHOT_CONFLICT"
E_VERSION_STALE = "E_VERSION_STALE"
E_INPUT_CORRUPT = "E_INPUT_CORRUPT"

ALL_ERROR_CODES = frozenset(
    {
        E_VERSION_UNSUPPORTED,
        E_FORMAT_CORRUPT,
        E_SEMANTIC_DRIFT,
        E_TOLERANCE_EXCEEDED,
        E_CACHE_CORRUPT,
        E_CACHE_CONFLICT,
        E_INCONSISTENT,
        E_RESOURCE_LIMIT,
        E_CONFIG_CASE_MISMATCH,
        E_SNAPSHOT_CONFLICT,
        E_VERSION_STALE,
        E_INPUT_CORRUPT,
    }
)


class RopeError(Exception):
    """An error with a stable machine-readable code and auditable details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ALL_ERROR_CODES:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "business_code": self.code,
            "message": self.message,
            "details": self.details,
        }

