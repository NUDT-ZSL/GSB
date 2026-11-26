from __future__ import annotations

from dataclasses import dataclass

from .errors import EngineError, ErrorCode


@dataclass(frozen=True)
class LogicalClock:
    """Injectable millisecond clock. The engine never reads a wall clock."""

    current_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.current_ms, int) or isinstance(self.current_ms, bool):
            raise EngineError(
                ErrorCode.SCHEMA_VIOLATION,
                "logical clock must be an integer number of milliseconds",
                details={"value": self.current_ms},
            )
        if self.current_ms < 0:
            raise EngineError(
                ErrorCode.SCHEMA_VIOLATION,
                "logical clock must be non-negative",
                details={"value": self.current_ms},
            )

    def advance_seconds(self, relative_seconds: int) -> dict:
        if (
            not isinstance(relative_seconds, int)
            or isinstance(relative_seconds, bool)
            or relative_seconds < 0
        ):
            raise EngineError(
                ErrorCode.INVALID_EFFECTIVE_SECONDS,
                "relative_effective_seconds must be a non-negative integer",
                details={"value": relative_seconds},
            )
        scale = 1000
        scaled = relative_seconds * scale
        # Integer seconds convert exactly to milliseconds. Floor is recorded
        # as the stable rounding policy even though remainder is zero.
        return {
            "effective_at_ms": self.current_ms + scaled,
            "base_logical_clock_ms": self.current_ms,
            "input_value": relative_seconds,
            "input_unit": "second",
            "scale_ms_per_second": scale,
            "rounding_direction": "floor",
            "remainder_ms": 0,
            "formula": "base_logical_clock_ms + floor(relative_seconds * 1000)",
        }


def require_clock(value: int | None, default: int | None = None) -> int:
    if value is None:
        value = default
    if value is None:
        raise EngineError(
            ErrorCode.SCHEMA_VIOLATION,
            "an injected --clock value is required",
        )
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EngineError(
            ErrorCode.CLOCK_ROLLBACK,
            "clock must be a non-negative integer millisecond value",
            details={"value": value},
        )
    return value
