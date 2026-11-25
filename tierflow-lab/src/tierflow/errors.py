from __future__ import annotations

from typing import Any


class TierFlowError(Exception):
    code = "E_TIERFLOW"
    numeric_code = 1000

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "numeric_code": self.numeric_code,
            "error": self.message,
            "details": self.details,
        }


class ValidationError(TierFlowError):
    code = "E_VALIDATION"
    numeric_code = 1001


class ClockRollbackError(TierFlowError):
    code = "E_CLOCK_ROLLBACK"
    numeric_code = 2001


class PolicyRollbackError(TierFlowError):
    code = "E_POLICY_ROLLBACK"
    numeric_code = 3001


class PolicyIntegrityError(TierFlowError):
    code = "E_POLICY_INTEGRITY"
    numeric_code = 3002


class StateConflictError(TierFlowError):
    code = "E_STATE_CONFLICT"
    numeric_code = 4001


class CapacityExceededError(TierFlowError):
    code = "E_CAPACITY_EXCEEDED"
    numeric_code = 4002


class VerifyError(TierFlowError):
    code = "E_VERIFY_FAILED"
    numeric_code = 5001
