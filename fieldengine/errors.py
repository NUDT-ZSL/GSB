from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    INVALID_ARGUMENT = "E_INVALID_ARGUMENT"
    FILE_NOT_FOUND = "E_FILE_NOT_FOUND"
    INVALID_JSON = "E_INVALID_JSON"
    DUPLICATE_JSON_KEY = "E_DUPLICATE_JSON_KEY"
    BATCH_SEQUENCE = "E_BATCH_SEQUENCE"
    BATCH_SCHEMA = "E_BATCH_SCHEMA"
    COLUMN_DUPLICATE = "E_COLUMN_DUPLICATE"
    COLUMN_LENGTH = "E_COLUMN_LENGTH"
    COLUMN_TYPE = "E_COLUMN_TYPE"
    ROW_COUNT = "E_ROW_COUNT"
    BATCH_DIGEST = "E_BATCH_DIGEST"
    QUERY_REFERENCE = "E_QUERY_REFERENCE"
    QUERY_TYPE = "E_QUERY_TYPE"
    QUERY_AGGREGATE = "E_QUERY_AGGREGATE"
    QUERY_FILTER = "E_QUERY_FILTER"
    WINDOW_ARGUMENT = "E_WINDOW_ARGUMENT"
    CONFIG_PARALLELISM = "E_CONFIG_PARALLELISM"
    CONFIG_MEMORY = "E_CONFIG_MEMORY"
    CONFIG_FIXED_SCALE = "E_CONFIG_FIXED_SCALE"
    CONFIG_TOLERANCE = "E_CONFIG_TOLERANCE"
    CONFIG_CLOCK = "E_CONFIG_CLOCK"
    CONFIG_SHARDS = "E_CONFIG_SHARDS"
    CONFIG_ADAPTIVE = "E_CONFIG_ADAPTIVE"
    INJECTION = "E_INJECTION"
    STATE_NOT_FOUND = "E_STATE_NOT_FOUND"
    STATE_MISMATCH = "E_STATE_MISMATCH"
    RUN_EXISTS_INCOMPLETE = "E_RUN_EXISTS_INCOMPLETE"
    RUN_INCOMPLETE = "E_RUN_INCOMPLETE"
    RUN_ABANDONED = "E_RUN_ABANDONED"
    SHARD_FAILED = "E_SHARD_FAILED"
    IDEMPOTENCY_CONFLICT = "E_IDEMPOTENCY_CONFLICT"
    LEASE_CONFLICT = "E_LEASE_CONFLICT"
    BUDGET_REJECTED = "E_BUDGET_REJECTED"
    FIXED_POINT_OVERFLOW = "E_FIXED_POINT_OVERFLOW"
    VERIFY_FAILED = "E_VERIFY_FAILED"
    INTERNAL = "E_INTERNAL"


@dataclass
class EngineError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 2

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def fail(code: str, message: str, **details: Any) -> EngineError:
    return EngineError(code=code, message=message, details=details)
