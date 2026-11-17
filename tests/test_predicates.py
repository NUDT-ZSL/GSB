from __future__ import annotations

import unittest

from tests.helpers import SAMPLES

from shardrouter.config import load_config
from shardrouter.errors import E_SQL, RouterError
from shardrouter.predicates import normalize_predicate
from shardrouter.session import RouterSession
from shardrouter.verification import canonical_text


class PredicateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(SAMPLES / "config.json")
        cls.orders = cls.config.table_map()["orders"]
        cls.ledger = cls.config.table_map()["ledger"]

    def test_whitespace_case_and_in_order_normalize_identically(self) -> None:
        left = normalize_predicate(
            self.orders,
            "tenant_id = 7 AND region IN ('east', 'west')",
        )
        right = normalize_predicate(
            self.orders,
            "  TENANT_ID=7   and    REGION in ('west','east')  ",
        )
        self.assertEqual(left.normalized, right.normalized)
        self.assertEqual(left.routing_id, right.routing_id)
        self.assertEqual(left.normalized, "region IN ('east', 'west') AND tenant_id = 7")

    def test_single_value_in_collapses_to_equality(self) -> None:
        equality = normalize_predicate(self.orders, "tenant_id = 7 AND region = 'east'")
        in_list = normalize_predicate(self.orders, "tenant_id=7 AND region IN ('east')")
        self.assertEqual(equality.normalized, in_list.normalized)
        self.assertEqual(equality.routing_id, in_list.routing_id)

    def test_complete_shard_key_detection_and_missing_key(self) -> None:
        complete = normalize_predicate(self.orders, "tenant_id=7 AND region='east'")
        partial = normalize_predicate(self.orders, "region='east'")
        self.assertTrue(complete.has_complete_shard_key)
        self.assertEqual(complete.missing_shard_keys, ())
        self.assertFalse(partial.has_complete_shard_key)
        self.assertEqual(partial.missing_shard_keys, ("tenant_id",))

    def test_composite_point_extraction_is_sorted(self) -> None:
        info = normalize_predicate(
            self.orders,
            "tenant_id IN (9,7) AND region IN ('west','east')",
        )
        points = info.point_tuples(self.orders.shard_keys)
        self.assertEqual(
            points,
            ((7, "east"), (7, "west"), (9, "east"), (9, "west")),
        )

    def test_range_constraints_and_and_combination(self) -> None:
        info = normalize_predicate(
            self.ledger,
            "amount >= 100 AND amount < 500",
        )
        constraint = info.constraints["amount"]
        self.assertTrue(constraint.lower.inclusive)
        self.assertFalse(constraint.upper.inclusive)
        self.assertEqual(constraint.lower.value, 100)
        self.assertEqual(constraint.upper.value, 500)

    def test_semantically_equivalent_plans_are_byte_identical(self) -> None:
        session = RouterSession(self.config)
        left = session.route("orders", "tenant_id=7 AND region IN ('east')")
        right = session.route("orders", "  REGION = 'east' and TENANT_ID = 7 ")
        self.assertEqual(canonical_text(left), canonical_text(right))

    def test_syntax_error_has_position(self) -> None:
        sql = "tenant_id = 7 AND region ="
        with self.assertRaises(RouterError) as raised:
            normalize_predicate(self.orders, sql)
        self.assertEqual(raised.exception.code, E_SQL)
        self.assertEqual(raised.exception.position["offset"], len(sql))
        self.assertEqual(raised.exception.position["line"], 1)

    def test_or_is_e_sql_at_or_position(self) -> None:
        sql = "tenant_id = 7 OR region = 'east'"
        with self.assertRaises(RouterError) as raised:
            normalize_predicate(self.orders, sql)
        self.assertEqual(raised.exception.code, E_SQL)
        self.assertEqual(raised.exception.position["offset"], sql.index("OR"))

    def test_unknown_column_is_e_sql_at_column_position(self) -> None:
        sql = "tenant_id = 7 AND missing = 1"
        with self.assertRaises(RouterError) as raised:
            normalize_predicate(self.orders, sql)
        self.assertEqual(raised.exception.code, E_SQL)
        self.assertEqual(raised.exception.position["offset"], sql.index("missing"))


if __name__ == "__main__":
    unittest.main()
