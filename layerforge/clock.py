from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .errors import ClockRollbackError


@dataclass
class ClockDecision:
    observed_ms: int
    previous_watermark_ms: int
    watermark_ms: int
    tolerance_ms: int
    rolled_back: bool
    accepted: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "observed_ms": self.observed_ms,
            "previous_watermark_ms": self.previous_watermark_ms,
            "watermark_ms": self.watermark_ms,
            "tolerance_ms": self.tolerance_ms,
            "rolled_back": self.rolled_back,
            "accepted": self.accepted,
        }


class LogicalClock:
    """Monotonic-ish injected clock with a declared rollback tolerance."""

    def __init__(self, watermark_ms: int, tolerance_ms: int = 2) -> None:
        if isinstance(watermark_ms, bool) or not isinstance(watermark_ms, int):
            raise ValueError("watermark_ms must be an integer")
        self.watermark_ms = watermark_ms
        self.tolerance_ms = tolerance_ms

    def observe(self, value_ms: Any, *, field: str = "logical_clock_ms") -> ClockDecision:
        if isinstance(value_ms, bool) or not isinstance(value_ms, int):
            raise ClockRollbackError(
                f"{field} must be an integer millisecond value",
                details={"field": field, "value": value_ms},
            )
        previous = self.watermark_ms
        difference = previous - value_ms
        rolled_back = value_ms < previous
        if difference > self.tolerance_ms:
            raise ClockRollbackError(
                "logical clock moved backwards beyond tolerance",
                details={
                    "field": field,
                    "observed_ms": value_ms,
                    "watermark_ms": previous,
                    "backward_ms": difference,
                    "tolerance_ms": self.tolerance_ms,
                },
            )
        if value_ms > self.watermark_ms:
            self.watermark_ms = value_ms
        return ClockDecision(
            observed_ms=value_ms,
            previous_watermark_ms=previous,
            watermark_ms=self.watermark_ms,
            tolerance_ms=self.tolerance_ms,
            rolled_back=rolled_back,
            accepted=True,
        )

    def advance(self, duration_ms: int) -> int:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative integer")
        self.watermark_ms += duration_ms
        return self.watermark_ms

    def snapshot(self) -> Dict[str, int]:
        return {"watermark_ms": self.watermark_ms, "tolerance_ms": self.tolerance_ms}
