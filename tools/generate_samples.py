"""Generate the repository's deterministic local experiment fixture."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples"
DATA = SAMPLE / "data"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    regions = ["east", "west", "north", "south"]
    customers = [
        {
            "id": index,
            "name": f"customer-{index:02d}",
            "region": regions[(index - 1) % len(regions)],
            "age": 22 + ((index * 5) % 35),
        }
        for index in range(1, 13)
    ]

    statuses = ["paid", "open", "cancelled"]
    orders = []
    for index in range(1, 37):
        orders.append(
            {
                "id": index,
                "customer_id": 1 + ((index * 5 + 2) % 12),
                "status": statuses[(index - 1) % 3],
                "amount": round(10.0 + ((index * 17) % 120) + (index % 5) * 0.25, 2),
                "created": f"2025-{(index % 12) + 1:02d}-{(index % 27) + 1:02d}",
            }
        )

    categories = ["book", "game", "home", "office"]
    products = []
    for index in range(1, 21):
        products.append(
            {
                "id": index,
                "category": categories[(index - 1) % len(categories)],
                "price": round(5.0 + index * 1.75, 2),
                "active": index % 5 != 0,
            }
        )

    order_items = []
    for order_id in range(1, 37):
        for slot in (0, 1):
            item_id = (order_id - 1) * 2 + slot + 1
            order_items.append(
                {
                    "id": item_id,
                    "order_id": order_id,
                    "product_id": 1 + ((order_id * 7 + slot * 3) % 20),
                    "quantity": 1 + ((item_id * 3) % 4),
                    "unit_price": round(3.5 + ((item_id * 11) % 80) / 10, 2),
                }
            )

    write_json(DATA / "customers.json", {"table": "customers", "rows": customers})
    write_json(DATA / "orders.json", {"table": "orders", "rows": orders})
    write_json(DATA / "products.json", {"table": "products", "rows": products})
    write_json(DATA / "order_items.json", {"table": "order_items", "rows": order_items})

    catalog = {
        "name": "joinlab-deterministic-sample",
        "schema_version": 1,
        "data_version": 1,
        "skew_limit": 0.80,
        "tables": [
            {
                "name": "customers",
                "row_count": len(customers),
                "skew": False,
                "data_file": "data/customers.json",
                "skew_column": "region",
                "skew_limit": 0.80,
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False, "ndv": 12},
                    {"name": "name", "type": "text", "nullable": False, "ndv": 12},
                    {"name": "region", "type": "text", "nullable": False, "ndv": 4},
                    {"name": "age", "type": "integer", "nullable": False, "ndv": 12},
                ],
            },
            {
                "name": "orders",
                "row_count": len(orders),
                "skew": True,
                "data_file": "data/orders.json",
                "skew_column": "customer_id",
                "skew_limit": 0.80,
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False, "ndv": 36},
                    {"name": "customer_id", "type": "integer", "nullable": False, "ndv": 9},
                    {"name": "status", "type": "text", "nullable": False, "ndv": 3},
                    {"name": "amount", "type": "real", "nullable": False, "ndv": 36},
                    {"name": "created", "type": "date", "nullable": False, "ndv": 27},
                ],
            },
            {
                "name": "products",
                "row_count": len(products),
                "skew": False,
                "data_file": "data/products.json",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False, "ndv": 20},
                    {"name": "category", "type": "text", "nullable": False, "ndv": 4},
                    {"name": "price", "type": "real", "nullable": False, "ndv": 20},
                    {"name": "active", "type": "boolean", "nullable": False, "ndv": 2},
                ],
            },
            {
                "name": "order_items",
                "row_count": len(order_items),
                "skew": False,
                "data_file": "data/order_items.json",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False, "ndv": 72},
                    {"name": "order_id", "type": "integer", "nullable": False, "ndv": 36},
                    {"name": "product_id", "type": "integer", "nullable": False, "ndv": 20},
                    {"name": "quantity", "type": "integer", "nullable": False, "ndv": 4},
                    {"name": "unit_price", "type": "real", "nullable": False, "ndv": 48},
                ],
            },
        ],
    }
    write_json(SAMPLE / "catalog.json", catalog)

    queries = {
        "queries": [
            {
                "name": "two_table_inner",
                "sql": (
                    "SELECT o.id, c.name, o.amount, o.created "
                    "FROM orders o JOIN customers c ON o.customer_id = c.id "
                    "WHERE o.status = 'paid' AND o.amount >= 50.0"
                ),
            },
            {
                "name": "three_table_inner",
                "sql": (
                    "SELECT o.id, c.name, i.id, i.quantity "
                    "FROM orders o "
                    "JOIN customers c ON c.id = o.customer_id "
                    "JOIN order_items i ON i.order_id = o.id "
                    "WHERE c.region = 'east' AND i.quantity > 1"
                ),
            },
            {
                "name": "left_customer_orders",
                "sql": (
                    "SELECT c.id, c.name, o.id, o.status, o.amount "
                    "FROM customers c "
                    "LEFT JOIN orders o ON c.id = o.customer_id "
                    "WHERE c.region = 'east'"
                ),
            },
            {
                "name": "three_table_products",
                "sql": (
                    "SELECT p.category, i.order_id, i.quantity, p.price "
                    "FROM order_items i "
                    "JOIN products p ON i.product_id = p.id "
                    "JOIN orders o ON o.id = i.order_id "
                    "WHERE p.category = 'game' AND o.amount < 75"
                ),
            },
        ]
    }
    write_json(SAMPLE / "queries.json", queries)


if __name__ == "__main__":
    main()
