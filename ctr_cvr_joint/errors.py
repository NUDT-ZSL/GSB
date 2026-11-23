"""Stable business error codes used by the command line and tests."""

from __future__ import annotations

from typing import Any


E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_INVALID = "E_FORMAT_INVALID"
E_DUP_REQUEST = "E_DUP_REQUEST"
E_THRESHOLD_INVALID = "E_THRESHOLD_INVALID"
E_TOLERANCE_INVALID = "E_TOLERANCE_INVALID"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_INVALID_ARGUMENT = "E_INVALID_ARGUMENT"

E_CODES = {
    E_VERSION_UNSUPPORTED,
    E_FORMAT_INVALID,
    E_DUP_REQUEST,
    E_THRESHOLD_INVALID,
    E_TOLERANCE_INVALID,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_ROLLBACK,
    E_CACHE_CORRUPT,
    E_STAGE_BUSY,
    E_LINEAGE_BROKEN,
    E_INVALID_ARGUMENT,
}


class BusinessError(Exception):
    """An expected, machine-readable domain error.

    ``line`` and ``field`` are promoted to the top level because format errors
    must identify both the input line and the offending field.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        line: int | None = None,
        field: str | None = None,
        **details: Any,
    ) -> None:
        if code not in E_CODES:
            raise ValueError(f"unknown business code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line
        self.field = field
        self.details: dict[str, Any] = dict(details)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.line is not None:
            result["line"] = self.line
        if self.field is not None:
            result["field"] = self.field
        if self.details:
            result["details"] = self.details
        return result

    def __str__(self) -> str:
        location = ""
        if self.line is not None:
            location += f" line={self.line}"
        if self.field is not None:
            location += f" field={self.field}"
        return f"{self.code}{location}: {self.message}"


def fail(code: str, message: str, **kwargs: Any) -> None:
    raise BusinessError(code, message, **kwargs)

