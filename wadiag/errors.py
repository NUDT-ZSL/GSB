from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EngineError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def error(code: str, message: str, **details: Any) -> EngineError:
    return EngineError(code=code, message=message, details=details)
