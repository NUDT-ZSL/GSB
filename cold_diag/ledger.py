from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any


@dataclass(frozen=True)
class PrecisionMarker:
    seq_a: int
    seq_b: int
    wait_a_ms: int
    wait_b_ms: int
    difference_ms: int
    tolerance_ms: int
    order_depends_on_difference: bool
    sensitive: bool
    exact_order: str
    fixed_order: str
    resolution: str
    entry_ids: tuple[int, int] = field(default_factory=tuple)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq_a": self.seq_a,
            "seq_b": self.seq_b,
            "wait_a_ms": self.wait_a_ms,
            "wait_b_ms": self.wait_b_ms,
            "difference_ms": self.difference_ms,
            "tolerance_ms": self.tolerance_ms,
            "order_depends_on_difference": self.order_depends_on_difference,
            "sensitive": self.sensitive,
            "exact_order": self.exact_order,
            "fixed_order": self.fixed_order,
            "resolution": self.resolution,
            "entry_ids": list(self.entry_ids),
            "reason": self.reason,
        }


class FixedPointLedger:
    """Exact conversion ledger.

    ``scale`` is the fixed-point multiplier for a duration expressed in
    ``unit_ms`` units.  The sample declaration ``unit_ms=1000, scale=100``
    therefore means hundredths of a second; one fixed integer is 10 ms.
    """

    def __init__(
        self,
        scale: int = 100,
        unit_ms: int = 1000,
        default_rounding: str = "floor",
        tolerance_ms: int = 10,
    ) -> None:
        if not isinstance(scale, int) or scale <= 0:
            raise ValueError("scale must be a positive integer")
        if not isinstance(unit_ms, int) or unit_ms <= 0:
            raise ValueError("unit_ms must be a positive integer")
        if default_rounding not in {"floor", "ceil", "nearest"}:
            raise ValueError("rounding must be floor, ceil, or nearest")
        if tolerance_ms < 0:
            raise ValueError("tolerance_ms must be non-negative")
        self.scale = scale
        self.unit_ms = unit_ms
        self.default_rounding = default_rounding
        self.tolerance_ms = tolerance_ms
        self.entries: list[dict[str, Any]] = []
        self.markers: list[PrecisionMarker] = []
        self._error_bound = Fraction(0, 1)

    @property
    def quantum_ms(self) -> Fraction:
        return Fraction(self.unit_ms, self.scale)

    @property
    def cumulative_error_bound_ms(self) -> str:
        return self._format_fraction(self._error_bound)

    @property
    def cumulative_abs_rounding_error_ms(self) -> str:
        total = sum((Fraction(item["rounding_error_ms"]) for item in self.entries), Fraction(0, 1))
        return self._format_fraction(abs(total))

    @staticmethod
    def _format_fraction(value: Fraction) -> str:
        if value.denominator == 1:
            return str(value.numerator)
        return f"{value.numerator}/{value.denominator}"

    def convert(
        self,
        duration_ms: int,
        *,
        label: str = "",
        rounding: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative integer")
        direction = rounding or self.default_rounding
        if direction not in {"floor", "ceil", "nearest"}:
            raise ValueError("rounding must be floor, ceil, or nearest")

        exact_scaled = Fraction(duration_ms * self.scale, self.unit_ms)
        floor_int = exact_scaled.numerator // exact_scaled.denominator
        fractional = exact_scaled - floor_int
        if direction == "floor":
            fixed_int = floor_int
        elif direction == "ceil":
            fixed_int = floor_int + (1 if fractional > 0 else 0)
        else:
            if fractional >= Fraction(1, 2):
                fixed_int = floor_int + 1
            else:
                fixed_int = floor_int

        reconstructed = Fraction(fixed_int * self.unit_ms, self.scale)
        rounding_error = Fraction(duration_ms) - reconstructed
        discarded = fractional * self.quantum_ms
        if direction == "nearest":
            bound = self.quantum_ms / 2
        else:
            bound = self.quantum_ms
        self._error_bound += bound

        entry = {
            "entry_id": len(self.entries) + 1,
            "label": label,
            "source_duration_ms": duration_ms,
            "scale": self.scale,
            "unit_ms": self.unit_ms,
            "rounding_direction": direction,
            "exact_scaled_value": self._format_fraction(exact_scaled),
            "fixed_point_integer": fixed_int,
            "fractional_remainder_scaled": self._format_fraction(fractional),
            "discarded_remainder_ms": self._format_fraction(discarded),
            "rounding_error_ms": self._format_fraction(rounding_error),
            "absolute_error_bound_ms": self._format_fraction(bound),
            "cumulative_error_bound_ms": self.cumulative_error_bound_ms,
        }
        self.entries.append(entry)
        return entry

    def assess_wait_pair(
        self,
        seq_a: int,
        wait_a_ms: int,
        seq_b: int,
        wait_b_ms: int,
        *,
        order_depends_on_difference: bool = True,
        tolerance_ms: int | None = None,
    ) -> PrecisionMarker:
        tolerance = self.tolerance_ms if tolerance_ms is None else tolerance_ms
        difference = abs(wait_a_ms - wait_b_ms)
        entry_a = self.convert(wait_a_ms, label=f"wait-seq-{seq_a}")
        entry_b = self.convert(wait_b_ms, label=f"wait-seq-{seq_b}")

        def order(first_wait: int, second_wait: int) -> str:
            if first_wait < second_wait:
                return "first_before_second"
            if first_wait > second_wait:
                return "second_before_first"
            return "tie"

        exact_order = order(wait_a_ms, wait_b_ms)
        fixed_a = int(entry_a["fixed_point_integer"])
        fixed_b = int(entry_b["fixed_point_integer"])
        fixed_order = order(fixed_a, fixed_b)
        close = 0 < difference < tolerance
        sensitive = bool(close and order_depends_on_difference)
        if sensitive:
            if fixed_order == "tie":
                resolution = "fixed_point_tie_use_stable_request_seq"
                reason = "sub-tolerance wait difference collapses after fixed-point conversion"
            elif fixed_order != exact_order:
                resolution = "fixed_point_reversal_use_exact_ledger_and_request_seq"
                reason = "fixed-point conversion reverses the wait order"
            else:
                resolution = "mark_sensitive_keep_priority_then_request_seq"
                reason = "wait difference is below tolerance and participates in ordering"
        else:
            resolution = "not_precision_sensitive"
            reason = (
                "wait difference is zero or not below tolerance, or order does not depend on it"
            )

        marker = PrecisionMarker(
            seq_a=seq_a,
            seq_b=seq_b,
            wait_a_ms=wait_a_ms,
            wait_b_ms=wait_b_ms,
            difference_ms=difference,
            tolerance_ms=tolerance,
            order_depends_on_difference=order_depends_on_difference,
            sensitive=sensitive,
            exact_order=exact_order,
            fixed_order=fixed_order,
            resolution=resolution,
            entry_ids=(int(entry_a["entry_id"]), int(entry_b["entry_id"])),
            reason=reason,
        )
        self.markers.append(marker)
        return marker

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "unit_ms": self.unit_ms,
            "quantum_ms": self._format_fraction(self.quantum_ms),
            "tolerance_ms": self.tolerance_ms,
            "entries": list(self.entries),
            "markers": [
                marker.to_dict() if hasattr(marker, "to_dict") else marker
                for marker in self.markers
            ],
            "cumulative_error_bound_ms": self.cumulative_error_bound_ms,
            "cumulative_abs_rounding_error_ms": self.cumulative_abs_rounding_error_ms,
        }
