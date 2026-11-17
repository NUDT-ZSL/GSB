"""Integer fixed-point latency aggregation with an explicit clock basis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class LatencyMetrics:
    budget_ms: float = 5.0
    samples_us: list[int] = field(default_factory=list)

    @staticmethod
    def elapsed_us(start_ns: int, end_ns: int) -> int:
        if end_ns < start_ns:
            raise ValueError("monotonic clock moved backwards")
        # Round to nearest microsecond.  The absolute quantisation error is
        # therefore at most 0.5 us; 1 us is reported as a conservative bound.
        return (end_ns - start_ns + 500) // 1000

    def record(self, elapsed_us: int) -> dict[str, object]:
        value = max(0, int(elapsed_us))
        self.samples_us.append(value)
        exceeded = value > int(round(self.budget_ms * 1000))
        return {
            "elapsed_us": value,
            "budget_us": int(round(self.budget_ms * 1000)),
            "budget_exceeded": exceeded,
            "degradation_reason": "fast_path_budget_exceeded" if exceeded else None,
        }

    @staticmethod
    def _nearest_rank(sorted_values: list[int], percentile: int) -> int:
        if not sorted_values:
            return 0
        # Nearest-rank P95: ceil(0.95*n), no interpolation.
        index = (percentile * len(sorted_values) + 99) // 100 - 1
        return sorted_values[max(0, min(index, len(sorted_values) - 1))]

    def aggregate(self) -> dict[str, object]:
        values = sorted(self.samples_us)
        count = len(values)
        total = sum(values)
        mean = (total + count // 2) // count if count else 0
        return {
            "unit": "microseconds",
            "fixed_point_scale": 1,
            "sample_count": count,
            "mean_fixed_us": mean,
            "p95_fixed_us": self._nearest_rank(values, 95),
            "max_fixed_us": values[-1] if values else 0,
            "quantization_error_upper_bound_us": 1,
            "clock_basis": "time.perf_counter_ns (local monotonic counter)",
            "truth_source": "local monotonic counter; latency observations never alter visibility facts",
            "budget_ms": self.budget_ms,
        }

    def extend(self, elapsed_values: Iterable[int]) -> None:
        self.samples_us.extend(max(0, int(value)) for value in elapsed_values)
