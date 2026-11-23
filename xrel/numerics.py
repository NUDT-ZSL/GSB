"""Precision backends sharing one operator semantics.

Three backends implement the *same* small operator surface:

* :class:`Float64Backend`  - IEEE binary64 reference truth;
* :class:`Float32Backend`  - IEEE binary32 with subnormal flush-to-zero;
* :class:`FixedBackend`    - scaled integer arithmetic with an explicit
  scale factor and an explicit rounding mode.

The scoring pipeline is written once against the :class:`NumericBackend`
surface, therefore switching precision can only change the numeric values,
never the candidate set or the hit document set (those are determined by
term identities, not by the numeric backend).
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import Any, Iterable, List, Sequence

import numpy as np

from .errors import ConfigInvalid


class RoundingMode(str, enum.Enum):
    """Rounding modes accepted by the fixed point backend."""

    HALF_EVEN = "half_even"
    HALF_UP = "half_up"
    TRUNCATE = "truncate"
    FLOOR = "floor"
    CEILING = "ceiling"


VALID_ROUNDING_MODES = frozenset(mode.value for mode in RoundingMode)

# Smallest positive normal binary32 number: 2 ** -126 ~ 1.1754944e-38.
FLOAT32_SMALLEST_NORMAL = np.float32(np.float32(2.0) ** np.float32(-126.0))


def round_int(value: float, mode: RoundingMode) -> int:
    """Round a real value to an integer using the declared mode."""

    if mode is RoundingMode.TRUNCATE:
        return int(math.trunc(value))
    if mode is RoundingMode.FLOOR:
        return int(math.floor(value))
    if mode is RoundingMode.CEILING:
        return int(math.ceil(value))
    if mode is RoundingMode.HALF_UP:
        return int(math.floor(value + 0.5)) if value >= 0 else int(math.ceil(value - 0.5))
    # HALF_EVEN: banker's rounding, implemented explicitly to stay independent
    # of platform libc quirks and to remain deterministic.
    floor_value = math.floor(value)
    fraction = value - floor_value
    if fraction < 0.5:
        return floor_value
    if fraction > 0.5:
        return floor_value + 1
    if floor_value % 2 == 0:
        return floor_value
    return floor_value + 1


class NumericBackend:
    """Abstract operator surface shared by every precision backend."""

    name = "abstract"

    def quantize(self, real: Any) -> Any:
        raise NotImplementedError

    def add(self, left: Any, right: Any) -> Any:
        raise NotImplementedError

    def sub(self, left: Any, right: Any) -> Any:
        raise NotImplementedError

    def mul(self, left: Any, right: Any) -> Any:
        raise NotImplementedError

    def div(self, numerator: Any, denominator: Any) -> Any:
        raise NotImplementedError

    def log(self, value: Any) -> Any:
        """Natural logarithm of a dimensionless positive value."""
        raise NotImplementedError

    def sqrt(self, value: Any) -> Any:
        raise NotImplementedError

    def maximum(self, left: Any, right: Any) -> Any:
        raise NotImplementedError

    def absolute(self, value: Any) -> Any:
        raise NotImplementedError

    def sum_sorted(self, values: Iterable[Any]) -> Any:
        """Sum values in the caller's deterministic order."""
        raise NotImplementedError

    def to_float(self, value: Any) -> float:
        raise NotImplementedError

    def is_subnormal(self, value: Any) -> bool:
        raise NotImplementedError

    def flush_subnormal(self, value: Any) -> Any:
        raise NotImplementedError


