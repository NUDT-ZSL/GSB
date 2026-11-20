from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KernelError(Exception):
    """A stable, machine-readable business error."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def fail(code: str, message: str, **details: Any) -> None:
    raise KernelError(code=code, message=message, details=details)

