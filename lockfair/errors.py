"""Business errors with stable codes and diagnostic fields."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    # Input validation and request shape
    INVALID_INPUT = "INVALID_INPUT"
    DUPLICATE_RESOURCE_KEY = "DUPLICATE_RESOURCE_KEY"
    DUPLICATE_APPLICATION_SEQ = "DUPLICATE_APPLICATION_SEQ"
    HOLDER_NOT_FOUND = "HOLDER_NOT_FOUND"
    LOCK_NOT_FOUND = "LOCK_NOT_FOUND"
    INVALID_LEASE_DURATION = "INVALID_LEASE_DURATION"
    INVALID_LEASE_GENERATION = "INVALID_LEASE_GENERATION"
    INVALID_CONCURRENCY_LIMIT = "INVALID_CONCURRENCY_LIMIT"
    INVALID_ROUND_BUDGET = "INVALID_ROUND_BUDGET"
    INVALID_FAIRNESS_LABEL = "INVALID_FAIRNESS_LABEL"
    INVALID_STARVATION_THRESHOLD = "INVALID_STARVATION_THRESHOLD"
    INVALID_CONFIG = "INVALID_CONFIG"
    DUPLICATE_HOLDER_IDENTITY = "DUPLICATE_HOLDER_IDENTITY"
    APPLICATION_NOT_QUEUED = "APPLICATION_NOT_QUEUED"

    # Scheduling and concurrency
    STALE_LEASE_GENERATION = "STALE_LEASE_GENERATION"
    BUDGET_CANNOT_SATISFY_ANY = "BUDGET_CANNOT_SATISFY_ANY"
    FAIR_QUOTA_UNSATISFIABLE = "FAIR_QUOTA_UNSATISFIABLE"
    CLOCK_ROLLBACK_EXCEEDED = "CLOCK_ROLLBACK_EXCEEDED"
    MAX_WAIT_EXCEEDED = "MAX_WAIT_EXCEEDED"
    ROUND_OUT_OF_ORDER = "ROUND_OUT_OF_ORDER"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    STATE_NOT_INITIALIZED = "STATE_NOT_INITIALIZED"
    STATE_ALREADY_INITIALIZED = "STATE_ALREADY_INITIALIZED"


@dataclass(slots=True)
class BusinessError(Exception):
    code: ErrorCode
    message: str
    round_no: int | None = None
    lock_key: str | None = None
    application_seq: int | None = None
    constraint: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "round_no": self.round_no,
                "lock_key": self.lock_key,
                "application_seq": self.application_seq,
                "constraint": self.constraint,
                "details": self.details,
            },
        }


def error(
    code: ErrorCode,
    message: str,
    *,
    round_no: int | None = None,
    lock_key: str | None = None,
    application_seq: int | None = None,
    constraint: str | None = None,
    details: dict[str, Any] | None = None,
) -> BusinessError:
    return BusinessError(
        code=code,
        message=message,
        round_no=round_no,
        lock_key=lock_key,
        application_seq=application_seq,
        constraint=constraint,
        details=details or {},
    )
