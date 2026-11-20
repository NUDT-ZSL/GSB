from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .config import ElectionConfig
from .errors import fail
from .numeric import FixedValue, fixed_from_input, fraction, fraction_text


@dataclass(frozen=True)
class ClockAdvance:
    requested: Fraction
    effective: Fraction
    fixed: FixedValue
    rewind_tolerated: bool
    rewind_ms: Fraction

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_clock_ms": fraction_text(self.requested),
            "effective_clock_ms": fraction_text(self.effective),
            "clock_fixed": self.fixed.as_dict(),
            "rewind_tolerated": self.rewind_tolerated,
            "rewind_ms": fraction_text(self.rewind_ms),
        }


def parse_clock(value: Any, config: ElectionConfig) -> tuple[Fraction, FixedValue]:
    try:
        exact = fraction(value)
    except Exception:
        fail("E_BAD_CLOCK", "Logical clock must be a finite decimal number", details={"value": str(value)})
    if exact < 0:
        fail("E_BAD_CLOCK", "Logical clock must be non-negative", details={"value": fraction_text(exact)})
    fixed = fixed_from_input(
        value,
        config.scale,
        label="logical_clock_ms",
        include_float_repr=True,
    )
    return exact, fixed


def advance_clock(
    value: Any,
    previous: Fraction,
    config: ElectionConfig,
) -> ClockAdvance:
    requested, fixed = parse_clock(value, config)
    if requested < previous:
        rewind_ms = previous - requested
        if rewind_ms > config.clock_backward_tolerance:
            fail(
                "E_CLOCK_REWIND",
                "Logical clock moved backwards beyond the configured tolerance",
                details={
                    "previous_clock_ms": fraction_text(previous),
                    "requested_clock_ms": fraction_text(requested),
                    "rewind_ms": fraction_text(rewind_ms),
                    "tolerance_ms": fraction_text(config.clock_backward_tolerance),
                },
            )
        effective = previous
        effective_fixed = fixed_from_input(
            previous,
            config.scale,
            label="logical_clock_ms_clamped",
            include_float_repr=True,
        )
        return ClockAdvance(
            requested=requested,
            effective=effective,
            fixed=effective_fixed,
            rewind_tolerated=True,
            rewind_ms=rewind_ms,
        )
    step = requested - previous
    if step > config.max_clock_step:
        fail(
            "E_CLOCK_JUMP",
            "Single logical clock step exceeds the configured maximum",
            details={
                "previous_clock_ms": fraction_text(previous),
                "requested_clock_ms": fraction_text(requested),
                "step_ms": fraction_text(step),
                "max_step_ms": fraction_text(config.max_clock_step),
            },
        )
    return ClockAdvance(
        requested=requested,
        effective=requested,
        fixed=fixed,
        rewind_tolerated=False,
        rewind_ms=Fraction(0, 1),
    )


def clock_fixed(value: Fraction, config: ElectionConfig, *, label: str = "logical_clock_ms") -> FixedValue:
    return fixed_from_input(
        value,
        config.scale,
        label=label,
        include_float_repr=True,
    )
