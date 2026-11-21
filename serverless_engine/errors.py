"""JSON business errors used by the CLI and library."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class EngineError(Exception):
    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.path is not None:
            error["path"] = self.path
        if self.details is not None:
            error["details"] = self.details
        return {"ok": False, "error": error}

    def __str__(self) -> str:
        suffix = f" ({self.path})" if self.path else ""
        return f"{self.code}: {self.message}{suffix}"


def bad(code: str, message: str, path: str | None = None, **details: Any) -> EngineError:
    return EngineError(code, message, path, details or None)
