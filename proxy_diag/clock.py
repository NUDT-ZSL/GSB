"""Injected logical clock conversion.

Business ordering never calls a wall clock.  The only epoch-like value used to
render v1 relative seconds as absolute milliseconds is read from the
deterministic injected clock file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import E_SCHEMA, WorkbenchError
from .jsonio import read_json


@dataclass(frozen=True)
class ClockSpec:
    clock_id: str
    base_absolute_ms: int
    v1_origin_logical_ms: int
    scale_numerator: int
    scale_denominator: int
    rounding: str
    watermark_ms: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClockSpec":
        required_int = (
            "base_absolute_ms",
            "v1_origin_logical_ms",
            "scale_numerator",
            "scale_denominator",
            "watermark_ms",
        )
        for key in required_int:
            if not isinstance(data.get(key), int) or isinstance(data[key], bool):
                raise WorkbenchError(E_SCHEMA, f"clock field {key} must be an integer")
        rounding = data.get("rounding", "nearest")
        if rounding not in {"floor", "ceil", "nearest"}:
            raise WorkbenchError(E_SCHEMA, "unsupported clock rounding", {"rounding": rounding})
        if data["scale_numerator"] <= 0 or data["scale_denominator"] <= 0:
            raise WorkbenchError(E_SCHEMA, "clock scale must be positive")
        return cls(
            clock_id=str(data.get("clock_id", "injected-clock")),
            base_absolute_ms=data["base_absolute_ms"],
            v1_origin_logical_ms=data["v1_origin_logical_ms"],
            scale_numerator=data["scale_numerator"],
            scale_denominator=data["scale_denominator"],
            rounding=rounding,
            watermark_ms=data["watermark_ms"],
        )

    @classmethod
    def load(cls, path: Path) -> "ClockSpec":
        return cls.from_dict(read_json(path))

    def convert_v1_second(self, relative_second: Any) -> dict[str, Any]:
        if (
            not isinstance(relative_second, int)
            or isinstance(relative_second, bool)
            or relative_second < 0
        ):
            raise WorkbenchError(
                "E_V1_RELATIVE_SECOND_INVALID",
                "v1 record_second must be a non-negative integer",
                {"value": relative_second},
            )
        numerator = relative_second * self.scale_numerator
        denominator = self.scale_denominator
        if self.rounding == "floor":
            offset_ms = numerator // denominator
        elif self.rounding == "ceil":
            offset_ms = -(-numerator // denominator)
        else:
            offset_ms = (2 * numerator + denominator) // (2 * denominator)
        logical_ms = self.v1_origin_logical_ms + offset_ms
        absolute_ms = self.base_absolute_ms + offset_ms
        return {
            "from_representation": "relative_record_seconds",
            "relative_record_second": relative_second,
            "scale_ms_per_second": self.scale_numerator / self.scale_denominator,
            "scale_numerator": self.scale_numerator,
            "scale_denominator": self.scale_denominator,
            "rounding_direction": self.rounding,
            "rounded_offset_ms": offset_ms,
            "logical_clock_ms": logical_ms,
            "absolute_ms": absolute_ms,
        }
