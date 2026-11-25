from __future__ import annotations

import threading
import time
import unittest
from fractions import Fraction

from radapt.fixedpoint import ErrorLedger, frac_str
from radapt.model import RuntimeConfig
from radapt.scheduler import (
    ParallelScheduler,
    ScheduledTask,
    STATUS_ABANDONED_LOGICAL,
    STATUS_ABANDONED_MEMORY,
    STATUS_FAILED,
    STATUS_NOT_STARTED_PARALLEL_LIMIT,
)


def config(parallel=2, logical=100, memory=1000):
    return RuntimeConfig(
        drift_threshold=Fraction(1, 5),
        cooldown_ms=10,
        switch_cost_budget_ms=Fraction(100),
        max_parallel_tasks=parallel,
        logical_time_budget_per_round_ms=Fraction(logical),
        fixed_point_scale=100,
        memory_budget_bytes=memory,
    )


class SchedulerTests(unittest.TestCase):
    def test_parallel_limit_distinguishes_not_started(self) -> None:
        scheduler = ParallelScheduler(config(parallel=2, logical=1000, memory=1000))
        active = 0
        max_active = 0
        lock = threading.Lock()

        def work():
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return "ok"

        tasks = [ScheduledTask(f"t{i}", f"w{i}", 1, 1, work) for i in range(3)]
        report = scheduler.run_round(tasks)
        self.assertEqual(len(report.completed), 2)
        self.assertEqual(len(report.not_started_within_budget), 1)
        self.assertEqual(report.not_started_within_budget[0].status, STATUS_NOT_STARTED_PARALLEL_LIMIT)
        self.assertEqual(max_active, 2)

    def test_logical_budget_abandons_remaining(self) -> None:
        scheduler = ParallelScheduler(config(parallel=4, logical=5, memory=1000))
        tasks = [
            ScheduledTask("a", "w1", 5, 1, lambda: None),
            ScheduledTask("b", "w2", 1, 1, lambda: None),
        ]
        report = scheduler.run_round(tasks)
        self.assertEqual(len(report.completed), 1)
        self.assertEqual(len(report.actively_abandoned), 1)
        self.assertEqual(report.actively_abandoned[0].status, STATUS_ABANDONED_LOGICAL)

    def test_memory_budget_abandons_remaining(self) -> None:
        scheduler = ParallelScheduler(config(parallel=4, logical=100, memory=100))
        tasks = [
            ScheduledTask("a", "w1", 1, 60, lambda: None),
            ScheduledTask("b", "w2", 1, 60, lambda: None),
        ]
        report = scheduler.run_round(tasks)
        self.assertEqual(report.completed[0].task_id, "a")
        self.assertEqual(report.actively_abandoned[0].status, STATUS_ABANDONED_MEMORY)

    def test_failure_after_start_is_separate(self) -> None:
        scheduler = ParallelScheduler(config(parallel=2, logical=100, memory=1000))

        def boom():
            raise RuntimeError("boom")

        tasks = [
            ScheduledTask("bad", "w1", 1, 1, boom),
            ScheduledTask("good", "w2", 1, 1, lambda: 42),
        ]
        report = scheduler.run_round(tasks)
        self.assertEqual(len(report.failed_after_start), 1)
        self.assertEqual(report.failed_after_start[0].status, STATUS_FAILED)
        self.assertTrue(report.failed_after_start[0].started)
        self.assertEqual(report.completed[0].result, 42)


class FixedPointTests(unittest.TestCase):
    def test_conversion_records_direction_and_remainder(self) -> None:
        ledger = ErrorLedger(100)
        value = ledger.convert(Fraction(1, 3), subject="x", kind="elapsed_ms")
        self.assertEqual(value.integer, 33)
        self.assertEqual(value.rounding, "floor_down")
        self.assertEqual(frac_str(value.discarded_remainder), "1/300")
        self.assertEqual(value.error_bound, Fraction(1, 100))

    def test_add_and_mul_error_bounds_accumulate(self) -> None:
        ledger = ErrorLedger(1000)
        a = ledger.convert(Fraction(1, 3), subject="a", kind="cost")
        b = ledger.convert(Fraction(1, 7), subject="b", kind="cost")
        added = ledger.add([a, b], subject="sum")
        multiplied = ledger.multiply(a, b, subject="product")
        self.assertGreater(added.error_bound, Fraction(0))
        self.assertGreater(multiplied.error_bound, Fraction(0))
        self.assertGreaterEqual(ledger.cumulative_add_error_bound, added.error_bound)
        self.assertGreaterEqual(ledger.cumulative_mul_error_bound, multiplied.error_bound)
        chain = ledger.source_chain([added, multiplied])
        self.assertIn("participating_batches", chain)
        self.assertTrue(chain["conversion_ids"])


if __name__ == "__main__":
    unittest.main()
