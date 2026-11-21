"""Structured business errors used across pipeline stages."""

from __future__ import annotations

from typing import Any


E_SHAPE_INVALID = "E_SHAPE_INVALID"
E_PARAM_INVALID = "E_PARAM_INVALID"
E_OP_UNSUPPORTED = "E_OP_UNSUPPORTED"
E_DTYPE_UNSUPPORTED = "E_DTYPE_UNSUPPORTED"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_NUMERIC_NONFINITE = "E_NUMERIC_NONFINITE"
E_NUMERIC_MISMATCH = "E_NUMERIC_MISMATCH"
E_INTERNAL_UNATTRIBUTED = "E_INTERNAL_UNATTRIBUTED"


class DiagnosticError(Exception):
    """A failure attributed to a concrete pipeline stage."""

    def __init__(
        self,
        code: str,
        stage: str,
        job_id: str,
        field: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.job_id = job_id
        self.field = field
        self.message = message
        self.details = details or {}

    def as_record(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "job_id": self.job_id,
            "field": self.field,
            "message": self.message,
            "details": self.details,
        }
