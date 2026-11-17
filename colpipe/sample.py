"""Deterministic sample session generated entirely offline."""

from __future__ import annotations

from typing import Any

from .catalog import INPUT_FORMAT

SESSION_ID = "colpipe-sales-demo"
BASE_VERSION = "sales-2026-11-17.v1"
SAMPLE_QUERY = (
    "SELECT c.region AS region, o.category AS category, "
    "SUM(o.amount) AS total_amount, COUNT(*) AS order_count, "
    "MAX(o.quantity) AS max_quantity "
    "FROM orders AS o INNER JOIN customers AS c ON o.customer_id = c.id "
    "WHERE (o.amount >= 25.0 AND o.amount < 90.0) "
    "AND o.category IN ['books', 'games'] "
    "AND c.active = true "
    "GROUP BY c.region, o.category "
    "ORDER BY region, category"
)


class DeterministicRandom:
    """Tiny LCG with a fixed state; independent of Python's hash seed."""

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFF

    def next(self) -> int:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state


def sample_payload(version: str = BASE_VERSION) -> dict[str, Any]:
    rng = DeterministicRandom(0xC01D1E)
    customer_count = 128
    order_count = 4096
    regions = ["north", "south", "east", "west"]
    categories = ["books", "games", "home", "garden"]
    channels = ["online", "retail"]
    statuses = ["new", "paid", "cancelled"]

    customer_columns: dict[str, dict[str, Any]] = {
        "id": {"type": "int64", "values": [], "null_bitmap": []},
        "region": {"type": "string", "values": [], "null_bitmap": []},
        "active": {"type": "bool", "values": [], "null_bitmap": []},
        "tier": {"type": "int64", "values": [], "null_bitmap": []},
        "note": {"type": "string", "values": [], "null_bitmap": []},
        "signup_day": {"type": "int64", "values": [], "null_bitmap": []},
    }
    for index in range(customer_count):
        region_null = index in {17, 53, 97}
        active_null = index in {41, 109}
        customer_columns["id"]["values"].append(index)
        customer_columns["id"]["null_bitmap"].append(False)
        customer_columns["region"]["values"].append(None if region_null else regions[index % len(regions)])
        customer_columns["region"]["null_bitmap"].append(region_null)
        customer_columns["active"]["values"].append(
            None if active_null else (index % 11 not in {0, 7})
        )
        customer_columns["active"]["null_bitmap"].append(active_null)
        tier_null = index % 31 == 0
        customer_columns["tier"]["values"].append(None if tier_null else (index % 3) + 1)
        customer_columns["tier"]["null_bitmap"].append(tier_null)
        note_null = index % 19 == 0
        customer_columns["note"]["values"].append(
            None if note_null else f"customer-{index:03d}-{regions[index % 4]}"
        )
        customer_columns["note"]["null_bitmap"].append(note_null)
        customer_columns["signup_day"]["values"].append(index * 3 % 365)
        customer_columns["signup_day"]["null_bitmap"].append(False)

    order_columns: dict[str, dict[str, Any]] = {
        "order_id": {"type": "int64", "values": [], "null_bitmap": []},
        "customer_id": {"type": "int64", "values": [], "null_bitmap": []},
        "amount": {"type": "float64", "values": [], "null_bitmap": []},
        "category": {"type": "string", "values": [], "null_bitmap": []},
        "quantity": {"type": "int64", "values": [], "null_bitmap": []},
        "channel": {"type": "string", "values": [], "null_bitmap": []},
        "status": {"type": "string", "values": [], "null_bitmap": []},
        "created_day": {"type": "int64", "values": [], "null_bitmap": []},
    }
    for index in range(order_count):
        customer_id = rng.next() % customer_count
        amount_null = index % 43 == 0
        amount = None if amount_null else round(5.0 + (rng.next() % 12000) / 100.0, 2)
        category_null = index % 67 == 0
        quantity_null = index % 131 == 0
        order_columns["order_id"]["values"].append(index)
        order_columns["order_id"]["null_bitmap"].append(False)
        order_columns["customer_id"]["values"].append(customer_id)
        order_columns["customer_id"]["null_bitmap"].append(False)
        order_columns["amount"]["values"].append(amount)
        order_columns["amount"]["null_bitmap"].append(amount_null)
        order_columns["category"]["values"].append(
            None if category_null else categories[(index + customer_id) % len(categories)]
        )
        order_columns["category"]["null_bitmap"].append(category_null)
        order_columns["quantity"]["values"].append(None if quantity_null else (rng.next() % 8) + 1)
        order_columns["quantity"]["null_bitmap"].append(quantity_null)
        order_columns["channel"]["values"].append(channels[(index // 3) % len(channels)])
        order_columns["channel"]["null_bitmap"].append(False)
        order_columns["status"]["values"].append(statuses[(index + customer_id) % len(statuses)])
        order_columns["status"]["null_bitmap"].append(False)
        order_columns["created_day"]["values"].append(index % 365)
        order_columns["created_day"]["null_bitmap"].append(False)

    def column_payloads(columns: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "type": spec["type"],
                "values": spec["values"],
                "null_bitmap": spec["null_bitmap"],
            }
            for name, spec in columns.items()
        ]

    return {
        "format": INPUT_FORMAT,
        "session_id": SESSION_ID,
        "data_version": version,
        "tables": [
            {
                "name": "customers",
                "schema_version": "customers-schema-v1",
                "row_count": customer_count,
                "columns": column_payloads(customer_columns),
            },
            {
                "name": "orders",
                "schema_version": "orders-schema-v1",
                "row_count": order_count,
                "columns": column_payloads(order_columns),
            },
        ],
        "query": SAMPLE_QUERY,
        "settings": {
            "parallelism": 4,
            "memory_budget_bytes": 256000,
            "min_rows_per_task": 128,
        },
    }


def divergent_payload() -> dict[str, Any]:
    """A v2 state used only to prove restore replaces rather than merges."""

    payload = sample_payload("sales-2026-11-17.v2")
    orders = next(table for table in payload["tables"] if table["name"] == "orders")
    amount = next(column for column in orders["columns"] if column["name"] == "amount")
    amount["values"][2] = 999.0
    amount["values"][3] = 998.0
    return payload
