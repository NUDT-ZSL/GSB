from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any, Iterable

from .util import fraction_dict, fraction_str


def round_half_up_fraction(value: Fraction) -> tuple[int, Fraction, str]:
    floor_value = value.numerator // value.denominator
    remainder = value - floor_value
    if remainder == 0:
        return floor_value, Fraction(0), "exact"
    if remainder * 2 >= 1:
        result = floor_value + 1
        return result, (value - result), "up"
    return floor_value, (value - floor_value), "down"


def quantize_decimal_to_scale(value: Decimal, scale: int) -> tuple[int, Fraction, str]:
    return round_half_up_fraction(Fraction(value) * scale)


@dataclass
class LedgerEntry:
    scope_key: str
    local_seq: int
    kind: str
    operation: str
    scale: int
    exact_num: int
    exact_den: int
    result_scaled: int
    discarded: Fraction
    rounding_direction: str
    conversion_error: Fraction
    cumulative_error: Fraction
    source: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "local_seq": self.local_seq,
            "kind": self.kind,
            "operation": self.operation,
            "scale": self.scale,
            "exact": fraction_dict(Fraction(self.exact_num, self.exact_den)),
            "result_scaled": self.result_scaled,
            "discarded": fraction_dict(self.discarded),
            "rounding_direction": self.rounding_direction,
            "conversion_error": fraction_dict(self.conversion_error),
            "cumulative_error": fraction_dict(self.cumulative_error),
            "source": self.source,
            "details": self.details,
        }


