from __future__ import annotations

import unittest

from shardcheck.scheduler import plan_budget


class BudgetTests(unittest.TestCase):
    def test_retention_and_budget_evict_oldest_first(self) -> None:
        manifest = {
            "generations": [
                {"generation_id": "gen-1", "generation": 1, "round": 1, "payload_bytes": 100},
                {"generation_id": "gen-2", "generation": 2, "round": 2, "payload_bytes": 100},
                {"generation_id": "gen-3", "generation": 3, "round": 3, "payload_bytes": 100},
            ]
        }
        plan = plan_budget(
            manifest=manifest,
            pending_bytes=100,
            global_budget_bytes=250,
            desired_retention=4,
            max_retention=4,
            budget_action="evict_then_write",
            checkpoint_due=True,
        )
        self.assertEqual(plan.decision, "commit")
        self.assertEqual(plan.evicted_generation_ids, ["gen-1", "gen-2"])
        self.assertEqual([item["order"] for item in plan.evictions], [1, 2])
        self.assertEqual(plan.prospective_usage_bytes, 200)

    def test_reject_preserves_existing_generations(self) -> None:
        manifest = {
            "generations": [
                {"generation_id": "gen-1", "generation": 1, "round": 1, "payload_bytes": 100},
            ]
        }
        plan = plan_budget(
            manifest=manifest,
            pending_bytes=200,
            global_budget_bytes=250,
            desired_retention=2,
            max_retention=2,
            budget_action="reject",
            checkpoint_due=True,
        )
        self.assertEqual(plan.decision, "reject")
        self.assertEqual(plan.evicted_generation_ids, [])
        self.assertEqual(plan.retained_generation_ids, ["gen-1"])

    def test_illegal_retention_is_clamped_and_reported(self) -> None:
        plan = plan_budget(
            manifest={"generations": []},
            pending_bytes=10,
            global_budget_bytes=100,
            desired_retention=99,
            max_retention=3,
            budget_action="evict_then_write",
            checkpoint_due=True,
        )
        self.assertTrue(plan.retention_illegal)
        self.assertEqual(plan.effective_retention, 3)


if __name__ == "__main__":
    unittest.main()
