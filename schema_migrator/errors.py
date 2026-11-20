from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None
    exit_code: int = 2

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details or {},
            },
        }


def error(code: str, message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(code=code, message=message, details=details)

