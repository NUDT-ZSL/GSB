from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class Code:
    JSON_INPUT = "ARB-VAL-001"
    CARD_DUPLICATE = "ARB-VAL-002"
    CARD_COMPUTE = "ARB-VAL-003"
    CARD_MEMORY = "ARB-VAL-004"
    CARD_GRANULARITY = "ARB-VAL-005"
    CARD_DIVISIBILITY = "ARB-VAL-006"
    CARD_HEALTH = "ARB-VAL-007"
    JOB_DUPLICATE = "ARB-VAL-008"
    JOB_DEMAND = "ARB-VAL-009"
    JOB_DURATION = "ARB-VAL-010"
    JOB_PRIORITY = "ARB-VAL-011"
    JOB_QUOTA_GROUP = "ARB-VAL-012"
    JOB_TOLERANCE = "ARB-VAL-013"
    JOB_SINGLE_CARD = "ARB-VAL-014"
    CONFIG_FIELD = "ARB-VAL-015"
    CLOCK_STEPS = "ARB-VAL-016"
    ANOMALY_ENTRY = "ARB-VAL-017"
    REFERENCE = "ARB-VAL-018"
    ALIGNMENT_DIVISIBILITY = "ARB-VAL-019"
    GRACE_NOT_LESS_THAN_DURATION = "ARB-VAL-020"

    CLOCK_ROLLBACK = "ARB-RT-001"
    ROUND_RANGE = "ARB-RT-002"
    IDEMPOTENCY_CONFLICT = "ARB-RT-003"
    LEASE_NOT_FOUND = "ARB-RT-004"
    RENEW_IDENTITY = "ARB-RT-005"
    RENEW_GENERATION = "ARB-RT-006"
    RENEW_EXPIRED = "ARB-RT-007"
    JOB_NOT_WAITING = "ARB-RT-008"
    PREEMPT_NOT_ALLOWED = "ARB-RT-009"
    DB_NOT_LOADED = "ARB-RT-010"
    LOCK_TIMEOUT = "ARB-RT-011"
    LEDGER_CONSISTENCY = "ARB-RT-012"
    PROTOCOL = "ARB-RT-013"

    LOCK_INDEX_TIMEOUT = "ARB-LCK-001"

    ANOMALY_INTERVAL_OVERLAP = "ARB-ANOM-001"
    ANOMALY_SHARE_NONCONSERVATION = "ARB-ANOM-002"
    ANOMALY_GENERATION_REGRESSION = "ARB-ANOM-003"
    ANOMALY_FRAGMENT_DIGEST = "ARB-ANOM-004"


@dataclass
class BizError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    round_no: int | None = None
    card_id: str | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "round": self.round_no,
            "card_id": self.card_id,
        }


class ValidationFailure(Exception):
    def __init__(self, errors: list[BizError]):
        super().__init__(f"{len(errors)} validation error(s)")
        self.errors = errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.errors[0].code if self.errors else Code.PROTOCOL,
            "message": "input validation failed",
            "errors": [error.to_dict() for error in self.errors],
        }


def error(code: str, message: str, **details: Any) -> BizError:
    return BizError(code=code, message=message, details=details)
