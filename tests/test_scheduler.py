from __future__ import annotations

import unittest

from ifdata.errors import (
    BusinessError,
    E_COMPARE_BUDGET,
    E_CONCURRENCY_LIMIT,
    E_MEMORY_LIMIT,
    E_WORKER_FAILURE,
)
from ifdata.scheduler import (
    plan_admission,
    run_worker_batches,
    select_worker_count,
)
from ifdata.types import BatchEstimate, BudgetLimits


class SchedulerTests(unittest.TestCase):
    def test_primary_error_follows_declared_priority(self):
        batch = BatchEstimate("b1", 5, 100, 1000, 2)
        plan = plan_admission(
            [batch],
            BudgetLimits(1, 50, 800),
        )
        self.assertEqual(plan.rejected[0]["code"], E_CONCURRENCY_LIMIT)
        self.assertEqual(
            len(plan.rejected[0]["all_violations"]),
            3,
        )
        plan = plan_admission(
            [batch],
            BudgetLimits(
                1,
                50,
                800,
                error_priority=(
                    E_MEMORY_LIMIT,
                    E_COMPARE_BUDGET,
                    E_CONCURRENCY_LIMIT,
                ),
            ),
        )
        self.assertEqual(plan.rejected[0]["code"], E_MEMORY_LIMIT)

    def test_admission_subset_keeps_cost_accounting(self):
        batches = [
            BatchEstimate("b1", 2, 40, 100, 2),
            BatchEstimate("b2", 2, 70, 100, 2),
            BatchEstimate("b3", 2, 10, 100, 2),
        ]
        plan = plan_admission(
            batches,
            BudgetLimits(2, 60, 100),
        )
        self.assertEqual(
            [item.batch_id for item in plan.admitted], ["b1", "b3"]
        )
        self.assertEqual(plan.rejected[0]["code"], E_COMPARE_BUDGET)
        self.assertEqual(plan.comparisons_used, 50)

    def test_worker_count_is_reduced_to_fit_memory(self):
        workers, decisions = select_worker_count(
            requested_workers=3,
            limits=BudgetLimits(4, 100, 600),
            base_memory_bytes=100,
            per_worker_memory_bytes=200,
            stage="quality",
        )
        self.assertEqual(workers, 2)
        rejected = [
            item for item in decisions if item["status"] == "rejected"
        ]
        self.assertEqual(rejected[0]["candidate_workers"], 3)
        self.assertEqual(rejected[0]["code"], E_MEMORY_LIMIT)

    def test_worker_exception_has_stage_and_batch(self):
        def bad_worker(items):
            raise RuntimeError("worker exploded")

        with self.assertRaises(BusinessError) as context:
            run_worker_batches(
                [1, 2, 3],
                bad_worker,
                stage="quality",
                batch_size=2,
                workers=2,
            )
        exc = context.exception
        self.assertEqual(exc.code, E_WORKER_FAILURE)
        self.assertEqual(exc.stage, "quality")
        self.assertEqual(exc.batch_id, "quality-batch-0001")

    def test_injected_failpoint_is_attributed(self):
        with self.assertRaises(BusinessError) as context:
            run_worker_batches(
                [1, 2],
                lambda items: items,
                stage="dedup",
                batch_size=1,
                workers=1,
                failpoints={"dedup:2": "injected stop"},
            )
        self.assertEqual(context.exception.code, E_WORKER_FAILURE)
        self.assertEqual(context.exception.batch_id, "dedup-batch-0002")


if __name__ == "__main__":
    unittest.main()

