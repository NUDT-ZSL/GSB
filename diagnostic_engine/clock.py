from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Iterable

import numpy as np

from .errors import ErrorCode, DiagnosticError


DEFAULT_CLOCK_PROFILE = {
    # Fixed anchor; not wall clock, so repeated runs are bit-for-bit stable.
    "anchor_ms": 1_760_000_000_000,
    "tick_ms": 137,
    "tick_scale": "1 logical_tick = 137 absolute_ms",
}


@dataclass(frozen=True)
class ClockProfile:
    anchor_ms: int = DEFAULT_CLOCK_PROFILE["anchor_ms"]  # type: ignore[assignment]
    tick_ms: int = DEFAULT_CLOCK_PROFILE["tick_ms"]  # type: ignore[assignment]

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "ClockProfile":
        data = value or DEFAULT_CLOCK_PROFILE
        try:
            anchor_ms = int(data.get("anchor_ms", cls.anchor_ms))
            tick_ms = int(data.get("tick_ms", cls.tick_ms))
        except (TypeError, ValueError) as exc:
            raise DiagnosticError(
                ErrorCode.CLOCK_PROFILE_INVALID,
                "clock profile values must be integers",
                details={"clock_profile": data},
            ) from exc
        if anchor_ms < 0 or tick_ms <= 0:
            raise DiagnosticError(
                ErrorCode.CLOCK_PROFILE_INVALID,
                "anchor_ms must be non-negative and tick_ms must be positive",
                details={"anchor_ms": anchor_ms, "tick_ms": tick_ms},
            )
        return cls(anchor_ms=anchor_ms, tick_ms=tick_ms)

    def clock_to_ms(self, logical_clock: int) -> int:
        if logical_clock < 0:
            raise DiagnosticError(
                ErrorCode.LOGICAL_CLOCK_NEGATIVE,
                "logical clock must be non-negative",
                logical_clock=logical_clock,
            )
        return self.anchor_ms + int(logical_clock) * self.tick_ms

    def clock_series_to_ms(self, clocks: Iterable[int]) -> list[int]:
        values = list(clocks)
        if any(value < 0 for value in values):
            bad = next(value for value in values if value < 0)
            raise DiagnosticError(
                ErrorCode.LOGICAL_CLOCK_NEGATIVE,
                "logical clock must be non-negative",
                logical_clock=bad,
            )
        # NumPy gives deterministic vectorized conversion for injected clocks.
        array = np.asarray(values, dtype=np.int64)
        converted = self.anchor_ms + array * np.int64(self.tick_ms)
        return [int(value) for value in converted.tolist()]


class LogicalClockDriver:
    """Monotone logical-clock driver with an injectable starting value."""

    def __init__(self, profile: ClockProfile | None = None, initial_clock: int = 0) -> None:
        if initial_clock < 0:
            raise DiagnosticError(
                ErrorCode.LOGICAL_CLOCK_NEGATIVE,
                "initial logical clock must be non-negative",
                logical_clock=initial_clock,
            )
        self.profile = profile or ClockProfile()
        self.current = initial_clock
        self.observations: list[dict[str, int]] = []

    def advance_to(self, requested_clock: int) -> int:
        if requested_clock < 0:
            raise DiagnosticError(
                ErrorCode.LOGICAL_CLOCK_NEGATIVE,
                "event logical clock must be non-negative",
                logical_clock=requested_clock,
            )
        self.current = max(self.current, int(requested_clock))
        self.observations.append(
            {"requested_clock": int(requested_clock), "effective_clock": self.current}
        )
        return self.current

    def absolute_ms(self, logical_clock: int | None = None) -> int:
        return self.profile.clock_to_ms(self.current if logical_clock is None else logical_clock)


