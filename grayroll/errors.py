from __future__ import annotations

from typing import Any


class GrayrollError(Exception):
    """A deterministic, machine-readable business error."""

    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "ERROR",
            "code": self.code,
            "message": self.message,
        }
        if self.details is not None:
            result["details"] = self.details
        return result


EXIT_CODES = {
    "E_BAD_RHYTHM": 2,
    "E_BAD_SHARD": 2,
    "E_BAD_MANIFEST": 2,
    "E_BAD_DEFAULTS": 2,
    "E_BAD_EVENT": 2,
    "E_MERGE_CONFLICT": 3,
    "E_CLOCK_REGRESSION": 4,
    "E_VERSION_ROLLBACK": 5,
    "E_PACKAGE_CONFLICT": 5,
    "E_PACKAGE_INCOMPATIBLE": 5,
    "E_NO_ACTIVE_PACKAGE": 6,
    "E_NO_STATE": 6,
    "E_NOT_FOUND": 6,
    "E_INVALID_STATE": 7,
    "E_EVENT_BATCH_STATE": 7,
    "E_VERIFY_FAILED": 8,
}


def exit_code_for(code: str) -> int:
    return EXIT_CODES.get(code, 1)
