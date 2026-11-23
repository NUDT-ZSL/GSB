from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from elastic_control.aggregation import aggregate_windows
from elastic_control.decision import decide
from elastic_control.engine import load_sample_file
from elastic_control.models import load_policy_from_directory
from elastic_control.scheduler import next_runtime_after_failure, plan_actions


ROOT = Path(__file__).resolve().parents[1]


class SchedulerTests(unittest.TestCase):
    def test_budget_exhaustion_preserves_start_order_and_three_outcomes(self) -> None:
        bundle = load_policy_from_directory(ROOT / "samples")
        sample_bundle = load_sample_file(ROOT / "samples" / "samples.json", bundle)
        windows = aggregate_windows(bundle.metrics, sample_bundle.samples, clock_ms=2000)
        replicas = {
            name: workload.current_replicas for name, workload in bundle.workloads.items()
        }
        output = decide(bundle, windows, replicas, at_ms=2000)
        planning = plan_actions(
            bundle,
            output.workloads.values(),
            round_id="r1",
            at_ms=2000,
            used_cpu_core_ms=Decimal(0),
            used_memory_mib=Decimal(0),
            runtime={name: {} for name in bundle.workloads},
            outcomes={"worker": {"success": False, "reason": "injected"}},
        )
        by_workload = {action.workload: action for action in planning.actions}
        self.assertEqual(by_workload["api-web"].status, "reserved")
        self.assertEqual(by_workload["worker"].status, "reserved")
        self.assertEqual(by_workload["batch"].status, "reserved")
        self.assertEqual(by_workload["archive"].status, "abandoned")
        self.assertEqual(by_workload["archive"].reason, "cpu_budget_exhausted")
        self.assertFalse(by_workload["worker"].outcome_success)
        self.assertEqual(
            [action.workload for action in planning.actions],
            ["api-web", "worker", "batch", "archive"],
        )

    def test_freeze_blocks_only_scale_out(self) -> None:
        bundle = load_policy_from_directory(ROOT / "samples")
        sample_bundle = load_sample_file(ROOT / "samples" / "samples.json", bundle)
        windows = aggregate_windows(bundle.metrics, sample_bundle.samples, clock_ms=5000)
        replicas = {
            "api-web": 5,
            "worker": 2,
            "batch": 4,
            "archive": 2,
        }
        output = decide(bundle, windows, replicas, at_ms=5000)
        planning = plan_actions(
            bundle,
            output.workloads.values(),
            round_id="r2",
            at_ms=5000,
            used_cpu_core_ms=Decimal(0),
            used_memory_mib=Decimal(0),
            runtime={"batch": {"freeze_until_ms": 6000}},
        )
        batch = next(action for action in planning.actions if action.workload == "batch")
        self.assertEqual(batch.status, "not_started")
        self.assertEqual(batch.reason, "freeze_window")

    def test_failure_backoff_uses_logical_clock_and_failure_count(self) -> None:
        bundle = load_policy_from_directory(ROOT / "samples")
        first = next_runtime_after_failure(
            bundle,
            at_ms=2000,
            duration_ms=1000,
            runtime={},
        )
        self.assertEqual(first["consecutive_failures"], 1)
        self.assertEqual(first["backoff_until_ms"], 4000)
        second = next_runtime_after_failure(
            bundle,
            at_ms=5000,
            duration_ms=1000,
            runtime=first,
        )
        self.assertEqual(second["consecutive_failures"], 2)
        self.assertEqual(second["backoff_until_ms"], 8000)


if __name__ == "__main__":
    unittest.main()
