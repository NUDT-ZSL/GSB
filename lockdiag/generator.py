"""Deterministic local sample generator."""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path
from typing import Any

from .util import ensure_dir, pretty_json


DEFAULT_SEED = 20261122
RAW_UNITS_PER_MS = 7
CLOCK_START = 7000
CLOCK_STEP = 1
CLOCK_LENGTH = 50_000


def _dump(path: Path, value: Any) -> None:
    path.write_text(pretty_json(value) + "\n", encoding="utf-8", newline="\n")


def _sample_schema() -> dict:
    return {
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True},
                    {"name": "name", "type": "TEXT", "primary_key": False},
                    {"name": "region", "type": "TEXT", "primary_key": False},
                    {"name": "balance", "type": "INTEGER", "primary_key": False},
                ],
            },
            {
                "name": "orders",
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True},
                    {"name": "customer_id", "type": "INTEGER", "primary_key": False},
                    {"name": "amount", "type": "INTEGER", "primary_key": False},
                    {"name": "status", "type": "TEXT", "primary_key": False},
                ],
            },
            {
                "name": "audit",
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True},
                    {"name": "note", "type": "TEXT", "primary_key": False},
                ],
            },
            {
                "name": "settings",
                "columns": [
                    {"name": "key", "type": "TEXT", "primary_key": True},
                    {"name": "value", "type": "TEXT", "primary_key": False},
                ],
            },
        ],
        "indexes": [
            {
                "name": "idx_customers_region",
                "table": "customers",
                "columns": ["region"],
                "unique": False,
            },
            {
                "name": "idx_orders_customer",
                "table": "orders",
                "columns": ["customer_id"],
                "unique": False,
            },
            {
                "name": "idx_orders_status",
                "table": "orders",
                "columns": ["status"],
                "unique": False,
            },
        ],
    }


def _transactions() -> dict:
    transactions = [
        {
            "tx_id": "T1",
            "start_order": 1,
            "version": 1,
            "isolation": "SERIALIZABLE",
            "budget_ms": 4,
            "depends_on": [],
            "operations": [
                {
                    "op_id": "T1-1",
                    "op": "read",
                    "table": "customers",
                    "column": "id",
                    "value": 1,
                    "row_id": 1,
                    "lock": "shared",
                },
                {
                    "op_id": "T1-2",
                    "op": "write",
                    "table": "customers",
                    "row_id": 2,
                    "column": "balance",
                    "value": 120,
                    "lock": "exclusive",
                },
                {
                    "op_id": "T1-3",
                    "op": "read",
                    "table": "orders",
                    "column": "status",
                    "value": "paid",
                    "row_id": 1,
                    "lock": "shared",
                },
                {
                    "op_id": "T1-4",
                    "op": "scan",
                    "table": "orders",
                    "column": "customer_id",
                    "start": 1,
                    "end": 3,
                    "lock": "shared",
                },
                {
                    "op_id": "T1-5",
                    "op": "read",
                    "table": "audit",
                    "column": "id",
                    "value": 1,
                    "row_id": 1,
                    "lock": "shared",
                },
                {
                    "op_id": "T1-6",
                    "op": "write",
                    "table": "audit",
                    "row_id": 1,
                    "column": "note",
                    "value": "T1-commit",
                    "lock": "exclusive",
                },
                {
                    "op_id": "T1-7",
                    "op": "read",
                    "table": "customers",
                    "column": "region",
                    "value": "west",
                    "row_id": 3,
                    "lock": "shared",
                },
            ],
        },
        {
            "tx_id": "T2",
            "start_order": 2,
            "version": 1,
            "isolation": "SERIALIZABLE",
            "budget_ms": 2,
            "depends_on": [],
            "operations": [
                {
                    "op_id": "T2-1",
                    "op": "write",
                    "table": "customers",
                    "row_id": 2,
                    "column": "balance",
                    "value": 220,
                    "lock": "exclusive",
                },
                {
                    "op_id": "T2-2",
                    "op": "write",
                    "table": "customers",
                    "row_id": 1,
                    "column": "balance",
                    "value": 210,
                    "lock": "exclusive",
                },
            ],
        },
        {
            "tx_id": "T3",
            "start_order": 3,
            "version": 1,
            "isolation": "SNAPSHOT",
            "budget_ms": 3,
            "depends_on": [],
            "operations": [
                {
                    "op_id": "T3-1",
                    "op": "scan",
                    "table": "orders",
                    "column": "customer_id",
                    "start": 1,
                    "end": 3,
                    "lock": "shared",
                },
                {
                    "op_id": "T3-2",
                    "op": "write",
                    "table": "orders",
                    "row_id": 2,
                    "column": "amount",
                    "value": 310,
                    "lock": "exclusive",
                },
                {
                    "op_id": "T3-3",
                    "op": "read",
                    "table": "customers",
                    "column": "id",
                    "value": 3,
                    "row_id": 3,
                    "lock": "shared",
                },
                {
                    "op_id": "T3-4",
                    "op": "read",
                    "table": "orders",
                    "column": "status",
                    "value": "paid",
                    "row_id": 1,
                    "lock": "shared",
                },
                {
                    "op_id": "T3-5",
                    "op": "scan",
                    "table": "orders",
                    "column": "customer_id",
                    "start": 4,
                    "end": 6,
                    "lock": "shared",
                },
                {
                    "op_id": "T3-6",
                    "op": "read",
                    "table": "audit",
                    "column": "id",
                    "value": 2,
                    "row_id": 2,
                    "lock": "shared",
                },
            ],
        },
        {
            "tx_id": "T4",
            "start_order": 4,
            "version": 1,
            "isolation": "READ_COMMITTED",
            "budget_ms": 1,
            "depends_on": [],
            "operations": [
                {
                    "op_id": "T4-1",
                    "op": "write",
                    "table": "orders",
                    "row_id": 2,
                    "column": "amount",
                    "value": 410,
                    "lock": "exclusive",
                },
                {
                    "op_id": "T4-2",
                    "op": "read",
                    "table": "customers",
                    "column": "region",
                    "value": "east",
                    "row_id": 1,
                    "lock": "shared",
                },
            ],
        },
        {
            "tx_id": "T5",
            "start_order": 5,
            "version": 1,
            "isolation": "READ_COMMITTED",
            "budget_ms": 2,
            "depends_on": [],
            "operations": [
                {
                    "op_id": "T5-1",
                    "op": "lock_decl",
                    "resource_type": "table",
                    "table": "settings",
                    "mode": "exclusive",
                },
                {
                    "op_id": "T5-2",
                    "op": "read",
                    "table": "customers",
                    "column": "region",
                    "value": "north",
                    "row_id": 4,
                    "lock": "shared",
                },
                {"op_id": "T5-3", "op": "rollback", "reason": "demo_active_rollback"},
            ],
        },
    ]
    return {
        "format_version": "1.0",
        "scripts_version": 1,
        "transactions": transactions,
    }


