"""Stage-level fixed-point aggregation and SLO verdict.

Aggregation caliber (version agg-v1), all outputs are integer fixed-point:

  success_rate_bp : floor(10000 * success / total) in basis points.
                    Quantization error bound: < 1 bp (= 0.01 percentage point)
                    in the downward direction; exact when total=0.
  p95_latency_ms  : nearest-rank percentile on integer-ms latencies.
                    Rank r = ceil(0.95 * n), value = sorted_latency[r-1].
                    Latencies are already integer ms (monotonic-clock deltas),
                    so the quantization error bound of the percentile value is
                    <= 1 ms relative to the real-valued latency.
  throughput_rps  : floor(total_completed / elapsed_seconds), where
                    elapsed_seconds = duration_ms / 1000 measured on the
                    monotonic clock. Quantization error bound: < 1 rps
                    downward; additionally duration itself is bounded by
                    +/- 1 ms (integer truncation at both endpoints).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import AGGREGATION_VERSION
from .models import (Observation, SloThresholds, StageMetrics,
                     SATURATION_BOTH, SATURATION_FAILURE_RATE,
                     SATURATION_LATENCY, VERDICT_PASS, VERDICT_SATURATED)

QUANT_BOUNDS = {
    "success_rate_bp": "truncation_error<1bp(0.01pp),downward",
    "p95_latency_ms": "<=1ms integer-latency quantization; nearest-rank exact",
    "throughput_rps": "floor error<1rps downward; duration_ms bounded +-1ms",
}


def aggregate(observation: Observation) -> StageMetrics:
    success = observation.total_success
    failure = observation.total_failure
    total = success + failure
    duration_ms = max(observation.duration_ms, 1)

    success_rate_bp = (10000 * success // total) if total else 0

    all_latency: list[int] = []
    for sample in observation.samples:
        all_latency.extend(sample.latency_ms)
    p95 = _nearest_rank_p95(all_latency)

    elapsed_s = duration_ms / 1000.0
    throughput = int(math.floor(total / elapsed_s)) if elapsed_s > 0 else 0

    return StageMetrics(
        success_rate_bp=success_rate_bp,
        p95_latency_ms=p95,
        throughput_rps=throughput,
        total_requests=total,
        duration_ms=duration_ms,
        quantization_error_bounds=dict(QUANT_BOUNDS),
        aggregation_version=AGGREGATION_VERSION,
    )


def _nearest_rank_p95(latencies: list[int]) -> int:
    if not latencies:
        return 0
    arr = np.asarray(latencies, dtype=np.int64)
    n = arr.size
    rank = max(1, math.ceil(0.95 * n))
    return int(np.partition(arr, rank - 1)[rank - 1])


@dataclass
class Verdict:
    verdict: str
    saturation_reason: str | None
    triggered_metrics: dict


def judge(metrics: StageMetrics, slo: SloThresholds) -> Verdict:
    actual_sr = metrics.success_rate_bp
    actual_p95 = metrics.p95_latency_ms
    failure_over = actual_sr < slo.min_success_rate_bp
    latency_over = actual_p95 > slo.max_p95_latency_ms

    triggered: dict = {}
    if failure_over:
        triggered["success_rate_bp"] = {
            "observed": actual_sr,
            "threshold_min": slo.min_success_rate_bp,
        }
    if latency_over:
        triggered["p95_latency_ms"] = {
            "observed": actual_p95,
            "threshold_max": slo.max_p95_latency_ms,
        }

    if failure_over and latency_over:
        return Verdict(VERDICT_SATURATED, SATURATION_BOTH, triggered)
    if failure_over:
        return Verdict(VERDICT_SATURATED, SATURATION_FAILURE_RATE, triggered)
    if latency_over:
        return Verdict(VERDICT_SATURATED, SATURATION_LATENCY, triggered)
    return Verdict(VERDICT_PASS, None, triggered)
