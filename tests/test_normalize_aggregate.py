from __future__ import annotations

import unittest

from slowplan.aggregate import aggregate_samples, percentile
from slowplan.errors import BusinessError, E_SQL_UNNORMALIZABLE
from slowplan.normalize import normalize_sql


class NormalizationTests(unittest.TestCase):
    def test_literals_and_in_lengths_are_stable(self) -> None:
        first = normalize_sql("select * from products where category_id IN (1, 2, 3)")
        second = normalize_sql(
            "SELECT * FROM products WHERE category_id IN (3, 6, 9, 12, 15);"
        )
        third = normalize_sql("SELECT * FROM products WHERE category_id IN (?)")
        self.assertEqual(first.template_sql, second.template_sql)
        self.assertEqual(first.template_sql, third.template_sql)
        self.assertEqual(first.template_id, second.template_id)
        self.assertEqual(
            first.template_sql,
            "SELECT * FROM products WHERE category_id IN (?)",
        )

    def test_literal_values_are_removed(self) -> None:
        first = normalize_sql("SELECT * FROM orders WHERE customer_id = 17")
        second = normalize_sql(" SELECT *  FROM orders WHERE customer_id=991 ")
        negative = normalize_sql("SELECT * FROM orders WHERE customer_id = -17")
        self.assertEqual(first.template_sql, second.template_sql)
        self.assertEqual(first.template_id, second.template_id)
        self.assertEqual(first.template_sql, negative.template_sql)
        self.assertEqual(first.template_sql, "SELECT * FROM orders WHERE customer_id = ?")

    def test_negative_in_literals_still_collapse(self) -> None:
        query = normalize_sql("SELECT * FROM orders WHERE customer_id IN (-1, -2, -3)")
        self.assertEqual(
            query.template_sql,
            "SELECT * FROM orders WHERE customer_id IN (?)",
        )

    def test_structure_extraction(self) -> None:
        query = normalize_sql(
            "SELECT * FROM orders WHERE status = 'paid' AND created_at >= 1700000000000 ORDER BY id DESC LIMIT 10"
        )
        self.assertEqual(query.structure["table"], "orders")
        self.assertEqual(
            query.structure["predicates"],
            [
                {"column": "status", "op": "=", "kind": "value"},
                {"column": "created_at", "op": ">=", "kind": "value"},
            ],
        )
        self.assertTrue(query.structure["has_limit"])

    def test_unnormalizable_sql_is_rejected(self) -> None:
        for sql in (
            "SELECT * FROM orders WHERE note = 'unterminated",
            "SELECT FROM WHERE",
            "SELECT * FROM orders WHERE a = 1 OR b = 2",
            "UPDATE orders SET status = 'x'",
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(BusinessError) as raised:
                    normalize_sql(sql)
                self.assertEqual(raised.exception.code, E_SQL_UNNORMALIZABLE)


class AggregationTests(unittest.TestCase):
    def test_percentile_linear_interpolation(self) -> None:
        self.assertEqual(percentile([10, 20, 30, 40], 0.95), 38.5)
        self.assertEqual(percentile([5], 0.95), 5.0)

    def test_aggregate_is_sequence_stable(self) -> None:
        base = [
            {
                "sample_seq": 3,
                "duration_ms": 91,
                "rows_scanned": 11,
                "rows_returned": 11,
                "window_start_ms": 2000,
                "sample_digest": "c",
            },
            {
                "sample_seq": 1,
                "duration_ms": 81,
                "rows_scanned": 10,
                "rows_returned": 10,
                "window_start_ms": 1000,
                "sample_digest": "a",
            },
            {
                "sample_seq": 2,
                "duration_ms": 76,
                "rows_scanned": 9,
                "rows_returned": 9,
                "window_start_ms": 1000,
                "sample_digest": "b",
            },
        ]
        first = aggregate_samples(base, "tpl_x", "SELECT * FROM orders WHERE customer_id = ?")
        second = aggregate_samples(list(reversed(base)), "tpl_x", "SELECT * FROM orders WHERE customer_id = ?")
        self.assertEqual(first, second)
        self.assertEqual(first["sample_count"], 3)
        self.assertEqual(first["latency_ms"]["p95"], 90.0)
        self.assertEqual(first["scanned_rows"]["mean"], 10.0)
        self.assertEqual(first["scan_amplification"], 1.0)
        self.assertEqual(
            first["windows"],
            [
                {"window_start_ms": 1000, "sample_count": 2},
                {"window_start_ms": 2000, "sample_count": 1},
            ],
        )


if __name__ == "__main__":
    unittest.main()
