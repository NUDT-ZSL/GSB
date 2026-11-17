"""Business error types shared by the CLI and processing pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BusinessError(Exception):
    """An expected, user-facing failure with a stable E_xxx code."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 2

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
            "exit_code": self.exit_code,
        }


def require(condition: bool, code: str, message: str, **details: Any) -> None:
    """Raise a BusinessError when *condition* is false."""

    if not condition:
        raise BusinessError(code, message, details)
