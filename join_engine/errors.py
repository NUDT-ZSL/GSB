from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EngineError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 2

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload


def fail(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    exit_code: int = 2,
) -> None:
    raise EngineError(code, message, details or {}, exit_code)

