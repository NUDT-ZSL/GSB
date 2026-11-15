from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Iterable


# Unit roundoff u = epsilon / 2 for IEEE-754 binary64 round-to-nearest.
UNIT_ROUNDOFF = sys.float_info.epsilon / 2.0


def gamma(k: int) -> float:
    """Classical floating-point summation factor k*u/(1-k*u)."""

    if k < 0:
        raise ValueError("k must be non-negative")
    ku = k * UNIT_ROUNDOFF
    if ku >= 1.0:
        return math.inf
    return ku / (1.0 - ku)


@dataclass(frozen=True, slots=True)
class AccumulatorState:
    """A persisted Neumaier compensated-sum state.

    Inputs are replayed in the canonical event-key order by the aggregation
    layer. Consequently the complete state, not only the final value, is
    independent of network/arrival order.
    """

    total: float = 0.0
    compensation: float = 0.0
    absolute_sum: float = 0.0
    count: int = 0

    @classmethod
    def from_terms(cls, terms: Iterable[float]) -> "AccumulatorState":
        total = 0.0
        compensation = 0.0
        absolute_sum = 0.0
        count = 0
        for term in terms:
            value = float(term)
            updated = total + value
            if abs(total) >= abs(value):
                compensation += (total - updated) + value
            else:
                compensation += (value - updated) + total
            total = updated
            absolute_sum += abs(value)
            count += 1
        return cls(total, compensation, absolute_sum, count)

    @property
    def value(self) -> float:
        return self.total + self.compensation

    def error_bound(self, input_relative_error: float = 0.0) -> float:
        """Conservative absolute error envelope for this compensated sum.

        The envelope combines the n-dependent summation factor with an error in
        the rounded input terms (for example x*x or w*x). It is derived from the
        binary64 unit roundoff and the observed absolute magnitude; no tuned
        constant is involved.
        """

        if self.count == 0:
            return 0.0
        g = gamma(self.count + 2)
        if math.isinf(g):
            return math.inf
        if input_relative_error < 0.0 or input_relative_error >= 1.0:
            raise ValueError("input_relative_error must be in [0, 1)")
        input_factor = input_relative_error / (1.0 - input_relative_error)

        # absolute_sum is itself a sequential float sum. Bound its own relative
        # rounding before applying the compensated-sum term.
        magnitude_gamma = gamma(self.count)
        if math.isinf(magnitude_gamma):
            return math.inf
        magnitude = math.nextafter(
            self.absolute_sum / (1.0 - magnitude_gamma), math.inf
        )
        return (g + input_factor) * magnitude + UNIT_ROUNDOFF * abs(self.value)
