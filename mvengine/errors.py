"""Stable business errors used by every public surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EngineError(Exception):
    """A deterministic, machine-readable engine failure."""

    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "context": self.context,
                "message": self.message,
            }
        }


def fail(code: str, message: str, **context: Any) -> "EngineError":
    return EngineError(code=code, message=message, context=context)

