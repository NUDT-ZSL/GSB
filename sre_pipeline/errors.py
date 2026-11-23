"""Business errors and stable error codes."""

from __future__ import annotations

from typing import Any, Dict, Optional


class PipelineError(Exception):
    """An error carrying a stable business code and diagnostic fields."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "error_code": self.code,
            "message": self.message,
        }
        if self.stage is not None:
            payload["stage"] = self.stage
        if self.details:
            payload["details"] = self.details
        return payload


E_BAD_JSON = "E_BAD_JSON"
E_BAD_TOPOLOGY = "E_BAD_TOPOLOGY"
E_BAD_POLICY = "E_BAD_POLICY"
E_BAD_EVENT = "E_BAD_EVENT"
E_BAD_ANOMALY = "E_BAD_ANOMALY"

E_POLICY_VERSION_CONFLICT = "E_POLICY_VERSION_CONFLICT"
E_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
E_WINDOW_DIGEST_MISMATCH = "E_WINDOW_DIGEST_MISMATCH"
E_HALF_OPEN_PROBE_OVERFLOW = "E_HALF_OPEN_PROBE_OVERFLOW"
E_DEGRADATION_ILLEGAL_REGRESSION = "E_DEGRADATION_ILLEGAL_REGRESSION"

E_SAMPLE_CONFLICT = "E_SAMPLE_CONFLICT"
E_STORAGE_STATE = "E_STORAGE_STATE"
E_NOT_LOADED = "E_NOT_LOADED"
E_OUT_OF_ORDER = "E_OUT_OF_ORDER"
E_BAD_ARGUMENT = "E_BAD_ARGUMENT"
E_INTERNAL = "E_INTERNAL"

RUNTIME_ANOMALY_CODES = {
    ("parse", "policy_version_conflict"): E_POLICY_VERSION_CONFLICT,
    ("rate_limit", "clock_rollback_over_tolerance"): E_CLOCK_ROLLBACK,
    ("rate_limit", "token_refill_clock_rollback"): E_CLOCK_ROLLBACK,
    ("circuit", "window_digest_mismatch"): E_WINDOW_DIGEST_MISMATCH,
    ("circuit", "half_open_probe_overflow"): E_HALF_OPEN_PROBE_OVERFLOW,
    ("degradation", "illegal_degradation_regression"): E_DEGRADATION_ILLEGAL_REGRESSION,
}

