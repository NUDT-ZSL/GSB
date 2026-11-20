from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    INVALID_JSON = "INVALID_JSON"
    DUPLICATE_JSON_KEY = "DUPLICATE_JSON_KEY"
    INVALID_WORKLOAD = "INVALID_WORKLOAD"
    DUPLICATE_DATASET = "DUPLICATE_DATASET"
    DUPLICATE_JOB = "DUPLICATE_JOB"
    UNKNOWN_DATASET = "UNKNOWN_DATASET"
    UNKNOWN_JOB = "UNKNOWN_JOB"
    INVALID_NAME = "INVALID_NAME"
    INVALID_SAMPLE_COUNT = "INVALID_SAMPLE_COUNT"
    INVALID_FEATURE_DIM = "INVALID_FEATURE_DIM"
    INVALID_SAMPLE_BYTES = "INVALID_SAMPLE_BYTES"
    INVALID_SHARD_SIZE = "INVALID_SHARD_SIZE"
    SHARD_SIZE_NOT_DIVISIBLE = "SHARD_SIZE_NOT_DIVISIBLE"
    INVALID_SHARD_INDEX = "INVALID_SHARD_INDEX"
    INVALID_BATCH_SIZE = "INVALID_BATCH_SIZE"
    INVALID_EPOCHS = "INVALID_EPOCHS"
    INVALID_DATA_PARALLELISM = "INVALID_DATA_PARALLELISM"
    INVALID_PREFETCH_DEPTH = "INVALID_PREFETCH_DEPTH"
    INVALID_BOOLEAN = "INVALID_BOOLEAN"
    INVALID_CACHE_GENERATION = "INVALID_CACHE_GENERATION"
    CACHE_GENERATION_REGRESSION = "CACHE_GENERATION_REGRESSION"
    CACHE_GENERATION_NOT_MONOTONIC = "CACHE_GENERATION_NOT_MONOTONIC"
    CACHE_BYTES_MISMATCH = "CACHE_BYTES_MISMATCH"
    CACHE_DIGEST_MISMATCH = "CACHE_DIGEST_MISMATCH"
    DUPLICATE_CACHE_ENTRY = "DUPLICATE_CACHE_ENTRY"
    INVALID_CONFIG = "INVALID_CONFIG"
    INVALID_GLOBAL_SEED = "INVALID_GLOBAL_SEED"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    RUN_BUSY = "RUN_BUSY"
    RUN_CONFLICT = "RUN_CONFLICT"
    NO_WORKLOAD = "NO_WORKLOAD"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    STATE_CORRUPT = "STATE_CORRUPT"
    CACHE_FILE_CORRUPT = "CACHE_FILE_CORRUPT"
    VERIFY_FAILED = "VERIFY_FAILED"
    SCHEDULER_FAILED = "SCHEDULER_FAILED"
    INVALID_STATE_DIR = "INVALID_STATE_DIR"


class BusinessError(Exception):
    """A stable, machine-readable business failure.

    Errors are raised before persistent mutations whenever validation is
    possible.  The CLI serializes this exception without a traceback.
    """

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.message = message
        self.details = details or {}
        super().__init__(f"{self.code}: {message}")

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "error": {"code": self.code, "message": self.message},
        }
        if self.details:
            result["error"]["details"] = self.details
        return result
