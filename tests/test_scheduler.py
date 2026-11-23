from __future__ import annotations

import unittest

from sparse_recall.canonical import canonical_json_bytes
from sparse_recall.errors import (
    E_COMPUTE_BUDGET,
    E_MEMORY_BUDGET,
    E_SLOT_LIMIT,
)
from sparse_recall.scheduler import accepted_decisions, build_plan

from tests._fixtures import load_inputs, make_dataset, scheduler_config


class SchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary, cls.data = make_dataset()
        cls.weights, cls.ingested = load_inputs(cls.data)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_plan_is_byte_repeatable(self) -> None:
        config = scheduler_config(self.ingested)
        first = build_plan(self.ingested, self.weights, config)
        second = build_plan(self.ingested, self.weights, config)
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))

    def test_budget_constraints_and_totals(self) -> None:
        config = scheduler_config(self.ingested, fma_budget=26000, memory_budget=20000)
        plan = build_plan(self.ingested, self.weights, config)
        statuses = {item["status"] for item in plan["decisions"]}
        self.assertEqual(statuses, {"admitted", "degraded", "rejected"})
        self.assertLessEqual(plan["totals"]["executed_fma"], config.fma_budget)
        for wave in plan["waves"]:
            self.assertLessEqual(wave["resident_memory_bytes"], config.memory_budget)
            self.assertLessEqual(wave["slots_used"], config.slots)
        for decision in plan["decisions"]:
            self.assertIn("full_fma", decision["estimate"])
            self.assertIn("full_memory_bytes", decision["estimate"])
            self.assertIn("shared_terms", decision["weight_reuse"])
            self.assertIn("reason_codes", decision)

    def test_query_priority_is_visible_in_order(self) -> None:
        plan = build_plan(
            self.ingested,
            self.weights,
            scheduler_config(self.ingested, fma_budget=26000),
        )
        priorities = [item["priority"]["query_priority"] for item in plan["decisions"]]
        self.assertEqual(priorities, sorted(priorities, reverse=True))
        self.assertEqual(plan["decisions"][0]["kind"], "query")

    def test_slot_compute_and_memory_rejection_codes(self) -> None:
        slot_plan = build_plan(
            self.ingested,
            self.weights,
            scheduler_config(self.ingested, slots=0),
        )
        self.assertTrue(slot_plan["decisions"])
        self.assertTrue(
            all(item["code"] == E_SLOT_LIMIT for item in slot_plan["decisions"])
        )

        compute_plan = build_plan(
            self.ingested,
            self.weights,
            scheduler_config(self.ingested, slots=2, fma_budget=1, memory_budget=10**9),
        )
        self.assertTrue(
            any(item["code"] == E_COMPUTE_BUDGET for item in compute_plan["decisions"])
        )

        memory_plan = build_plan(
            self.ingested,
            self.weights,
            scheduler_config(self.ingested, slots=2, fma_budget=10**9, memory_budget=1),
        )
        self.assertTrue(
            any(item["code"] == E_MEMORY_BUDGET for item in memory_plan["decisions"])
        )
        self.assertEqual(accepted_decisions(memory_plan), [])

        shard_plan = build_plan(
            self.ingested,
            self.weights,
            scheduler_config(
                self.ingested,
                slots=1,
                fma_budget=26000,
                memory_budget=20000,
                workers=2,
            ),
        )
        shard_degraded = [
            item
            for item in shard_plan["decisions"]
            if item["status"] == "degraded" and E_SLOT_LIMIT in item["reason_codes"]
        ]
        self.assertTrue(shard_degraded)
        self.assertTrue(
            all(item["execution"]["shards"] == 1 for item in shard_degraded)
        )


if __name__ == "__main__":
    unittest.main()
