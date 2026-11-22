"""Deterministic fixed-point arithmetic with tracked rounding-error bounds.

All SLI / budget math runs on :class:`decimal.Decimal` (never binary ``float``)
so the same inputs always produce the same serialised digits.  Every addition
and division is performed through the helpers in this module, which also
accumulate a rigorous upper bound on the rounding error introduced, following
the standard model ``fl(a op b) = (a op b)(1 + d)`` with
``|d| <= u = 1/2 * 10**(1 - precision)``.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Iterable, List, Sequence, Tuple

#: Shared arithmetic context.  38 significant digits is ample for budget math
#: and keeps every result byte-stable across Python 3.10+ interpreters.
PRECISION = 38
UNIT_ROUND: Decimal = Decimal(1).scaleb(1 - PRECISION) / Decimal(2)

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


def make_context():
    """Return a fresh deterministic decimal context (half-even, precision 38)."""
    from decimal import Context

    return Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)


def to_decimal(value, field: str = "value") -> Decimal:
    """Parse a number/string into Decimal via its canonical string form.

    Going through ``str`` avoids inheriting binary-float representation noise
    when JSON happens to deliver a float literal.
    """
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{field} must be numeric, got {type(value).__name__}")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive
        raise TypeError(f"{field} is not a valid decimal: {value!r}") from exc


def add(a: Decimal, b: Decimal) -> Tuple[Decimal, Decimal]:
    """Exact-rounded addition; returns ``(result, single-op error bound)``."""
    result = (a + b).normalize()
    return result, abs(result) * UNIT_ROUND


def div(a: Decimal, b: Decimal) -> Tuple[Decimal, Decimal]:
    """Exact-rounded division; returns ``(result, single-op error bound)``."""
    if b == 0:
        raise ZeroDivisionError("division by zero in SLI arithmetic")
    result = (a / b).normalize()
    return result, abs(result) * UNIT_ROUND


def mul(a: Decimal, b: Decimal) -> Tuple[Decimal, Decimal]:
    """Exact-rounded multiplication; returns ``(result, single-op error bound)``."""
    result = (a * b).normalize()
    return result, abs(result) * UNIT_ROUND


def sub(a: Decimal, b: Decimal) -> Tuple[Decimal, Decimal]:
    """Exact-rounded subtraction; returns ``(result, single-op error bound)``."""
    result = (a - b).normalize()
    return result, abs(result) * UNIT_ROUND


class Tracked:
    """A Decimal value paired with the accumulated arithmetic error bound.

    The bound follows the worst-case forward error model:

    * addition:  ``e_new = e_a + e_b + u * |a + b|``
    * division:  ``e_new = (e_a + |a/b| * e_b) / |b| + u * |a/b|``
    """

    __slots__ = ("value", "error")

    def __init__(self, value: Decimal, error: Decimal = ZERO) -> None:
        self.value = value
        self.error = error

    def add_value(self, other: Decimal) -> "Tracked":
        result, op_err = add(self.value, other)
        return Tracked(result, self.error + op_err)

    def add_tracked(self, other: "Tracked") -> "Tracked":
        result, op_err = add(self.value, other.value)
        return Tracked(result, self.error + other.error + op_err)

    def div_by(self, other: "Tracked") -> "Tracked":
        result, op_err = div(self.value, other.value)
        propagated = (self.error + abs(result) * other.error) / abs(other.value)
        return Tracked(result, propagated + op_err)

    def multiply(self, other: "Tracked") -> "Tracked":
        result, op_err = mul(self.value, other.value)
        propagated = (
            abs(self.value) * other.error
            + abs(other.value) * self.error
            + self.error * other.error
        )
        return Tracked(result, propagated + op_err)

    def subtract(self, other: "Tracked") -> "Tracked":
        result, op_err = sub(self.value, other.value)
        return Tracked(result, self.error + other.error + op_err)

    def to_dict(self) -> dict:
        return {"value": str(self.value), "error_bound": str(self.error.normalize())}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Tracked({self.value!s}, err<={self.error.normalize()!s})"


def ordered_sum(values: Iterable[Decimal]) -> Tracked:
    """Sum values strictly left-to-right in the given (deterministic) order."""
    acc = Tracked(ZERO)
    for value in values:
        acc = acc.add_value(value)
    return acc


def quantile_type7(sorted_values: Sequence[Decimal], percentile: Decimal) -> Decimal:
    """Linear-interpolation quantile (R-7, the same rule as NumPy ``linear``).

    ``sorted_values`` must already be sorted ascending; ties keep their input
    order which itself is deterministic (event sequence order).

    Index:  ``h = (n - 1) * p`` with ``p`` in ``[0, 1]``; interpolate between
    the two bracketing order statistics.
    """
    if not sorted_values:
        raise ValueError("quantile of empty sequence")
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    p = percentile / HUNDRED
    with localcontext(make_context()):
        h = ((Decimal(n - 1)) * p)
        lower = int(h.to_integral_value(rounding="ROUND_FLOOR"))
        upper = int(h.to_integral_value(rounding="ROUND_CEILING"))
        if lower == upper:
            return sorted_values[lower]
        fraction = h - Decimal(lower)
        low_value = sorted_values[lower]
        high_value = sorted_values[upper]
        return low_value + (high_value - low_value) * fraction


def encode_decimal(value: Decimal) -> str:
    """Canonical string encoding persisted in SQLite / JSON."""
    return str(value.normalize())


def decode_decimal(text: str) -> Decimal:
    return Decimal(text)


def stable_json_numbers(decimals: List[Decimal]) -> List[str]:
    return [encode_decimal(d) for d in decimals]
