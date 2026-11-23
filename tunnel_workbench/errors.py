from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    http_status: int = 400

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def fail(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    http_status: int = 400,
) -> None:
    raise BusinessError(code, message, details or {}, http_status)

