import unittest

from mmpipe.config import BudgetConfig
from mmpipe.errors import ResourceLimitError
from mmpipe.scheduler import (
    DEGRADE_BATCH,
    DEGRADE_WORKERS,
    ResourceObservation,
    arbitrate,
    chunk_pairs,
)


class SchedulerArbitrationTests(unittest.TestCase):
    def test_happy_path_no_degradation(self):
        budget = BudgetConfig()
        decision = arbitrate(
            ResourceObservation("image", pair_count=12, requested_workers=2, requested_batch=4),
            budget,
        )
        self.assertFalse(decision.degraded)
        self.assertEqual(decision.chosen_workers, 2)
        self.assertEqual(decision.chosen_batch, 4)
        self.assertEqual(decision.parallel_tasks, 2)

    def test_stage_pair_cap_rejects(self):
        budget = BudgetConfig(max_stage_pairs=10)
        with self.assertRaises(ResourceLimitError) as ctx:
            arbitrate(ResourceObservation("image", 11, 2, 4), budget)
        self.assertEqual(ctx.exception.code, "E_RESOURCE_LIMIT")

    def test_resident_memory_cap_rejects(self):
        budget = BudgetConfig(max_resident_embeddings=4, overflow_policy="reject")
        with self.assertRaises(ResourceLimitError) as ctx:
            arbitrate(ResourceObservation("image", 5, 2, 4), budget)
        self.assertEqual(ctx.exception.code, "E_RESOURCE_LIMIT")

    def test_batch_degradation_is_recorded_and_reproducible(self):
        budget = BudgetConfig(max_batch_pairs=2)
        obs = ResourceObservation("text", pair_count=8, requested_workers=2, requested_batch=8)
        first = arbitrate(obs, budget)
        second = arbitrate(obs, budget)
        self.assertTrue(first.degraded)
        self.assertIn(DEGRADE_BATCH, first.degradation_sources)
        self.assertEqual(first.chosen_batch, 2)
        self.assertEqual(first.to_report(), second.to_report())
        self.assertTrue(first.rationale)

    def test_worker_clamp_for_slot_cap(self):
        budget = BudgetConfig(max_workers=4, max_parallel_tasks=2, max_batch_pairs=1)
        decision = arbitrate(
            ResourceObservation("fusion", pair_count=8, requested_workers=4, requested_batch=1),
            budget,
        )
        self.assertIn(DEGRADE_WORKERS, decision.degradation_sources)
        self.assertLessEqual(decision.parallel_tasks, budget.max_parallel_tasks)

    def test_reject_policy_on_task_cap(self):
        budget = BudgetConfig(
            max_workers=4,
            max_parallel_tasks=1,
            max_batch_pairs=1,
            overflow_policy="reject",
        )
        with self.assertRaises(ResourceLimitError):
            arbitrate(ResourceObservation("score", pair_count=4, requested_workers=4, requested_batch=1), budget)

    def test_deterministic_chunks_and_merge_order(self):
        pairs = [f"p{i}" for i in range(10)]
        chunks = chunk_pairs(pairs, 3)
        self.assertEqual(chunks[0], ("p0", "p1", "p2"))
        self.assertEqual(chunks[-1], ("p9",))
        flattened = [pair for chunk in chunks for pair in chunk]
        self.assertEqual(flattened, pairs)


if __name__ == "__main__":
    unittest.main()

