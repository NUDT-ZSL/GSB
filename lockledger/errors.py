"""Business error codes with stable process exit semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class LedgerError(Exception):
    code: str
    message: str
    path: str | None = None
    exit_code: int = 2
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        location = f" at {self.path}" if self.path else ""
        return f"{self.code}{location}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.path:
            result["path"] = self.path
        if self.details:
            result["details"] = self.details
        return result


def fail(
    code: str,
    message: str,
    path: str | None = None,
    *,
    exit_code: int = 2,
    details: dict[str, Any] | None = None,
) -> None:
    raise LedgerError(code, message, path, exit_code, details)

