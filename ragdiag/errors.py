"""Business errors shared by all workbench layers."""

from __future__ import annotations

from typing import Any


E_OK = "E_OK"
E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_CONCURRENCY_BUDGET = "E_CONCURRENCY_BUDGET"
E_TIME_BUDGET = "E_TIME_BUDGET"
E_MEMORY_BUDGET = "E_MEMORY_BUDGET"
E_BLOCK_MISMATCH = "E_BLOCK_MISMATCH"
E_INCONSISTENT = "E_INCONSISTENT"
E_SNAPSHOT_STALE = "E_SNAPSHOT_STALE"


class WorkbenchError(Exception):
    """A stable, serializable business error."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def error_dict(code: str, message: str, **details: Any) -> dict[str, Any]:
    return WorkbenchError(code, message, **details).to_dict()

