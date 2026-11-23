"""Business errors with stable machine-readable codes."""

from __future__ import annotations

from typing import Any, Mapping


class ControlError(Exception):
    """An expected business error.

    ``code`` is stable and intended for automation.  ``details`` is always a
    JSON-serialisable mapping.  CLI failures are printed as JSON, never as a
    traceback unless ``--debug`` is requested.
    """

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "details": self.details,
        }


def fail(code: str, message: str, **details: Any) -> "None":
    raise ControlError(code, message, details)


# Validation
INVALID_JSON = "INVALID_JSON"
SCHEMA_ERROR = "SCHEMA_ERROR"
DUPLICATE_NAME = "DUPLICATE_NAME"
INVALID_REPLICA_RANGE = "INVALID_REPLICA_RANGE"
INVALID_POSITIVE_INTEGER = "INVALID_POSITIVE_INTEGER"
INVALID_AGGREGATION = "INVALID_AGGREGATION"
INVALID_PERCENTILE = "INVALID_PERCENTILE"
INVALID_THRESHOLD = "INVALID_THRESHOLD"
INVALID_WINDOW = "INVALID_WINDOW"
INVALID_SCALE = "INVALID_SCALE"
INVALID_ERROR_BUDGET = "INVALID_ERROR_BUDGET"
INVALID_RULE = "INVALID_RULE"
INVALID_BUDGET = "INVALID_BUDGET"
MISSING_REFERENCE = "MISSING_REFERENCE"
DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
INVALID_PATCH = "INVALID_PATCH"

# Runtime/algorithm
CLOCK_ROLLBACK = "CLOCK_ROLLBACK"
UNKNOWN_METRIC = "UNKNOWN_METRIC"
UNKNOWN_WORKLOAD = "UNKNOWN_WORKLOAD"
DUPLICATE_SAMPLE_ID = "DUPLICATE_SAMPLE_ID"
INVALID_SAMPLE = "INVALID_SAMPLE"
SAMPLE_BEFORE_WATERMARK = "SAMPLE_BEFORE_WATERMARK"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
FROZEN = "FROZEN"
COOLDOWN = "COOLDOWN"
BACKOFF = "BACKOFF"

# Persistence/concurrency
VERSION_CONFLICT = "VERSION_CONFLICT"
VERSION_ROLLBACK = "VERSION_ROLLBACK"
ALREADY_LOADED = "ALREADY_LOADED"
NOT_LOADED = "NOT_LOADED"
ROUND_CONFLICT = "ROUND_CONFLICT"
ACTION_CONFLICT = "ACTION_CONFLICT"
ACTION_RESULT_CONFLICT = "ACTION_RESULT_CONFLICT"
PERSISTENCE_ERROR = "PERSISTENCE_ERROR"
VERIFY_MISMATCH = "VERIFY_MISMATCH"
LOCK_ERROR = "LOCK_ERROR"