class Float64Backend(NumericBackend):
    """Binary64 backend used as the reference truth."""

    name = "float64"

    def quantize(self, real: Any) -> float:
        return float(real)

    def add(self, left: Any, right: Any) -> float:
        return float(left) + float(right)

    def sub(self, left: Any, right: Any) -> float:
        return float(left) - float(right)

    def mul(self, left: Any, right: Any) -> float:
        return float(left) * float(right)

    def div(self, numerator: Any, denominator: Any) -> float:
        return float(numerator) / float(denominator)

    def log(self, value: Any) -> float:
        return math.log(float(value))

    def sqrt(self, value: Any) -> float:
        return math.sqrt(float(value))

    def maximum(self, left: Any, right: Any) -> float:
        return max(float(left), float(right))

    def absolute(self, value: Any) -> float:
        return abs(float(value))

    def sum_sorted(self, values: Iterable[Any]) -> float:
        total = 0.0
        for value in values:
            total += float(value)
        return total

    def to_float(self, value: Any) -> float:
        return float(value)

    def is_subnormal(self, value: Any) -> bool:
        number = float(value)
        if number == 0.0:
            return False
        return abs(number) < sys_float_min

    def flush_subnormal(self, value: Any) -> float:
        number = float(value)
        if number != 0.0 and abs(number) < sys_float_min:
            return 0.0
        return number


sys_float_min = np.finfo(np.float64).tiny


class Float32Backend(NumericBackend):
    """Binary32 backend; every result is rounded back to binary32.

    Subnormal binary32 results are flushed to zero so that values landing in
    the denormal band cannot silently flip threshold decisions (requirement 5).
    """

    name = "float32"

    def quantize(self, real: Any) -> np.float32:
        return self._flush(np.float32(float(real)))

    def is_subnormal_real(self, real: Any) -> bool:
        value = np.float32(float(real))
        if value == np.float32(0.0):
            return False
        return abs(value) < FLOAT32_SMALLEST_NORMAL

    @staticmethod
    def _flush(value: np.float32) -> np.float32:
        if value != np.float32(0.0) and abs(value) < FLOAT32_SMALLEST_NORMAL:
            return np.float32(0.0)
        return np.float32(value)

    def add(self, left: Any, right: Any) -> np.float32:
        return self._flush(np.float32(np.float32(left) + np.float32(right)))

    def sub(self, left: Any, right: Any) -> np.float32:
        return self._flush(np.float32(np.float32(left) - np.float32(right)))

    def mul(self, left: Any, right: Any) -> np.float32:
        return self._flush(np.float32(np.float32(left) * np.float32(right)))

    def div(self, numerator: Any, denominator: Any) -> np.float32:
        return self._flush(np.float32(np.float32(numerator) / np.float32(denominator)))

    def log(self, value: Any) -> np.float32:
        return self._flush(np.float32(math.log(float(value))))

    def sqrt(self, value: Any) -> np.float32:
        return self._flush(np.float32(np.sqrt(np.float32(value))))

    def maximum(self, left: Any, right: Any) -> np.float32:
        return self._flush(np.maximum(np.float32(left), np.float32(right)))

    def absolute(self, value: Any) -> np.float32:
        return self._flush(np.float32(abs(np.float32(value))))

    def sum_sorted(self, values: Iterable[Any]) -> np.float32:
        total = np.float32(0.0)
        for value in values:
            total = self._flush(np.float32(total + np.float32(value)))
        return total

    def to_float(self, value: Any) -> float:
        return float(np.float32(value))

    def is_subnormal(self, value: Any) -> bool:
        number = np.float32(value)
        if number == np.float32(0.0):
            return False
        return abs(number) < FLOAT32_SMALLEST_NORMAL

    def flush_subnormal(self, value: Any) -> np.float32:
        return self._flush(np.float32(value))


@dataclass(frozen=True)
class FixedConfig:
    """Explicit fixed point configuration.

    ``scale`` is a positive integer multiplier: a real value ``x`` is stored
    as the integer ``round(x * scale)``.
    """

    scale: int
    rounding: RoundingMode

    def validate(self) -> None:
        if not isinstance(self.scale, int) or isinstance(self.scale, bool) or self.scale <= 0:
            raise ConfigInvalid(
                "fixed point scale factor must be a positive integer",
                field_name="scale",
                value=self.scale,
            )
        if not isinstance(self.rounding, RoundingMode):
            raise ConfigInvalid(
                "fixed point rounding mode is unknown",
                field_name="rounding",
                value=self.rounding,
            )


