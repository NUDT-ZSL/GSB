import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from capbench.metrics import aggregate, judge, _nearest_rank_p95
from capbench.models import (Observation, Sample, SloThresholds)
from capbench.clock import CLOCK_BASIS


def observation(latencies, success, failure, start=0, end=1000):
    samples = [Sample(1, 1, 250, success, failure, list(latencies))]
    return Observation(1, start, end, samples, success, failure,
                       CLOCK_BASIS, "receipt")


class MetricsTest(unittest.TestCase):
    def test_fixed_point_success_rate_bounds(self):
        obs = observation([10] * 99 + [20], success=99, failure=1)
        m = aggregate(obs)
        self.assertEqual(m.success_rate_bp, 9900)
        self.assertIn("success_rate_bp", m.quantization_error_bounds)
        self.assertEqual(m.aggregation_version, "agg-v1")

    def test_truncation_error_under_one_bp(self):
        obs = observation([5], success=1, failure=3, end=1000)
        m = aggregate(obs)
        self.assertEqual(m.success_rate_bp, 2500)  # floor(10000/4)

    def test_nearest_rank_p95(self):
        values = list(range(1, 101))  # 1..100, rank = ceil(95)=95
        self.assertEqual(_nearest_rank_p95(values), 95)
        self.assertEqual(_nearest_rank_p95([42]), 42)
        self.assertEqual(_nearest_rank_p95([]), 0)

    def test_throughput_floor_and_duration(self):
        obs = observation([5] * 25, success=25, failure=0,
                          start=0, end=2000)
        m = aggregate(obs)
        self.assertEqual(m.throughput_rps, 12)  # floor(25 / 2.0)

    def test_verdict_failure_rate_reason(self):
        slo = SloThresholds(9900, 1000)
        obs = observation([5] * 90, success=90, failure=10)
        verdict = judge(aggregate(obs), slo)
        self.assertEqual(verdict.verdict, "saturated")
        self.assertEqual(verdict.saturation_reason, "failure_rate_exceeded")
        self.assertIn("success_rate_bp", verdict.triggered_metrics)

    def test_verdict_latency_reason(self):
        slo = SloThresholds(9000, 10)
        obs = observation([5] * 90 + [50] * 10, success=100, failure=0)
        verdict = judge(aggregate(obs), slo)
        self.assertEqual(verdict.saturation_reason, "latency_p95_exceeded")
        self.assertIn("p95_latency_ms", verdict.triggered_metrics)

    def test_verdict_both_reasons(self):
        slo = SloThresholds(9900, 10)
        obs = observation([50] * 80, success=80, failure=20)
        verdict = judge(aggregate(obs), slo)
        self.assertEqual(verdict.saturation_reason,
                         "both_failure_rate_and_latency")


if __name__ == "__main__":
    unittest.main()
