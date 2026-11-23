from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    http_status: int = 400
    exit_code: int = 2

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def bad_snapshot(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError("E_BAD_SNAPSHOT", message, details)


def hash_mismatch(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError("E_HASH_MISMATCH", message, details)


def bad_plan(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError("E_BAD_PLAN", message, details)
