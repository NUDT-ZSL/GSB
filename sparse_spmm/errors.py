"""Business error types used by the SpMM validation and report pipeline."""

from __future__ import annotations

from typing import Any


class SpMMError(Exception):
    """An expected, reportable error with a stable business code."""

    business_code = "E_INTERNAL"

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        shape: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.shape = shape or {}
        self.details = details or {}

    def to_failure(self, *, injection_id: str | None = None) -> dict[str, Any]:
        failure: dict[str, Any] = {
            "business_code": self.business_code,
            "exception_type": type(self).__name__,
            "stage": self.stage,
            "shape": self.shape,
            "message": self.message,
            "details": self.details,
        }
        if injection_id is not None:
            failure["injection_id"] = injection_id
        return failure


class CSRValidationError(SpMMError):
    business_code = "E_CSR_INVALID"


class ShapeMismatchError(SpMMError):
    business_code = "E_SHAPE_MISMATCH"


class NonFiniteInputError(SpMMError):
    business_code = "E_NONFINITE_INPUT"


class BlockParameterError(SpMMError):
    business_code = "E_BLOCK_INVALID"


class ToleranceError(SpMMError):
    business_code = "E_TOLERANCE_INVALID"


class StrategyError(SpMMError):
    business_code = "E_STRATEGY_INVALID"


class ResumeError(SpMMError):
    business_code = "E_RESUME_INVALID"


class ConfigError(SpMMError):
    business_code = "E_CONFIG_INVALID"


class AtomicWriteError(SpMMError):
    business_code = "E_ATOMIC_WRITE"


class InjectionMissError(SpMMError):
    business_code = "E_INJECTION_MISS"
