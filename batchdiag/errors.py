from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


def _safe_detail(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _safe_detail(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_detail(item) for item in value]
    return value


@dataclass
class BusinessError(Exception):
    """An expected, machine-readable business failure."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 2

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "details": _safe_detail(self.details),
        }


class StateMismatchError(BusinessError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code="E_STATE_MISMATCH",
            message=message,
            details=details or {},
            exit_code=3,
        )
