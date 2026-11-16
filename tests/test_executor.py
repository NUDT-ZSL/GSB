from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joinlab.catalog import load_catalog
from joinlab.cost import CostParams, enumerate_plans
from joinlab.executor import ExecutionResult, compare_results, execute_and_verify, execute_query
from joinlab.errors import PlanError
from joinlab.queries import load_query_file
from joinlab.query import parse_query


class ExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog(ROOT / "samples" / "catalog.json")
        cls.queries = load_query_file(ROOT / "samples" / "queries.json", cls.catalog)
        cls.params = CostParams()
        cls.data = cls.catalog.data_by_key

    def test_all_sample_queries_are_byte_consistent_across_plans(self) -> None:
        for item in self.queries:
            with self.subTest(query=item.name):
                plans = enumerate_plans(item.query, self.catalog, self.params)
                bundle = execute_and_verify(
                    item.query,
                    self.catalog,
                    plans.selected,
                    plans.fallback_plan(),
                    self.data,
                )
                self.assertTrue(bundle.consistency.consistent)
                self.assertEqual(bundle.selected.encoded, bundle.fallback.encoded)
                self.assertEqual(bundle.selected.digest, bundle.fallback.digest)

    def test_repeated_execution_has_stable_bytes_and_stats_without_timing(self) -> None:
        item = self.queries[1]
        plans = enumerate_plans(item.query, self.catalog, self.params)
        first = execute_query(item.query, self.catalog, plans.selected, self.data)
        second = execute_query(item.query, self.catalog, plans.selected, self.data)
        self.assertEqual(first.encoded, second.encoded)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.stats_fingerprint, second.stats_fingerprint)
        self.assertNotIn("elapsed_ms", json.dumps(first.stats_fingerprint))

    def test_left_join_result_is_sorted_by_all_output_columns(self) -> None:
        query = parse_query(
            "SELECT c.id, c.name, o.id, o.amount FROM customers c LEFT JOIN orders o "
            "ON c.id=o.customer_id WHERE c.region='east'",
            self.catalog,
        )
        plans = enumerate_plans(query, self.catalog, self.params)
        result = execute_query(query, self.catalog, plans.selected, self.data)
        self.assertEqual(list(result.rows), sorted(result.rows, key=lambda row: (row[0], row[1], row[2] if row[2] is not None else 10**18)))

    def test_where_on_right_side_of_left_join_has_sql_semantics(self) -> None:
        left_sql = (
            "SELECT c.id, o.id FROM customers c LEFT JOIN orders o ON c.id=o.customer_id "
            "WHERE o.status='paid'"
        )
        inner_sql = (
            "SELECT c.id, o.id FROM customers c INNER JOIN orders o ON c.id=o.customer_id "
            "WHERE o.status='paid'"
        )
        left_query = parse_query(left_sql, self.catalog)
        inner_query = parse_query(inner_sql, self.catalog)
        left_plans = enumerate_plans(left_query, self.catalog, self.params)
        inner_plans = enumerate_plans(inner_query, self.catalog, self.params)
        left = execute_query(left_query, self.catalog, left_plans.selected, self.data)
        inner = execute_query(inner_query, self.catalog, inner_plans.selected, self.data)
        self.assertEqual(left.encoded, inner.encoded)

    def test_execute_and_verify_raises_e_plan_with_first_difference(self) -> None:
        from joinlab import executor as executor_module

        item = self.queries[0]
        plans = enumerate_plans(item.query, self.catalog, self.params)
        selected = execute_query(item.query, self.catalog, plans.selected, self.data)
        fallback = ExecutionResult(
            query_id=selected.query_id,
            plan_id=plans.fallback_plan().plan_id,
            columns=selected.columns,
            rows=((1, "different", 1.0, "2025-01-01"),),
            encoded=b'[1,"different",1.0,"2025-01-01"]\n',
            digest="different",
            stats=selected.stats,
            elapsed_total_ms=0.0,
            stats_fingerprint=selected.stats_fingerprint,
            attribution_chain=selected.attribution_chain,
            estimated_rows=1,
        )
        with patch.object(executor_module, "execute_query", side_effect=[selected, fallback]):
            with self.assertRaises(PlanError) as raised:
                executor_module.execute_and_verify(
                    item.query,
                    self.catalog,
                    plans.selected,
                    plans.fallback_plan(),
                    self.data,
                )
        self.assertEqual(raised.exception.code, "E_PLAN")
        self.assertEqual(
            raised.exception.details["consistency"]["first_difference"]["operator"],
            "canonical_sort",
        )

    def test_first_difference_contains_row_keys_and_operator(self) -> None:
        item = self.queries[0]
        plans = enumerate_plans(item.query, self.catalog, self.params)
        first = execute_query(item.query, self.catalog, plans.selected, self.data)
        fake = ExecutionResult(
            query_id=first.query_id,
            plan_id="p_fake",
            columns=first.columns,
            rows=((999, "different", 0.0, "2025-01-01"),),
            encoded=b'[999,"different",0,"2025-01-01"]\n',
            digest="fake",
            stats=first.stats,
            elapsed_total_ms=0.0,
            stats_fingerprint=first.stats_fingerprint,
            attribution_chain=first.attribution_chain,
            estimated_rows=1,
        )
        report = compare_results(first, fake)
        self.assertFalse(report.consistent)
        self.assertEqual(report.first_difference["operator"], "canonical_sort")
        self.assertIsNotNone(report.first_difference["selected_row_key"])


if __name__ == "__main__":
    unittest.main()
