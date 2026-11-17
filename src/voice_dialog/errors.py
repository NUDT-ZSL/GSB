"""Business errors that can be isolated to one segment or turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BusinessError(Exception):
    code: str
    phase: str
    message: str
    filename: str = ""
    turn_id: str = ""
    exception_type: str = "BusinessError"
    injection_id: str = ""
    detail: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.code} [{self.phase}] {self.message}"

    def to_failure(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "phase": self.phase,
            "filename": self.filename,
            "turn_id": self.turn_id,
            "exception_type": self.exception_type,
            "message": self.message,
            "injection_id": self.injection_id,
        }
        if self.detail:
            out["detail"] = self.detail
        return out


class ConfigError(BusinessError):
    def __init__(self, message: str, *, code: str = "E_INVALID_CONFIG", injection_id: str = ""):
        super().__init__(
            code=code,
            phase="config",
            message=message,
            exception_type=type(self).__name__,
            injection_id=injection_id,
        )


class GracefulStop(Exception):
    """Raised at a deterministic batch boundary after a stop request."""
