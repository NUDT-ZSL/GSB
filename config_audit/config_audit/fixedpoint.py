"""Unified fixed-point precision and integer-grid quantization.

A :class:`Grid` fixes the precision *for a whole audit session* via an integer
decimal scale ``factor = 10 ** places``.  Every real number is represented as an
integer number of grid steps::

    quantized(value) = round_half_even(value * factor)

All comparisons, tolerances and error propagation are then performed over
integers, which is what makes repeated audits bit-identical (no floating point,
no dict-order dependence).

For each quantization we record the exact downward and upward integer error
bounds in grid units.  With half-even rounding the representative is the
nearest grid point, so the bounds are::

    up_error   = ceil(exact_scaled - q)   in {0, 1}
    down_error = ceil(q - exact_scaled)   in {0, 1}

and at least one of them is zero.  Derived quantities accumulate these integer
bounds in :mod:`config_audit.propagation`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from .errors import GridError
from .models import QuantizationRecord

ROUNDING_NAME = "HALF_EVEN"
MIN_PLACES = 0
MAX_PLACES = 12


@dataclass(frozen=True)
class Grid:
    """Immutable fixed-point grid definition for one audit session."""

    places: int
    factor: int

    @classmethod
    def create(cls, places: int) -> "Grid":
        if not isinstance(places, int) or isinstance(places, bool):
            raise GridError(
                "grid places must be an integer",
                locations=["session.places"],
                grid=None,
            )
        if places < MIN_PLACES or places > MAX_PLACES:
            raise GridError(
                f"grid places must be within [{MIN_PLACES}, {MAX_PLACES}]",
                locations=["session.places"],
                grid=places,
            )
        return cls(places=places, factor=10 ** places)

    def to_decimal(self, q: int) -> Decimal:
        """Inverse mapping: integer grid steps -> exact fixed-point Decimal."""
        return Decimal(q) / Decimal(self.factor)

    def signature(self) -> str:
        return f"grid-p{self.places}-f{self.factor}"


def parse_decimal(raw) -> Decimal:
    """Parse a JSON numeric value into an exact Decimal.

    ``float`` inputs are routed through their shortest round-trip ``str`` form
    so a value authored as ``0.1`` is treated as exactly 0.1 rather than the
    binary float tail.  Booleans are rejected.
    """
    if isinstance(raw, bool):
        raise GridError("boolean is not a numeric value")
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(str(raw))
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, str):
        try:
            return Decimal(raw)
        except Exception as exc:  # noqa: BLE001 - re-raise as business error
            raise GridError(f"not a numeric literal: {raw!r}") from exc
    raise GridError(f"unsupported numeric type: {type(raw).__name__}")


def quantize(value: Decimal, grid: Grid, label: str) -> QuantizationRecord:
    """Quantize one Decimal onto the integer grid, recording error bounds.

    The computation is exact Decimal arithmetic with a generous precision cap;
    only the final representative is rounded (half-even).
    """
    if not isinstance(value, Decimal):
        value = parse_decimal(value)
    factor = Decimal(grid.factor)
    with localcontext() as ctx:
        ctx.prec = 50
        scaled = value * factor
        q = int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN))
        delta = scaled - Decimal(q)          # exact scaled - representative
        if delta >= 0:
            up_error = _ceil_nonneg(delta)
            down_error = 0
        else:
            down_error = _ceil_nonneg(-delta)
            up_error = 0
    return QuantizationRecord(
        raw=format(value, "f"),
        quantized=q,
        down_error=int(down_error),
        up_error=int(up_error),
        rounding=ROUNDING_NAME,
    )


def _ceil_nonneg(nonneg_decimal: Decimal) -> int:
    """Smallest integer >= a guaranteed non-negative Decimal."""
    return int(nonneg_decimal.to_integral_value(rounding="ROUND_CEILING"))


def quantize_coefficient(value: Decimal) -> QuantizationRecord:
    """Quantize a derivation coefficient to a plain integer grid coefficient.

    Coefficients do *not* carry the value scale ``S``: in the integer formula
    ``Y = sum(C_i * X_i) + K`` each ``C_i`` is the rounded integer coefficient
    and each ``X_i`` already contains the single required factor of ``S``.
    Rounding is half-even and the residual is at most one integer step.
    """
    if not isinstance(value, Decimal):
        value = parse_decimal(value)
    q = int(value.to_integral_value(rounding=ROUND_HALF_EVEN))
    delta = value - Decimal(q)
    if delta >= 0:
        down_error = _ceil_nonneg(delta)
        up_error = 0
    else:
        up_error = _ceil_nonneg(-delta)
        down_error = 0
    return QuantizationRecord(
        raw=format(value, "f"),
        quantized=q,
        down_error=int(down_error),
        up_error=int(up_error),
        rounding=ROUNDING_NAME,
    )
