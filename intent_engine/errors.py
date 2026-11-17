"""Typed errors used by the component and the command line interface."""

from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """An expected, reportable input or model error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.details = details or {}


class ConfigMismatchError(BusinessError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            "E_CONFIG_MISMATCH",
            message,
            stage="startup",
            details=details or {},
        )


class NonFiniteModelWeightError(ValueError):
    """Raised when an injected or loaded model weight is NaN or infinite."""

