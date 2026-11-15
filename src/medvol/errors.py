"""Stable business error reporting for integration with UI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BusinessCode(str, Enum):
    OK = "OK"
    E_VOLUME_SHAPE = "E_VOLUME_SHAPE"
    E_TF_RANGE = "E_TF_RANGE"
    E_STEP_TOO_SMALL = "E_STEP_TOO_SMALL"
    E_RAY_DIR = "E_RAY_DIR"
    E_ERROR_BOUND = "E_ERROR_BOUND"
    E_GL_CONTEXT = "E_GL_CONTEXT"
    E_BUFFER_SIZE = "E_BUFFER_SIZE"


@dataclass(frozen=True)
class ErrorInfo:
    code: BusinessCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }


class VolumeRenderError(Exception):
    """An input or device error that maps to a documented business code."""

    def __init__(
        self,
        code: BusinessCode | str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = BusinessCode(code)
        self.message = message
        self.details = details or {}
        super().__init__(f"{self.code.value}: {message}")

    def to_dict(self) -> dict[str, Any]:
        return error_envelope(self.code, self.message, self.details)


def error_envelope(
    code: BusinessCode | str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    business_code = BusinessCode(code)
    return {
        "schema_version": "1.0.0",
        "ok": False,
        "status": business_code.value,
        "business_code": business_code.value,
        "error": ErrorInfo(business_code, message, details or {}).to_dict(),
    }
