"""Business errors with stable codes and diagnostic payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


E_MANIFEST_MISSING = "E_MANIFEST_MISSING"
E_JSON_MALFORMED = "E_JSON_MALFORMED"
E_BAD_SCHEMA_DECL = "E_BAD_SCHEMA_DECL"
E_BAD_INDEX_DECL = "E_BAD_INDEX_DECL"
E_BAD_OP = "E_BAD_OP"
E_DUP_PK = "E_DUP_PK"
E_UNIQUE_VIOLATION = "E_UNIQUE_VIOLATION"
E_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
E_LEDGER_ROLLBACK = "E_LEDGER_ROLLBACK"
E_SPLIT_HEIGHT_OVERFLOW = "E_SPLIT_HEIGHT_OVERFLOW"
E_BROKEN_PARENT_POINTER = "E_BROKEN_PARENT_POINTER"
E_INDEX_DIGEST_MISMATCH = "E_INDEX_DIGEST_MISMATCH"
E_NOT_INITIALIZED = "E_NOT_INITIALIZED"
E_SAMPLE_MISMATCH = "E_SAMPLE_MISMATCH"
E_OP_CONFLICT = "E_OP_CONFLICT"
E_LOCK_TIMEOUT = "E_LOCK_TIMEOUT"
E_RANGE_WAIT_TIMEOUT = "E_RANGE_WAIT_TIMEOUT"
E_INCONSISTENT_STATE = "E_INCONSISTENT_STATE"
E_UNFINISHED_ATTEMPT = "E_UNFINISHED_ATTEMPT"
E_IO = "E_IO"
