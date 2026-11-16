from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joinlab.catalog import load_catalog
from joinlab.cost import DECISION_RULE, CostParams, enumerate_plans
from joinlab.query import parse_query


class PlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog(ROOT / "samples" / "catalog.json")
        cls.query = parse_query(
            "SELECT o.id, c.name, i.id FROM orders o JOIN customers c ON o.customer_id=c.id "
            "JOIN order_items i ON i.order_id=o.id",
            cls.catalog,
        )

    def test_enumeration_contains_orders_and_operators(self) -> None:
        plans = enumerate_plans(self.query, self.catalog, CostParams())
        self.assertGreater(len(plans.candidates), 6)
        signatures = {plan.plan_signature for plan in plans.candidates}
        self.assertTrue(any("hash" in signature for signature in signatures))
        self.assertTrue(any("nested_loop" in signature for signature in signatures))
        self.assertEqual(plans.decision_rule, DECISION_RULE)

    def test_candidate_order_is_total_and_stable(self) -> None:
        first = enumerate_plans(self.query, self.catalog, CostParams())
        second = enumerate_plans(self.query, self.catalog, CostParams())
        self.assertEqual(
            [(plan.plan_id, plan.quantized_cost_micro) for plan in first.candidates],
            [(plan.plan_id, plan.quantized_cost_micro) for plan in second.candidates],
        )
        keys = [(plan.quantized_cost_micro, plan.plan_signature) for plan in first.candidates]
        self.assertEqual(keys, sorted(keys))

    def test_quantum_ties_use_signature_not_dict_order(self) -> None:
        params = CostParams(cost_quantum=1_000_000_000.0)
        plans = enumerate_plans(self.query, self.catalog, params)
        self.assertEqual({plan.quantized_cost_micro for plan in plans.candidates}, {0})
        expected = min(plan.plan_signature for plan in plans.candidates)
        self.assertEqual(plans.selected.plan_signature, expected)

    def test_order_bias_can_change_first_joined_table(self) -> None:
        params = CostParams(join_order_bias=(("customers", 1000.0),))
        plans = enumerate_plans(self.query, self.catalog, params)
        self.assertNotEqual(plans.selected.relation_order[-1], "customers")
        self.assertIn("customers", plans.selected.relation_order)


if __name__ == "__main__":
    unittest.main()
