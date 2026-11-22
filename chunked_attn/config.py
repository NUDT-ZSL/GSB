"""Numerical stability configuration (requirement 5)."""
from __future__ import annotations

from dataclasses import dataclass, asdict

ROUND_NEAREST_EVEN = "nearest_even"
ROUND_TOWARD_ZERO = "toward_zero"
ROUNDING_MODES = (ROUND_NEAREST_EVEN, ROUND_TOWARD_ZERO)

# Declared, explicit adjudication order for equal scores / equal positions
# (requirement 1): ties are always broken by ascending index.
TIE_BREAK_INDEX_ASCENDING = "index_ascending"


@dataclass(frozen=True)
class StabilityConfig:
    """Configuration for the float64 online-softmax pipeline.

    subnormal_protection: flush subnormal float64 intermediates to zero.
    rounding_mode:        rounding applied when casting float64 outputs to
                          the manifest dtype ("nearest_even" | "toward_zero").
    tolerance:            declared tolerance for softmax-denominator
                          reconstruction from per-block statistics.
    tie_break:            explicit tie-break rule, fixed to index ascending.
    """

    subnormal_protection: bool = True
    rounding_mode: str = ROUND_NEAREST_EVEN
    tolerance: float = 1e-12
    tie_break: str = TIE_BREAK_INDEX_ASCENDING

    def to_dict(self):
        return asdict(self)
