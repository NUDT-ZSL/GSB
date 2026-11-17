from __future__ import annotations

import bisect
import unittest

from tests.helpers import SAMPLES

from shardrouter.algorithms import build_virtual_ring
from shardrouter.config import load_config
from shardrouter.session import RouterSession
from shardrouter.util import canonical_json, stable_hash_int


class AlgorithmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(SAMPLES / "config.json")
        cls.session = RouterSession(cls.config)

    def plan(self, table: str, predicate: str) -> dict:
        return self.session.route(table, predicate)

    def test_mod_is_repeatable_and_reports_parameter_version(self) -> None:
        first = self.plan("orders", "tenant_id=7 AND region='east'")
        second = self.plan("orders", "TENANT_ID=7 and region IN ('east')")
        self.assertEqual(first["routing"]["algorithm"], "mod")
        self.assertEqual(first["routing"]["targets"], second["routing"]["targets"])
        self.assertEqual(first["routing"]["rule_version"], 1)
        self.assertIn("stable_hash", first["routing"]["reasons"][0])
        self.assertEqual(first["routing"]["target_count"], 1)

    def test_range_boundary_is_lower_inclusive_upper_exclusive(self) -> None:
        below = self.plan("ledger", "amount = 99")
        lower_boundary = self.plan("ledger", "amount = 100")
        middle_boundary = self.plan("ledger", "amount = 500")
        upper_boundary = self.plan("ledger", "amount = 1000")
        self.assertEqual(below["routing"]["targets"][0]["algorithm_bucket"], 0)
        self.assertEqual(lower_boundary["routing"]["targets"][0]["algorithm_bucket"], 1)
        self.assertEqual(middle_boundary["routing"]["targets"][0]["algorithm_bucket"], 2)
        self.assertEqual(upper_boundary["routing"]["targets"][0]["algorithm_bucket"], 3)

    def test_range_interval_returns_every_overlapping_bucket(self) -> None:
        plan = self.plan("ledger", "amount BETWEEN 100 AND 500")
        self.assertEqual(
            [target["algorithm_bucket"] for target in plan["routing"]["targets"]],
            [1, 2],
        )
        self.assertEqual(plan["routing"]["coverage"], "range")

    def test_consistent_hash_uses_clockwise_first_node(self) -> None:
        sessions = self.config.table_map()["sessions"]
        ring = build_virtual_ring(sessions)
        key_token = stable_hash_int(canonical_json(["s-01"]))
        index = bisect.bisect_left(ring.tokens, key_token)
        if index == len(ring.tokens):
            index = 0
        expected = ring.nodes[index]
        plan = self.plan("sessions", "session_id = 's-01'")
        target = plan["routing"]["targets"][0]
        self.assertEqual(plan["routing"]["algorithm"], "consistent_hash")
        self.assertEqual(plan["routing"]["ring_version"], 1)
        self.assertEqual(target["database_index"], expected.database)
        self.assertEqual(target["table_index"], expected.table)
        self.assertEqual(target["algorithm_bucket"], expected.token)

    def test_consistent_hash_in_list_is_stable_and_deduplicated(self) -> None:
        first = self.plan("sessions", "session_id IN ('s-02','s-01')")
        second = self.plan("sessions", "session_id IN ('s-01','s-02','s-01')")
        self.assertEqual(first["routing"]["targets"], second["routing"]["targets"])
        self.assertEqual(first["predicate"]["routing_id"], second["predicate"]["routing_id"])

    def test_incomplete_key_marks_broadcast_explicitly(self) -> None:
        plan = self.plan("orders", "region='east'")
        self.assertTrue(plan["routing"]["broadcast"])
        self.assertEqual(plan["routing"]["coverage"], "broadcast")
        self.assertEqual(plan["routing"]["reason_code"], "BROADCAST_MISSING_SHARD_KEYS")
        self.assertEqual(plan["routing"]["target_count"], 8)
        self.assertEqual(plan["shard_key"]["missing"], ["tenant_id"])

    def test_targets_are_always_sorted(self) -> None:
        plan = self.plan("orders", "region='east'")
        keys = [
            (target["database_index"], target["table_index"])
            for target in plan["routing"]["targets"]
        ]
        self.assertEqual(keys, sorted(keys))


if __name__ == "__main__":
    unittest.main()
