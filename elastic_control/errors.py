from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INPUT_JSON = "E_INPUT_JSON"
    INPUT_SCHEMA = "E_INPUT_SCHEMA"
    DUPLICATE_SERVICE = "E_DUPLICATE_SERVICE"
    DUPLICATE_NODE = "E_DUPLICATE_NODE"
    INVALID_SERVICE_NAME = "E_INVALID_SERVICE_NAME"
    INVALID_NODE_NAME = "E_INVALID_NODE_NAME"
    INVALID_MODEL_VERSION = "E_INVALID_MODEL_VERSION"
    INVALID_MEMORY = "E_INVALID_MEMORY"
    INVALID_COMPUTE = "E_INVALID_COMPUTE"
    INVALID_REPLICA_BOUNDS = "E_INVALID_REPLICA_BOUNDS"
    INVALID_COLD_START = "E_INVALID_COLD_START"
    INVALID_BATCH_WINDOW = "E_INVALID_BATCH_WINDOW"
    INVALID_THROUGHPUT = "E_INVALID_THROUGHPUT"
    INVALID_CONCURRENCY_LIMIT = "E_INVALID_CONCURRENCY_LIMIT"
    INVALID_TOPOLOGY = "E_INVALID_TOPOLOGY"
    INVALID_GPU_COUNT = "E_INVALID_GPU_COUNT"
    INVALID_GPU_MEMORY = "E_INVALID_GPU_MEMORY"
    INVALID_NODE_CAPACITY = "E_INVALID_NODE_CAPACITY"
    INVALID_COMPUTE_CAPACITY = "E_INVALID_COMPUTE_CAPACITY"
    INVALID_TRACE = "E_INVALID_TRACE"
    INVALID_TRACE_TICK = "E_INVALID_TRACE_TICK"
    INVALID_ARRIVAL_RATE = "E_INVALID_ARRIVAL_RATE"
    INVALID_SEQUENCE_LENGTH = "E_INVALID_SEQUENCE_LENGTH"

    TRACE_WINDOW_MISSING = "E_TRACE_WINDOW_MISSING"
    UNKNOWN_SERVICE = "E_UNKNOWN_SERVICE"
    CLOCK_REGRESSION = "E_CLOCK_REGRESSION"
    ROUND_CONFLICT = "E_ROUND_CONFLICT"

    LAUNCH_CONNECTION_REFUSED = "E_LAUNCH_CONNECTION_REFUSED"
    LAUNCH_TIMEOUT = "E_LAUNCH_TIMEOUT"
    REPLICA_VERSION_MISMATCH = "E_REPLICA_VERSION_MISMATCH"
    SAMPLE_DIGEST_MISMATCH = "E_SAMPLE_DIGEST_MISMATCH"
    LAUNCH_HTTP_ERROR = "E_LAUNCH_HTTP_ERROR"
    LAUNCH_REMOTE_ERROR = "E_LAUNCH_REMOTE_ERROR"
    LAUNCH_BAD_RESPONSE = "E_LAUNCH_BAD_RESPONSE"
    LAUNCH_INTERNAL = "E_LAUNCH_INTERNAL"

    STATE_CORRUPT = "E_STATE_CORRUPT"
    STATE_BUSY = "E_STATE_BUSY"


class BusinessError(Exception):
    """A deterministic, serializable business failure."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = ErrorCode(code)
        self.message = message
        self.details = details or {}
        super().__init__(f"{self.code}: {message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            },
        }

