"""Exact integer-millisecond time conversion and allocation helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

GRID_MS = 10
OUTPUT_DECIMALS = 3


class TimeValueError(ValueError):
    pass


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TimeValueError(f"not a numeric timestamp: {value!r}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise TimeValueError(f"invalid timestamp: {value!r}") from exc
    else:
        raise TimeValueError(f"unsupported timestamp type: {type(value).__name__}")
    if not result.is_finite():
        raise TimeValueError(f"non-finite timestamp: {value!r}")
    return result


def seconds_to_ms(value: Any) -> tuple[int, Decimal, Decimal]:
    """Return (rounded_ms, absolute_rounding_error_ms, raw_ms).

    The caller is responsible for accumulating precision statistics.  Using
    Decimal here avoids binary floating point accumulation entirely.
    """

    raw_ms = _decimal(value) * Decimal(1000)
    rounded = raw_ms.to_integral_value(rounding=ROUND_HALF_UP)
    error = abs(raw_ms - rounded)
    return int(rounded), error, raw_ms


def srt_time_to_ms(value: str) -> int:
    import re

    match = re.fullmatch(r"(\d{2}):([0-5]\d):([0-5]\d),(\d{3})", value.strip())
    if not match:
        raise TimeValueError(f"invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def seconds_text(ms: int) -> str:
    """Render milliseconds as seconds with exactly three decimal places."""

    if ms < 0:
        raise TimeValueError("negative timestamps cannot be serialized")
    return f"{ms // 1000}.{ms % 1000:03d}"


def srt_time_text(ms: int) -> str:
    if ms < 0:
        raise TimeValueError("negative timestamps cannot be serialized")
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def round_to_grid(ms: int, grid_ms: int = GRID_MS) -> int:
    if grid_ms <= 0:
        raise ValueError("grid must be positive")
    if ms >= 0:
        return ((ms + grid_ms // 2) // grid_ms) * grid_ms
    return -(((-ms + grid_ms // 2) // grid_ms) * grid_ms)


def allocate_by_char_counts(
    start_ms: int,
    end_ms: int,
    texts: Iterable[str],
    grid_ms: int = GRID_MS,
) -> tuple[list[tuple[int, int]], int, int]:
    """Allocate [start,end) to tokens by character weight on a time grid.

    Returns (spans, rounding_count, max_grid_error_ms).  Boundaries are
    integer millisecond values.  If the interval is long enough, each token
    receives at least one grid unit.  The final token always ends at end_ms so
    sentence endpoints are never changed by inference.
    """

    token_list = list(texts)
    if not token_list:
        return [], 0, 0
    if start_ms < 0 or end_ms < start_ms:
        raise TimeValueError("invalid allocation interval")

    duration = end_ms - start_ms
    weights = [max(1, len(text)) for text in token_list]
    total_weight = sum(weights)
    existing_boundaries = [start_ms]
    cumulative_weight = 0
    rounding_count = 0
    max_error = 0
    for weight in weights[:-1]:
        cumulative_weight += weight
        ideal = Decimal(start_ms) + Decimal(duration) * Decimal(
            cumulative_weight
        ) / Decimal(total_weight)
        boundary = round_to_grid(
            int(ideal.to_integral_value(rounding=ROUND_HALF_UP)), grid_ms
        )
        boundary = max(existing_boundaries[-1], min(end_ms, boundary))
        error = abs(ideal - Decimal(boundary))
        if error > 0:
            rounding_count += 1
            max_error = max(
                max_error,
                int((error * 1000).to_integral_value(rounding=ROUND_HALF_UP)),
            )
        existing_boundaries.append(boundary)

    spans: list[tuple[int, int]] = []
    for index, boundary in enumerate(existing_boundaries):
        token_start = boundary
        token_end = (
            end_ms if index == len(token_list) - 1 else existing_boundaries[index + 1]
        )
        token_end = max(token_end, token_start)
        spans.append((token_start, token_end))

    return spans, rounding_count, max_error


def split_boundary(boundary_ms: int, grid_ms: int = GRID_MS) -> tuple[int, int]:
    """Return (grid-aligned boundary, absolute grid adjustment in ms)."""

    aligned = round_to_grid(boundary_ms, grid_ms)
    return aligned, abs(aligned - boundary_ms)


@dataclass(slots=True)
class PrecisionStats:
    parse_rounding_count: int = 0
    parse_max_error_us: int = 0
    grid_rounding_count: int = 0
    grid_max_error_us: int = 0
    output_timestamp_count: int = 0

    def add_parse_error(self, error_ms: Decimal) -> None:
        if error_ms > 0:
            self.parse_rounding_count += 1
            error_us = int((error_ms * 1000).to_integral_value(rounding=ROUND_HALF_UP))
            self.parse_max_error_us = max(self.parse_max_error_us, error_us)

    def add_grid_error(self, error_ms: int | Decimal, count: bool = True) -> None:
        if error_ms > 0:
            if count:
                self.grid_rounding_count += 1
            error_us = int(
                (Decimal(error_ms) * 1000).to_integral_value(rounding=ROUND_HALF_UP)
            )
            self.grid_max_error_us = max(self.grid_max_error_us, error_us)

    def merge(self, other: PrecisionStats) -> None:
        self.parse_rounding_count += other.parse_rounding_count
        self.parse_max_error_us = max(self.parse_max_error_us, other.parse_max_error_us)
        self.grid_rounding_count += other.grid_rounding_count
        self.grid_max_error_us = max(self.grid_max_error_us, other.grid_max_error_us)
        self.output_timestamp_count += other.output_timestamp_count

    @property
    def rounding_count(self) -> int:
        return self.parse_rounding_count + self.grid_rounding_count

    @property
    def max_error_us(self) -> int:
        return max(self.parse_max_error_us, self.grid_max_error_us)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parse_rounding_count": self.parse_rounding_count,
            "parse_max_error_ms": self.parse_max_error_us / 1000,
            "grid_rounding_count": self.grid_rounding_count,
            "grid_max_error_ms": self.grid_max_error_us / 1000,
            "output_timestamp_count": self.output_timestamp_count,
            "rounding_count": self.rounding_count,
            "max_rounding_error_ms": self.max_error_us / 1000,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PrecisionStats:
        if not data:
            return cls()
        return cls(
            parse_rounding_count=int(data.get("parse_rounding_count", 0)),
            parse_max_error_us=round(float(data.get("parse_max_error_ms", 0)) * 1000),
            grid_rounding_count=int(data.get("grid_rounding_count", 0)),
            grid_max_error_us=round(float(data.get("grid_max_error_ms", 0)) * 1000),
            output_timestamp_count=int(data.get("output_timestamp_count", 0)),
        )
