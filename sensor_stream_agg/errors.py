from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    OK = "OK"
    E_WINDOW_STATE = "E_WINDOW_STATE"
    E_DUPLICATE = "E_DUPLICATE"
    E_LATE_DROP = "E_LATE_DROP"
    E_WINDOW_PARAM = "E_WINDOW_PARAM"
    E_TIME_REGRESS = "E_TIME_REGRESS"
    E_DB_CONFLICT = "E_DB_CONFLICT"
    E_ERRBOUND = "E_ERRBOUND"
    E_INVALID_EVENT = "E_INVALID_EVENT"


class AggregationError(Exception):
    """A domain failure carrying a stable machine-readable error code."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = ErrorCode(code)
        self.message = message
        self.details = details or {}
        super().__init__(f"{self.code.value}: {message}")

