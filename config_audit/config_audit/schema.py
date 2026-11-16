"""Static schema knowledge: supported units, sources, default numeric bounds.

These tables are the single source of truth used by the strong loader and by
range validation.  They are plain immutable constants so that validation never
depends on read order.
"""

from __future__ import annotations

import re
from decimal import Decimal

# Human entry vs. upstream derivation.
SOURCE_MANUAL = "manual"
SOURCE_DERIVED = "derived"
SUPPORTED_SOURCES = frozenset({SOURCE_MANUAL, SOURCE_DERIVED})

# unit -> (inclusive lower bound, inclusive upper bound) in the unit itself.
# Bounds are expressed as Decimals so range checks are exact.
DEFAULT_UNIT_BOUNDS: dict[str, tuple[Decimal, Decimal]] = {
    "ms": (Decimal("0"), Decimal("3600000")),          # timeout, 0 .. 1h
    "count": (Decimal("0"), Decimal("1000000")),       # retry count
    "connections": (Decimal("0"), Decimal("100000")),  # concurrency cap
    "ratio": (Decimal("0"), Decimal("1")),             # sampling rate
    "hz": (Decimal("0"), Decimal("1000000")),          # refresh interval
    "s": (Decimal("0"), Decimal("86400")),             # interval seconds
}
SUPPORTED_UNITS = frozenset(DEFAULT_UNIT_BOUNDS.keys())

# A legal key: lowercase identifier segments separated by '.', e.g.
# "http.timeout" or "retry.count".  Length bounded to keep reports compact.
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
MAX_KEY_LENGTH = 64

# JSON numeric types accepted by the loader.  Booleans are explicitly excluded
# even though ``bool`` is an ``int`` subclass in Python.
ACCEPTED_NUMBER_TYPES = (int, float, Decimal)


def is_valid_key(key: str) -> bool:
    return (
        isinstance(key, str)
        and 0 < len(key) <= MAX_KEY_LENGTH
        and KEY_PATTERN.match(key) is not None
    )


def bounds_for(unit: str, override: tuple[Decimal, Decimal] | None = None
               ) -> tuple[Decimal, Decimal]:
    """Return the inclusive (low, high) bounds for an item.

    An item-level explicit bound overrides the unit default, but must itself be
    well ordered (low <= high).
    """
    if override is not None:
        low, high = override
        if low > high:
            raise ValueError(f"inverted bounds for unit {unit}: {low} > {high}")
        return low, high
    return DEFAULT_UNIT_BOUNDS[unit]