def _config(seed: int) -> dict:
    return {
        "format_version": "1.0",
        "seed": seed,
        "max_concurrent_transactions": 2,
        "worker_processes": 3,
        "default_lock_wait_budget_ms": 3,
        "deadlock_check_period": 1,
        "fixed_scale": 2,
        "wait_tolerance_ms": "0.30",
        "max_scheduler_steps": 1000,
        "clock_file": "clock.json",
    }


def _clock() -> dict:
    samples = [CLOCK_START + index * CLOCK_STEP for index in range(CLOCK_LENGTH)]
    return {
        "format_version": "1.0",
        "raw_units_per_ms": RAW_UNITS_PER_MS,
        "step_units": CLOCK_STEP,
        "samples": samples,
    }


def generate_sample(out_dir: str | Path, seed: int = DEFAULT_SEED) -> dict:
    """Generate a complete sample directory and return its manifest summary."""

    out = Path(out_dir)
    ensure_dir(out)
    rng = random.Random(seed)
    db_path = out / "sample.db"
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(str(db_path))
    try:
        connection.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                region TEXT NOT NULL,
                balance INTEGER NOT NULL
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE audit (
                id INTEGER PRIMARY KEY,
                note TEXT NOT NULL
            );
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX idx_customers_region ON customers(region);
            CREATE INDEX idx_orders_customer ON orders(customer_id);
            CREATE INDEX idx_orders_status ON orders(status);
            """
        )
        regions = ["east", "west", "north", "south"]
        for row_id in range(1, 13):
            connection.execute(
                "INSERT INTO customers(id, name, region, balance) VALUES (?, ?, ?, ?)",
                (
                    row_id,
                    f"customer-{row_id:02d}-{rng.randrange(1000, 9999)}",
                    regions[(row_id - 1) % len(regions)],
                    rng.randrange(10, 1000),
                ),
            )
        statuses = ["paid", "open", "void"]
        for row_id in range(1, 25):
            connection.execute(
                "INSERT INTO orders(id, customer_id, amount, status) VALUES (?, ?, ?, ?)",
                (
                    row_id,
                    ((row_id - 1) % 8) + 1,
                    rng.randrange(10, 500),
                    statuses[row_id % len(statuses)],
                ),
            )
        connection.execute("INSERT INTO audit(id, note) VALUES (1, 'seed-audit-1')")
        connection.execute("INSERT INTO audit(id, note) VALUES (2, 'seed-audit-2')")
        connection.execute("INSERT INTO settings(key, value) VALUES ('mode', 'diagnostic')")
        connection.execute("INSERT INTO settings(key, value) VALUES ('revision', '1')")
        connection.commit()
    finally:
        connection.close()

    _dump(out / "schema.json", _sample_schema())
    _dump(out / "transactions.json", _transactions())
    _dump(out / "config.json", _config(seed))
    _dump(out / "clock.json", _clock())
    manifest = {
        "seed": seed,
        "database": str(db_path.name),
        "schema": "schema.json",
        "transactions": "transactions.json",
        "config": "config.json",
        "clock": "clock.json",
    }
    _dump(out / "manifest.json", manifest)
    return manifest
