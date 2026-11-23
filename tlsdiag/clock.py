"""Deterministic logical clock and precision configuration.

The engine never reads the wall clock.  Instead a step table supplied with the
sample defines a logical timeline in milliseconds::

    {"epoch_ms": 1700000000000, "steps_ms": [0, 60000, 60000, 123, ...]}

Logical instant ``tick`` ``k`` resolves to ``epoch_ms + sum(steps_ms[:k+1])``
with ``tick 0`` anchored exactly on ``epoch_ms``.  All arithmetic uses ints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import DiagError


@dataclass(frozen=True)
class LogicalClock:
    """Injected clock; every returned value is an integer millisecond instant."""

    epoch_ms: int
    steps_ms: tuple[int, ...]

    @classmethod
    def from_file(cls, path: str | Path) -> "LogicalClock":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "LogicalClock":
        if not isinstance(data.get("epoch_ms"), int) or isinstance(data["epoch_ms"], bool):
            raise DiagError("E5001", "epoch_ms must be an integer")
        steps = data.get("steps_ms")
        if not isinstance(steps, list) or not steps:
            raise DiagError("E5001", "steps_ms must be a non-empty list")
        for step in steps:
            if not isinstance(step, int) or isinstance(step, bool) or step < 0:
                raise DiagError("E5001", "every clock step must be a non-negative integer")
        return cls(epoch_ms=data["epoch_ms"], steps_ms=tuple(steps))

    def instant_ms(self, tick: int) -> int:
        if tick < 0:
            raise DiagError("E5001", f"logical tick must be non-negative, got {tick}")
        if tick >= len(self.steps_ms):
            raise DiagError("E5001", f"logical tick {tick} exceeds step table length {len(self.steps_ms)}")
        return self.epoch_ms + sum(self.steps_ms[: tick + 1])

    @property
    def high_water_ms(self) -> int:
        """Latest defined instant, used as the persisted clock watermark."""
        return self.instant_ms(len(self.steps_ms) - 1)

    @property
    def tick_count(self) -> int:
        return len(self.steps_ms)


@dataclass(frozen=True)
class PrecisionConfig:
    """Knobs controlling the dual-path fixed-point / high-precision ledger.

    ``ms_per_second`` is expressed as a fraction (numerator/denominator) so the
    fixed-point path can use a deliberately truncated scale factor, while the
    decimal truth path keeps full precision.  ``frac_bits`` bounds the internal
    fixed-point accumulator so rounding actually occurs on awkward factors.
    """

    ms_num: int
    ms_den: int
    frac_bits: int
    rounding: str
    sensitive_tolerance_ms: int
    reverify_remaining_ms: int

    @classmethod
    def from_file(cls, path: str | Path) -> "PrecisionConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "PrecisionConfig":
        try:
            ms_num = int(data["ms_per_second_num"])
            ms_den = int(data["ms_per_second_den"])
            frac_bits = int(data["frac_bits"])
            rounding = str(data.get("rounding", "ROUND_HALF_EVEN"))
            tol = int(data["sensitive_tolerance_ms"])
            reverify = int(data["reverify_remaining_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DiagError("E5001", f"malformed precision config: {exc}") from exc
        if ms_num <= 0 or ms_den <= 0 or frac_bits < 0 or tol < 0 or reverify < 0:
            raise DiagError("E5001", "precision config values must be non-negative and scale positive")
        if rounding not in {"ROUND_HALF_EVEN", "ROUND_DOWN", "ROUND_CEILING", "ROUND_FLOOR"}:
            raise DiagError("E5001", f"unsupported rounding mode {rounding}")
        return cls(ms_num, ms_den, frac_bits, rounding, tol, reverify)

    def to_dict(self) -> dict:
        return {
            "ms_per_second_num": self.ms_num,
            "ms_per_second_den": self.ms_den,
            "frac_bits": self.frac_bits,
            "rounding": self.rounding,
            "sensitive_tolerance_ms": self.sensitive_tolerance_ms,
            "reverify_remaining_ms": self.reverify_remaining_ms,
        }
