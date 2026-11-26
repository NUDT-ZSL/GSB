"""Business errors with stable, machine-readable codes."""

from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """An expected validation or state conflict error.

    The CLI converts this exception to a stable JSON response.  Callers should
    never catch it and continue a write transaction.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def fail(code: str, message: str, **details: Any) -> "None":
    raise BusinessError(code, message, details=details)


# Validation errors are grouped by the input object they describe.  Keeping
# the codes stable is important because callers use them as an API.
INVALID_JSON = "INVALID_JSON"
INVALID_JSONL = "INVALID_JSONL"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
INVALID_JOB_ID = "INVALID_JOB_ID"
DUPLICATE_JOB_ID = "DUPLICATE_JOB_ID"
INVALID_OP_KIND = "INVALID_OP_KIND"
INVALID_DTYPE = "INVALID_DTYPE"
INVALID_LAYOUT = "INVALID_LAYOUT"
INVALID_SHAPE = "INVALID_SHAPE"
SHAPE_OP_MISMATCH = "SHAPE_OP_MISMATCH"
INVALID_SEED = "INVALID_SEED"
INVALID_REVISION = "INVALID_REVISION"
INVALID_CLOCK = "INVALID_CLOCK"
CLOCK_REGRESSION = "CLOCK_REGRESSION"
INVALID_PRIORITY = "INVALID_PRIORITY"
INVALID_DEADLINE = "INVALID_DEADLINE"
INVALID_COST = "INVALID_COST"
INVALID_COST_MODEL = "INVALID_COST_MODEL"
INVALID_PRECISION_FLAG = "INVALID_PRECISION_FLAG"

INVALID_STATS = "INVALID_STATS"
INVALID_CACHE_LAYER = "INVALID_CACHE_LAYER"
INVALID_HIT_RATE = "INVALID_HIT_RATE"
INVALID_THREAD_OCCUPANCY = "INVALID_THREAD_OCCUPANCY"
INVALID_THROUGHPUT = "INVALID_THROUGHPUT"
DUPLICATE_STAT = "DUPLICATE_STAT"
STAT_CONFLICT = "STAT_CONFLICT"

INVALID_PLAN = "INVALID_PLAN"
INVALID_PLAN_VERSION = "INVALID_PLAN_VERSION"
DUPLICATE_PLAN_VERSION = "DUPLICATE_PLAN_VERSION"
PLAN_VERSION_CONFLICT = "PLAN_VERSION_CONFLICT"
INVALID_CANDIDATE = "INVALID_CANDIDATE"
DUPLICATE_CANDIDATE = "DUPLICATE_CANDIDATE"
UNKNOWN_JOB = "UNKNOWN_JOB"
PLAN_CYCLE = "PLAN_CYCLE"
REVISION_CONFLICT = "REVISION_CONFLICT"
STALE_REVISION = "STALE_REVISION"
NO_PLATFORM_STAT = "NO_PLATFORM_STAT"
NO_ACTIVE_PLAN = "NO_ACTIVE_PLAN"
NOT_FOUND = "NOT_FOUND"

BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
RESOURCE_OVERFLOW = "RESOURCE_OVERFLOW"
DEADLOCK = "DEADLOCK"
LATE_RESULT = "LATE_RESULT"
STATE_CONFLICT = "STATE_CONFLICT"
DB_ERROR = "DB_ERROR"

