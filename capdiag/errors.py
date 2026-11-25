from __future__ import annotations

from typing import Any


class ErrorCode:
    SCHEMA = "E_SCHEMA_VALIDATION"
    DUPLICATE_SERVICE = "E_DUPLICATE_SERVICE"
    DUPLICATE_INSTANCE = "E_DUPLICATE_INSTANCE"
    TRACE_SEQUENCE = "E_TRACE_SEQUENCE"
    TRACE_CLOCK = "E_TRACE_CLOCK"
    TRACE_FIELD = "E_TRACE_FIELD"
    METRIC_SCALE = "E_METRIC_SCALE"
    WINDOW_CONFIG = "E_WINDOW_CONFIG"
    THRESHOLD_REFERENCE = "E_THRESHOLD_REFERENCE"
    V1_EFFECTIVE_SECONDS = "E_V1_EFFECTIVE_SECONDS"
    V2_HASH = "E_V2_SHARD_HASH"
    V2_GENERATION = "E_V2_GENERATION"
    VERSION_ROLLBACK = "E_VERSION_ROLLBACK"
    PRECISION_BUDGET = "E_PRECISION_BUDGET"
    CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
    LOCKED = "E_LOCKED"
    CRASH_INJECTED = "E_CRASH_INJECTED"


class CapDiagError(Exception):
    """Business error carrying a stable, machine-readable error code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        record_index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.record_index = record_index

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error_code": self.code,
            "error": self.message,
            "details": self.details,
        }
        if self.record_index is not None:
            payload["record_index"] = self.record_index
        return payload
