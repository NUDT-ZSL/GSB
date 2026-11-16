"""In-memory data model for archives and quantized configuration items."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Formula:
    """A linear derivation: value = sum(coef[k] * upstream[k]) + constant."""

    coefficients: dict[str, Decimal]
    constant: Decimal
    # Ordered list of upstream keys; deterministic and independent of dict order.
    upstream: tuple[str, ...]


@dataclass(frozen=True)
class ConfigItem:
    key: str
    value: Decimal | None           # None only for a derived item
    unit: str
    source: str                     # "manual" | "derived"
    formula: Formula | None
    bounds: tuple[Decimal, Decimal]
    location: str                   # JSON-pointer-ish position inside its archive


@dataclass(frozen=True)
class Archive:
    node_id: str
    version: str
    items: dict[str, ConfigItem]    # key -> item, unique within the archive

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self.items))


@dataclass(frozen=True)
class QuantizationRecord:
    """One integer-grid quantization with its signed residual and error bounds.

    Stored in integer grid units (not scaled real numbers) so that the whole
    audit can be reasoned about purely over integers.
    """

    raw: str                 # exact Decimal input rendered as string
    quantized: int           # representative on the integer grid
    down_error: int          # nonnegative integer grid steps (>= 0)
    up_error: int            # nonnegative integer grid steps (>= 0)
    rounding: str            # rounding rule name (e.g. HALF_EVEN)


@dataclass(frozen=True)
class QuantizedItem:
    node_id: str
    key: str
    unit: str
    source: str
    qvalue: int
    down_error: int
    up_error: int
    location: str
    # Per-input quantization trace (value / coefficient / constant), keyed by a
    # stable label so the final report can reconstruct every integer-grid step.
    trace: dict[str, QuantizationRecord] = field(default_factory=dict)
