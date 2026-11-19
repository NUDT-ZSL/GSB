"""Stable business errors exposed by the library and CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    INVALID_SCHEMA = "INVALID_SCHEMA"
    DUPLICATE_COLUMN = "DUPLICATE_COLUMN"
    INVALID_COLUMN_TYPE = "INVALID_COLUMN_TYPE"
    INVALID_TABLE = "INVALID_TABLE"
    INVALID_TXN = "INVALID_TXN"
    INVALID_CLOCK = "INVALID_CLOCK"
    TXN_START_ORDER = "TXN_START_ORDER"
    INVALID_ISOLATION = "INVALID_ISOLATION"
    INVALID_OPERATION = "INVALID_OPERATION"
    INVALID_VALUE = "INVALID_VALUE"
    INVALID_CONFIG = "INVALID_CONFIG"
    UNKNOWN_TRANSACTION = "UNKNOWN_TRANSACTION"
    TRANSACTION_NOT_STARTED = "TRANSACTION_NOT_STARTED"
    TRANSACTION_CLOSED = "TRANSACTION_CLOSED"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    UNKNOWN_READ_VIEW = "UNKNOWN_READ_VIEW"
    VERSION_DIGEST_MISMATCH = "VERSION_DIGEST_MISMATCH"
    SNAPSHOT_DIGEST_MISMATCH = "SNAPSHOT_DIGEST_MISMATCH"
    CLOCK_REGRESSION = "CLOCK_REGRESSION"
    QUERY_LATENCY_BUDGET_EXCEEDED = "QUERY_LATENCY_BUDGET_EXCEEDED"
    ROW_NOT_FOUND = "ROW_NOT_FOUND"
    ROW_ALREADY_EXISTS = "ROW_ALREADY_EXISTS"
    WRITE_CONFLICT = "WRITE_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DATABASE_NOT_INITIALIZED = "DATABASE_NOT_INITIALIZED"
    DATABASE_EXISTS = "DATABASE_EXISTS"
    INVALID_STATE = "INVALID_STATE"
    RECOVERY_CONFLICT = "RECOVERY_CONFLICT"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 2

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.code}: {self.message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }

