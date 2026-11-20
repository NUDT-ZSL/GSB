from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


E_BAD_SAMPLE = "E_BAD_SAMPLE"
E_BAD_STAT = "E_BAD_STAT"
E_DUP_TEMPLATE = "E_DUP_TEMPLATE"
E_SQL_UNNORMALIZABLE = "E_SQL_UNNORMALIZABLE"
E_SAMPLE_NOT_MONOTONIC = "E_SAMPLE_NOT_MONOTONIC"
E_SAMPLE_CONFLICT = "E_SAMPLE_CONFLICT"
E_STAT_VERSION_CONFLICT = "E_STAT_VERSION_CONFLICT"
E_STAT_VERSION_ROLLBACK = "E_STAT_VERSION_ROLLBACK"
E_STAT_EFFECTIVE_ROLLBACK = "E_STAT_EFFECTIVE_ROLLBACK"
E_UNKNOWN_STAT_COLUMN = "E_UNKNOWN_STAT_COLUMN"
E_UNKNOWN_STAT_INDEX = "E_UNKNOWN_STAT_INDEX"
E_DB_DIGEST_MISMATCH = "E_DB_DIGEST_MISMATCH"
E_ZERO_ESTIMATE = "E_ZERO_ESTIMATE"
E_MISSING_STAT = "E_MISSING_STAT"
E_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
E_NOT_INITIALIZED = "E_NOT_INITIALIZED"
E_ALREADY_INITIALIZED = "E_ALREADY_INITIALIZED"
E_IO = "E_IO"
E_VERIFY_FAILED = "E_VERIFY_FAILED"
E_OP_IN_PROGRESS = "E_OP_IN_PROGRESS"


EXIT_CODES: dict[str, int] = {
    E_IO: 1,
    E_BAD_SAMPLE: 2,
    E_BAD_STAT: 3,
    E_DUP_TEMPLATE: 4,
    E_SQL_UNNORMALIZABLE: 5,
    E_STAT_VERSION_CONFLICT: 6,
    E_STAT_VERSION_ROLLBACK: 7,
    E_UNKNOWN_STAT_COLUMN: 8,
    E_DB_DIGEST_MISMATCH: 9,
    E_ZERO_ESTIMATE: 10,
    E_CLOCK_ROLLBACK: 11,
    E_NOT_INITIALIZED: 12,
    E_SAMPLE_CONFLICT: 13,
    E_SAMPLE_NOT_MONOTONIC: 14,
    E_STAT_EFFECTIVE_ROLLBACK: 15,
    E_UNKNOWN_STAT_INDEX: 16,
    E_MISSING_STAT: 17,
    E_ALREADY_INITIALIZED: 18,
    E_VERIFY_FAILED: 19,
    E_OP_IN_PROGRESS: 20,
}


def exit_code_for(code: str) -> int:
    return EXIT_CODES.get(code, 1)