class LedgerBuilder:
    def __init__(self, scope_key: str, start_local_seq: int = 0) -> None:
        self.scope_key = scope_key
        self.entries: list[LedgerEntry] = []
        self._next_seq = start_local_seq

    def _append(
        self,
        kind: str,
        operation: str,
        scale: int,
        exact: Fraction,
        result_scaled: int,
        discarded: Fraction,
        rounding_direction: str,
        conversion_error: Fraction,
        cumulative_error: Fraction,
        source: str,
        details: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        entry = LedgerEntry(
            scope_key=self.scope_key,
            local_seq=self._next_seq,
            kind=kind,
            operation=operation,
            scale=scale,
            exact_num=exact.numerator,
            exact_den=exact.denominator,
            result_scaled=result_scaled,
            discarded=discarded,
            rounding_direction=rounding_direction,
            conversion_error=conversion_error,
            cumulative_error=cumulative_error,
            source=source,
            details=details or {},
        )
        self._next_seq += 1
        self.entries.append(entry)
        return entry

    def add_exact_count(self, kind: str, value: int, source: str, **details: Any) -> LedgerEntry:
        exact = Fraction(value)
        return self._append(
            kind=kind,
            operation="integer_count",
            scale=1,
            exact=exact,
            result_scaled=value,
            discarded=Fraction(0),
            rounding_direction="exact",
            conversion_error=Fraction(0),
            cumulative_error=Fraction(0),
            source=source,
            details=details,
        )

    def add_conversion(
        self,
        kind: str,
        exact_value: Decimal | Fraction,
        scale: int,
        source: str,
        operation: str = "fixed_scale_round",
        prior_error: Fraction = Fraction(0),
        **details: Any,
    ) -> LedgerEntry:
        exact_fraction = Fraction(exact_value)
        scaled = exact_fraction * scale
        result, discarded, direction = round_half_up_fraction(scaled)
        conversion_error = abs(scaled - result)
        cumulative = prior_error + conversion_error
        return self._append(
            kind=kind,
            operation=operation,
            scale=scale,
            exact=scaled,
            result_scaled=result,
            discarded=discarded,
            rounding_direction=direction,
            conversion_error=conversion_error,
            cumulative_error=cumulative,
            source=source,
            details=details,
        )

    def add_ratio(
        self,
        kind: str,
        numerator: int,
        denominator: int,
        scale: int,
        source: str,
        **details: Any,
    ) -> LedgerEntry:
        exact = Fraction(numerator, denominator) * scale
        result, discarded, direction = round_half_up_fraction(exact)
        conversion_error = abs(exact - result)
        return self._append(
            kind=kind,
            operation="divide_round_half_up",
            scale=scale,
            exact=exact,
            result_scaled=result,
            discarded=discarded,
            rounding_direction=direction,
            conversion_error=conversion_error,
            cumulative_error=conversion_error,
            source=source,
            details={"numerator": numerator, "denominator": denominator, **details},
        )

    def add_p95(
        self,
        kind: str,
        samples: Iterable[tuple[str, Decimal]],
        scale: int,
        source: str,
        percentile_num: int = 95,
        percentile_den: int = 100,
        **details: Any,
    ) -> LedgerEntry | None:
        sample_entries: list[tuple[str, str, int, Fraction]] = []
        for request_id, raw_latency in samples:
            entry = self.add_conversion(
                kind="latency_sample",
                exact_value=raw_latency,
                scale=scale,
                source=source,
                operation="fixed_scale_round",
                request_id=request_id,
            )
            sample_entries.append((request_id, raw_latency if isinstance(raw_latency, str) else str(raw_latency), entry.result_scaled, entry.conversion_error))
        if not sample_entries:
            return None
        ordered = sorted(sample_entries, key=lambda item: (item[2], item[0]))
        count = len(ordered)
        position = Fraction((count - 1) * percentile_num, percentile_den)
        lower = position.numerator // position.denominator
        fraction = position - lower
        upper = min(lower + 1, count - 1)
        low_value = ordered[lower][2]
        high_value = ordered[upper][2]
        exact_interpolated = Fraction(low_value) + Fraction(high_value - low_value) * fraction
        result, discarded, direction = round_half_up_fraction(exact_interpolated)
        low_error = ordered[lower][3]
        high_error = ordered[upper][3]
        interpolation_error = (1 - fraction) * low_error + fraction * high_error
        rounding_error = abs(exact_interpolated - result)
        cumulative = interpolation_error + rounding_error
        low_seq = self._find_sample_seq(ordered[lower][0])
        high_seq = self._find_sample_seq(ordered[upper][0])
        return self._append(
            kind=kind,
            operation="sort_interpolate_round_half_up",
            scale=scale,
            exact=exact_interpolated,
            result_scaled=result,
            discarded=discarded,
            rounding_direction=direction,
            conversion_error=rounding_error,
            cumulative_error=cumulative,
            source=source,
            details={
                "sample_count": count,
                "percentile_num": percentile_num,
                "percentile_den": percentile_den,
                "position": fraction_str(position),
                "lower_index": lower,
                "upper_index": upper,
                "lower_value_scaled": low_value,
                "upper_value_scaled": high_value,
                "lower_request_id": ordered[lower][0],
                "upper_request_id": ordered[upper][0],
                "lower_ledger_local_seq": low_seq,
                "upper_ledger_local_seq": high_seq,
                "interpolation_error": fraction_dict(interpolation_error),
                **details,
            },
        )

    def _find_sample_seq(self, request_id: str) -> int | None:
        for entry in reversed(self.entries):
            if entry.kind == "latency_sample" and entry.details.get("request_id") == request_id:
                return entry.local_seq
        return None


def scene_config_ledger(scene) -> list[LedgerEntry]:
    builder = LedgerBuilder("scene-config")
    precision = scene.precision
    for tenant in sorted(scene.tenants.values(), key=lambda item: item.name):
        builder.add_conversion(
            kind="slo_success_rate_min",
            exact_value=tenant.success_rate_min,
            scale=precision.percent_scale,
            source=tenant.source,
            tenant=tenant.name,
        )
        builder.add_conversion(
            kind="slo_p95_latency_max",
            exact_value=tenant.p95_latency_max_ms,
            scale=precision.latency_scale,
            source=tenant.source,
            tenant=tenant.name,
        )
        builder.add_exact_count(
            kind="tenant_concurrency_limit",
            value=tenant.concurrency_limit,
            source=tenant.source,
            tenant=tenant.name,
        )
    builder.add_conversion(
        kind="compare_tolerance_success",
        exact_value=precision.compare_tolerance.success_rate,
        scale=precision.percent_scale,
        source="precision.compare_tolerance.success_rate",
    )
    builder.add_conversion(
        kind="compare_tolerance_p95",
        exact_value=precision.compare_tolerance.p95_latency_ms,
        scale=precision.latency_scale,
        source="precision.compare_tolerance.p95_latency_ms",
    )
    builder.add_conversion(
        kind="degraded_margin_success",
        exact_value=precision.compare_tolerance.degraded_success_rate,
        scale=precision.percent_scale,
        source="precision.degraded_margin.success_rate",
    )
    builder.add_conversion(
        kind="degraded_margin_p95",
        exact_value=precision.compare_tolerance.degraded_p95_latency_ms,
        scale=precision.latency_scale,
        source="precision.degraded_margin.p95_latency_ms",
    )
    builder.add_conversion(
        kind="error_budget_success",
        exact_value=precision.error_budget.success_rate,
        scale=precision.percent_scale,
        source="precision.error_budget.success_rate",
    )
    builder.add_conversion(
        kind="error_budget_p95",
        exact_value=precision.error_budget.p95_latency_ms,
        scale=precision.latency_scale,
        source="precision.error_budget.p95_latency_ms",
    )
    return builder.entries


def scaled_tolerance(value: Decimal, scale: int) -> int:
    result, _, _ = quantize_decimal_to_scale(value, scale)
    return result

