"""Stable business errors used by the scheduler API and CLI."""

from __future__ import annotations

from typing import Any, Dict, Optional


class SchedulerError(Exception):
    """Base error with a stable machine-readable code and diagnostics."""

    error_code = "E_SCHEDULER"
    exit_code = 40

    def __init__(
        self,
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if error_code:
            self.error_code = error_code
        if exit_code is not None:
            self.exit_code = exit_code

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.error_code,
            "message": self.message,
            "details": _json_safe(self.details),
        }


class ValidationError(SchedulerError):
    error_code = "E_VALIDATION_FAILED"
    exit_code = 20


class NotFoundError(SchedulerError):
    error_code = "E_NOT_FOUND"
    exit_code = 21


class ConflictError(SchedulerError):
    error_code = "E_CONFLICT"
    exit_code = 49


class InjectedFault(SchedulerError):
    """Controlled fault from inject.json.

    Each fault type has its own stable business code.
    """

    exit_code = 58


FAULT_ERROR_CODES = {
    "interval_overlap": "E_INJECT_INTERVAL_OVERLAP",
    "ledger_imbalance": "E_INJECT_LEDGER_IMBALANCE",
    "generation_rollback": "E_INJECT_GENERATION_ROLLBACK",
    "reorg_digest_mismatch": "E_INJECT_REORG_DIGEST_MISMATCH",
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)