class FixedBackend(NumericBackend):
    """Scaled integer backend with every real value stored as ``Q`` ints."""

    name = "fixed"

    def __init__(self, config: FixedConfig) -> None:
        config.validate()
        self.config = config
        self.scale = config.scale
        self.rounding = config.rounding

    # -- conversion helpers -------------------------------------------------
    def quantize(self, real: Any) -> int:
        # Every real input, including integer statistics such as tf and
        # doc_len, must be scaled to the same Q representation.
        return round_int(float(real) * self.scale, self.rounding)

    def to_float(self, value: Any) -> float:
        return int(value) / self.scale

    # -- integer arithmetic -------------------------------------------------
    def add(self, left: Any, right: Any) -> int:
        return int(left) + int(right)

    def sub(self, left: Any, right: Any) -> int:
        return int(left) - int(right)

    def mul(self, left: Any, right: Any) -> int:
        exact = (int(left) * int(right)) / self.scale
        return round_int(exact, self.rounding)

    def div(self, numerator: Any, denominator: Any) -> int:
        # numerator and denominator are both scaled integers; their real
        # quotient is (num/scale)/(den/scale) = num/den, re-scaled to Q.
        exact = (int(numerator) * self.scale) / int(denominator)
        return round_int(float(exact), self.rounding)

    def log(self, value: Any) -> int:
        return round_int(math.log(int(value) / self.scale) * self.scale, self.rounding)

    def sqrt(self, value: Any) -> int:
        return round_int(math.sqrt(int(value) / self.scale) * self.scale, self.rounding)

    def maximum(self, left: Any, right: Any) -> int:
        return max(int(left), int(right))

    def absolute(self, value: Any) -> int:
        return abs(int(value))

    def sum_sorted(self, values: Iterable[Any]) -> int:
        total = 0
        for value in values:
            total += int(value)
        return total

    def is_subnormal(self, value: Any) -> bool:
        # A fixed point value cannot be represented at all once its magnitude
        # is below one quantum; report those as subnormal for protection.
        number = int(value)
        if number == 0:
            return False
        return abs(number) < 1

    def flush_subnormal(self, value: Any) -> int:
        number = int(value)
        return 0 if abs(number) < 1 else number


def make_backend(precision: str, fixed_config: FixedConfig | None = None) -> NumericBackend:
    """Factory validating the precision configuration up front."""

    if precision == "float64":
        return Float64Backend()
    if precision == "float32":
        return Float32Backend()
    if precision == "fixed":
        if fixed_config is None:
            raise ConfigInvalid(
                "fixed precision requires an explicit fixed point configuration",
                field_name="fixed",
            )
        fixed_config.validate()
        return FixedBackend(fixed_config)
    raise ConfigInvalid("unknown precision tier", field_name="precision", value=precision)


def parse_fixed_config(raw: dict | None) -> FixedConfig | None:
    """Parse and validate a raw fixed point configuration mapping."""

    if raw is None:
        return None
    scale = raw.get("scale")
    rounding_value = raw.get("rounding")
    if not isinstance(scale, int) or isinstance(scale, bool) or scale <= 0:
        raise ConfigInvalid(
            "fixed point scale factor must be a positive integer",
            field_name="fixed.scale",
            value=scale,
        )
    if rounding_value not in VALID_ROUNDING_MODES:
        raise ConfigInvalid(
            "fixed point rounding mode is unknown",
            field_name="fixed.rounding",
            value=rounding_value,
        )
    config = FixedConfig(scale=scale, rounding=RoundingMode(rounding_value))
    config.validate()
    return config


PRECISION_TIERS: Sequence[str] = ("float64", "float32", "fixed")


def to_json_number(value: Any) -> float | int:
    """Convert a backend scalar into a JSON friendly canonical number."""

    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    return value


def backend_values_to_floats(values: List[Any], backend: NumericBackend) -> List[float]:
    return [backend.to_float(value) for value in values]
