from __future__ import annotations

import unittest

from tests.helpers import SAMPLES

from shardrouter.config import load_config
from shardrouter.dependency import DependencyGraph
from shardrouter.errors import E_DEPEND
from shardrouter.session import RouterSession


class DependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config_v1 = load_config(SAMPLES / "config.json")
        cls.config_v2 = load_config(SAMPLES / "config_v2.json")

    def test_topological_order_places_dependencies_before_dependents(self) -> None:
        graph = DependencyGraph.build(self.config_v1.route_units)
        order = graph.topological_order()
        self.assertLess(order.index("orders_lookup"), order.index("orders_rollup"))
        self.assertLess(order.index("orders_region"), order.index("orders_rollup"))
        self.assertEqual(order, DependencyGraph.build(self.config_v1.route_units).topological_order())

    def test_rule_change_propagates_to_direct_and_indirect_downstream(self) -> None:
        session = RouterSession(self.config_v1)
        session.route("orders", "tenant_id=7 AND region='east'")
        session.route_unit("orders_lookup")
        session.route_unit("orders_rollup")
        report = session.apply_config(self.config_v2)
        invalidation = report["invalidation"]
        self.assertEqual(invalidation["order"], [
            "orders_lookup",
            "orders_audit",
            "orders_region",
            "orders_rollup",
            "session_pair",
        ])
        self.assertIn("orders_rollup", invalidation["order"])
        self.assertEqual(invalidation["cache_entries_removed"], 2)
        self.assertEqual(session.version_state()["table_versions"]["orders"], 2)
        self.assertEqual(session.version_state()["ring_versions"]["sessions"], 2)

    def test_route_unit_result_change_invalidates_downstream(self) -> None:
        session = RouterSession(self.config_v1)
        session.route_unit("orders_lookup")
        session.route_unit("orders_rollup")
        session.unit_results["orders_lookup"]["result_id"] = "stale-result"
        report = session.route_unit("orders_lookup")
        self.assertEqual(len(report["invalidation_events"]), 1)
        self.assertEqual(report["invalidation_events"][0]["order"], ["orders_rollup"])
        self.assertIn("orders_rollup", report["invalidation_events"][0]["unit_results_removed"])

    def test_delete_referenced_table_is_e_depend(self) -> None:
        session = RouterSession(self.config_v1)
        with self.assertRaises(Exception) as raised:
            session.remove_table_rule("orders")
        self.assertEqual(getattr(raised.exception, "code", None), E_DEPEND)
        self.assertEqual(raised.exception.details["referrers"], ["orders_lookup", "orders_region"])

    def test_delete_referenced_route_unit_is_e_depend(self) -> None:
        session = RouterSession(self.config_v1)
        with self.assertRaises(Exception) as raised:
            session.remove_route_unit("orders_lookup")
        self.assertEqual(getattr(raised.exception, "code", None), E_DEPEND)
        self.assertEqual(raised.exception.details["referrers"], ["orders_rollup"])


if __name__ == "__main__":
    unittest.main()
