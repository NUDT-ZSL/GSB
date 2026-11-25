from __future__ import annotations

from dataclasses import dataclass

from .errors import ClockRollbackError


@dataclass(frozen=True)
class ClockCheck:
    requested_clock_ms: int
    watermark_clock_ms: int
    tolerance_ms: int
    accepted: bool
    rollback_by_ms: int
    next_watermark_ms: int


def check_clock(
    requested_clock_ms: int,
    watermark_clock_ms: int,
    tolerance_ms: int,
) -> ClockCheck:
    rollback_by = watermark_clock_ms - requested_clock_ms
    if rollback_by > tolerance_ms:
        raise ClockRollbackError(
            "logical clock rollback exceeds tolerance",
            {
                "requested_clock_ms": requested_clock_ms,
                "watermark_clock_ms": watermark_clock_ms,
                "tolerance_ms": tolerance_ms,
                "rollback_by_ms": rollback_by,
            },
        )
    return ClockCheck(
        requested_clock_ms=requested_clock_ms,
        watermark_clock_ms=watermark_clock_ms,
        tolerance_ms=tolerance_ms,
        accepted=True,
        rollback_by_ms=max(0, rollback_by),
        next_watermark_ms=max(watermark_clock_ms, requested_clock_ms),
    )
