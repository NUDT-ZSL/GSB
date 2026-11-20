"""Business error types with stable error codes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(eq=False)
class ControlPlaneError(Exception):
    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        if self.path:
            return f"{self.code}: {self.message} (at {self.path})"
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.details:
            payload["details"] = self.details
        return payload


class VerifyFailure(ControlPlaneError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("E_VERIFY_FAILED", message, details=details)
