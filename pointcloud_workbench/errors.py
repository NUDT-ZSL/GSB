"""Business errors and stable process exit codes."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_VALIDATION = 2
EXIT_DIGEST = 3
EXIT_INTERRUPTED = 130


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


@dataclass
class BusinessError(Exception):
    """A deterministic, user-facing error with an E_xxx business code."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = EXIT_VALIDATION

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": _json_safe(self.details),
            },
            "exit_code": self.exit_code,
        }


class InterruptedRun(Exception):
    """Raised only at a completed block boundary after a stop request."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.exit_code = EXIT_INTERRUPTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "interrupted": True,
            "error": {
                "code": "E_INTERRUPTED",
                "message": self.message,
                "details": _json_safe(self.details),
            },
            "exit_code": self.exit_code,
        }


def validation_error(
    code: str,
    message: str,
    **details: Any,
) -> BusinessError:
    return BusinessError(code=code, message=message, details=details)


def digest_error(message: str, **details: Any) -> BusinessError:
    return BusinessError(
        code="E_DIGEST",
        message=message,
        details=details,
        exit_code=EXIT_DIGEST,
    )

