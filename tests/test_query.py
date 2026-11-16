from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joinlab.catalog import load_catalog
from joinlab.errors import JoinLabError
from joinlab.query import parse_query


class QueryNormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog(ROOT / "samples" / "catalog.json")

    def test_whitespace_case_alias_and_join_order_normalize_to_same_id(self) -> None:
        variants = [
            "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id WHERE o.status = 'paid'",
            "  select   O.ID , C.NAME\nfrom ORDERS O  inner join CUSTOMERS C on O.CUSTOMER_ID=C.ID where O.STATUS='paid' ",
            "/* normalized */ SELECT x.id, y.name FROM customers y INNER JOIN orders x ON y.id = x.customer_id WHERE 'paid' = x.status",
        ]
        parsed = [parse_query(sql, self.catalog) for sql in variants]
        self.assertEqual({item.query_id for item in parsed}, {parsed[0].query_id})
        self.assertEqual({item.canonical_sql for item in parsed}, {parsed[0].canonical_sql})

    def test_output_alias_spelling_does_not_change_identity(self) -> None:
        first = parse_query(
            "SELECT o.id AS order_id, c.name AS customer_name FROM orders o JOIN customers c ON o.customer_id=c.id",
            self.catalog,
        )
        second = parse_query(
            "SELECT x.id, y.name FROM orders x JOIN customers y ON x.customer_id=y.id",
            self.catalog,
        )
        self.assertEqual(first.query_id, second.query_id)

    def test_three_table_join_order_is_stable(self) -> None:
        first = parse_query(
            "SELECT o.id, c.name, i.id FROM orders o JOIN customers c ON o.customer_id=c.id "
            "JOIN order_items i ON i.order_id=o.id WHERE i.quantity>1",
            self.catalog,
        )
        second = parse_query(
            "SELECT x.id, y.name, z.id FROM order_items z JOIN orders x ON z.order_id=x.id "
            "JOIN customers y ON x.customer_id=y.id WHERE z.quantity>1",
            self.catalog,
        )
        self.assertEqual(first.query_id, second.query_id)
        self.assertEqual(first.tables, ("customers", "orders", "order_items"))

    def test_rejects_unsupported_features_as_e_query(self) -> None:
        cases = [
            "SELECT COUNT(*) FROM orders",
            "SELECT * FROM customers c RIGHT JOIN orders o ON c.id=o.customer_id",
            "SELECT * FROM customers c FULL JOIN orders o ON c.id=o.customer_id",
            "SELECT * FROM (SELECT * FROM customers) x",
            "SELECT * FROM customers WHERE id=1 OR id=2",
            "SELECT * FROM customers GROUP BY id",
            "SELECT * FROM customers ORDER BY id",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                with self.assertRaises(JoinLabError) as raised:
                    parse_query(sql, self.catalog)
                self.assertEqual(raised.exception.code, "E_QUERY")

    def test_type_mismatch_is_e_type(self) -> None:
        with self.assertRaises(JoinLabError) as raised:
            parse_query(
                "SELECT orders.id FROM orders JOIN customers ON orders.status = customers.id",
                self.catalog,
            )
        self.assertEqual(raised.exception.code, "E_TYPE")

    def test_unknown_reference_is_reported(self) -> None:
        with self.assertRaises(JoinLabError) as raised:
            parse_query(
                "SELECT orders.id FROM orders JOIN customers ON orders.nope = customers.id",
                self.catalog,
            )
        self.assertEqual(raised.exception.code, "E_QUERY")
        self.assertEqual(raised.exception.details.get("reason"), "unknown_ref")

    def test_at_most_three_tables(self) -> None:
        with self.assertRaises(JoinLabError) as raised:
            parse_query(
                "SELECT o.id FROM orders o JOIN customers c ON o.customer_id=c.id "
                "JOIN order_items i ON i.order_id=o.id JOIN products p ON p.id=i.product_id",
                self.catalog,
            )
        self.assertEqual(raised.exception.code, "E_QUERY")


if __name__ == "__main__":
    unittest.main()
