#!/usr/bin/env python3
"""Rebuild the deterministic SQLite sample database."""

from __future__ import annotations

import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "sample.db"

SCHEMA = """
PRAGMA journal_mode = DELETE;
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    region TEXT NOT NULL,
    tier TEXT NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    amount REAL NOT NULL,
    created_ms INTEGER NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE INDEX idx_customers_region ON customers(region);
CREATE INDEX idx_orders_customer_status ON orders(customer_id, status);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_created_ms ON orders(created_ms);
"""


def rebuild(db_path: Path | str = DB_PATH) -> None:
    path = Path(db_path)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        statuses = ("new", "paid", "shipped", "cancelled")
        customer_rows = []
        for customer_id in range(1, 121):
            customer_rows.append(
                (
                    customer_id,
                    f"region-{customer_id % 5}",
                    f"tier-{customer_id % 3}",
                    f"customer-{customer_id:03d}",
                )
            )
        connection.executemany(
            "INSERT INTO customers(id, region, tier, name) VALUES (?, ?, ?, ?)",
            customer_rows,
        )
        order_rows = []
        for order_id in range(1, 601):
            order_rows.append(
                (
                    order_id,
                    ((order_id * 17) % 120) + 1,
                    statuses[order_id % 4],
                    ((order_id * 37) % 1000) + 0.5,
                    order_id * 1000,
                )
            )
        connection.executemany(
            """
            INSERT INTO orders(id, customer_id, status, amount, created_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            order_rows,
        )
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    rebuild()
    print(DB_PATH)