def convert_relative_seconds(raw_seconds: Any) -> dict[str, Any]:
    """Convert non-negative seconds to floor milliseconds plus ns remainder."""
    if isinstance(raw_seconds, bool):
        raise DiagnosticError(
            ErrorCode.V1_RELATIVE_SECONDS_INVALID,
            "relative seconds must be a non-negative number or decimal string",
            details={"value": raw_seconds},
        )
    try:
        seconds = Decimal(str(raw_seconds))
    except (InvalidOperation, ValueError) as exc:
        raise DiagnosticError(
            ErrorCode.V1_RELATIVE_SECONDS_INVALID,
            "relative seconds must be a valid decimal",
            details={"value": raw_seconds},
        ) from exc
    if not seconds.is_finite() or seconds < 0:
        raise DiagnosticError(
            ErrorCode.V1_RELATIVE_SECONDS_INVALID,
            "relative seconds must be finite and non-negative",
            details={"value": str(raw_seconds)},
        )

    total_nanoseconds = (seconds * Decimal(1_000_000_000)).to_integral_value(
        rounding=ROUND_FLOOR
    )
    total_ns_int = int(total_nanoseconds)
    milliseconds, remainder_ns = divmod(total_ns_int, 1_000_000)
    return {
        "input": str(raw_seconds),
        "scale_seconds_to_milliseconds": 1000,
        "quantization_nanoseconds": 1_000_000,
        "rounding": "floor",
        "rounding_direction": "toward_zero_for_non_negative_values",
        "relative_milliseconds": milliseconds,
        "remainder_nanoseconds": remainder_ns,
        "exact_nanoseconds": total_ns_int,
    }


def v1_effective_time(
    profile: ClockProfile,
    declared_clock: int,
    raw_relative_seconds: Any,
) -> dict[str, Any]:
    clock_ms = profile.clock_to_ms(declared_clock)
    relative = convert_relative_seconds(raw_relative_seconds)
    effective_ms = clock_ms + relative["relative_milliseconds"]
    return {
        "declared_clock": declared_clock,
        "declared_clock_absolute_ms": clock_ms,
        "relative_conversion": relative,
        "effective_absolute_ms": effective_ms,
        "tick_ms": profile.tick_ms,
        "anchor_ms": profile.anchor_ms,
        "scale": f"absolute_ms={profile.anchor_ms}+logical_clock*{profile.tick_ms}+floor(relative_seconds*1000)",
    }


def decide_snapshot_candidate(
    v1_candidate: dict[str, Any] | None,
    v2_candidate: dict[str, Any] | None,
) -> dict[str, Any]:
    """Choose a snapshot version using generation as a fencing token.

    Generation dominates because a stale cache incarnation must not win merely
    because its clock is unusual. Within one generation, absolute logical-clock
    time decides; exact ties deterministically prefer v2.
    """
    if v1_candidate is None and v2_candidate is None:
        raise DiagnosticError(ErrorCode.V1_SNAPSHOT_INVALID, "no snapshot candidate")
    if v1_candidate is None:
        return {
            "winner": "v2",
            "reason": "only_v2_present",
            "winner_record": v2_candidate,
            "v1": None,
            "v2": v2_candidate,
        }
    if v2_candidate is None:
        return {
            "winner": "v1",
            "reason": "only_v1_present",
            "winner_record": v1_candidate,
            "v1": v1_candidate,
            "v2": None,
        }

    generation_v1 = int(v1_candidate["cache_generation"])
    generation_v2 = int(v2_candidate["cache_generation"])
    clock_v1 = int(v1_candidate["decision_clock_ms"])
    clock_v2 = int(v2_candidate["decision_clock_ms"])

    if generation_v1 != generation_v2:
        winner = "v2" if generation_v2 > generation_v1 else "v1"
        reason = "higher_cache_generation"
    elif clock_v1 != clock_v2:
        winner = "v2" if clock_v2 > clock_v1 else "v1"
        reason = "later_absolute_logical_clock"
    else:
        winner = "v2"
        reason = "exact_generation_and_clock_tie_prefer_v2"

    return {
        "winner": winner,
        "reason": reason,
        "winner_record": v2_candidate if winner == "v2" else v1_candidate,
        "comparison_tuple_order": "(cache_generation, decision_clock_ms, format_tie_break_v2)",
        "comparison_tuples": {
            "v1": [generation_v1, clock_v1, 0],
            "v2": [generation_v2, clock_v2, 1],
        },
        "v1": v1_candidate,
        "v2": v2_candidate,
    }
