"""Business errors exposed by the offline gateway tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AquaError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        suffix = ""
        if self.details:
            fields = ", ".join(f"{key}={value}" for key, value in self.details.items())
            suffix = f" ({fields})"
        return f"{self.code}: {self.message}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }

