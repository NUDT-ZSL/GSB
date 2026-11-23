"""Business errors used by both the library and the command line interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EngineError(Exception):
    """An expected, machine-readable input or state error."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def fail(code: str, message: str, **details: Any) -> "None":
    raise EngineError(code=code, message=message, details=details)
