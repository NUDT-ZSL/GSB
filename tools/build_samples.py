#!/usr/bin/env python3
"""Build the fixed local sample and its SQLite shard files."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shardroute.rules import RuleSnapshot


SAMPLES = ROOT / "samples"
SHARDS = SAMPLES / "shards"
STANDBY = SHARDS / "standby"


ORDERS = [
    (1, 0, 10, "NEW"),
    (2, 1, 20, "PAID"),
    (3, 2, 30, "NEW"),
    (4, 3, 40, "PAID"),
    (5, 4, 50, "NEW"),
    (6, 5, 60, "PAID"),
    (7, 6, 70, "NEW"),
    (8, 7, 80, "PAID"),
    (9, 8, 90, "NEW"),
    (10, 9, 100, "PAID"),
    (11, 10, 110, "NEW"),
    (12, 11, 120, "PAID"),
]

EVENTS = [
    (1, 10, 1, "LOGIN"),
    (2, 500, 2, "VIEW"),
    (3, 1200, 3, "BUY"),
    (4, 1500, 4, "VIEW"),
    (5, 2300, 5, "BUY"),
    (6, 2700, 6, "VIEW"),
    (7, 3400, 7, "BUY"),
    (8, 3900, 8, "VIEW"),
]


def table_sql(name: str, generated: bool = False) -> str:
    if generated:
        return (
            f'CREATE TABLE "{name}" ('
            "event_id INTEGER PRIMARY KEY, "
            "event_no INTEGER NOT NULL, "
            "amount INTEGER NOT NULL, "
            "kind TEXT NOT NULL, "
            "bucket INTEGER GENERATED ALWAYS AS (event_no / 1000) VIRTUAL"
            ")"
        )
    return (
        f'CREATE TABLE "{name}" ('
        "id INTEGER PRIMARY KEY, "
        "customer_id INTEGER NOT NULL, "
        "amount INTEGER NOT NULL, "
        "status TEXT NOT NULL"
        ")"
    )


def write_db(
    path: Path,
    table: str,
    rows: list[tuple[Any, ...]],
    generated: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(table_sql(table, generated=generated))
        placeholders = ",".join("?" for _ in rows[0])
        connection.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', rows)
        connection.commit()
    finally:
        connection.close()


def orders_rows_for_mod(rows: list[tuple[Any, ...]], shard_count: int, shard_id: int) -> list[tuple[Any, ...]]:
    return [row for row in rows if int(row[1]) % shard_count == shard_id]


def events_rows_for_range(shard_id: int) -> list[tuple[Any, ...]]:
    boundaries = [0, 1000, 2000, 3000, 4000]
    return [
        row
        for row in EVENTS
        if boundaries[shard_id] <= int(row[1]) < boundaries[shard_id + 1]
    ]


def orders_columns() -> list[dict[str, Any]]:
    return [
        {"name": "id", "type": "INTEGER"},
        {"name": "customer_id", "type": "INTEGER"},
        {"name": "amount", "type": "INTEGER"},
        {"name": "status", "type": "TEXT"},
    ]


def events_columns() -> list[dict[str, Any]]:
    return [
        {"name": "event_id", "type": "INTEGER"},
        {"name": "event_no", "type": "INTEGER"},
        {"name": "amount", "type": "INTEGER"},
        {"name": "kind", "type": "TEXT"},
        {"name": "bucket", "type": "INTEGER", "generated": True},
    ]


def build_v1_snapshot() -> dict[str, Any]:
    mappings: list[dict[str, Any]] = []
    for shard_id in range(4):
        mappings.extend(
            [
                {
                    "database": f"shards/orders_{shard_id:02d}.db",
                    "logical_table": "orders",
                    "role": "primary",
                    "shard_id": shard_id,
                    "table": f"orders_{shard_id:02d}",
                },
                {
                    "database": f"shards/standby/orders_{shard_id:02d}_bak.db",
                    "logical_table": "orders",
                    "role": "standby",
                    "shard_id": shard_id,
                    "table": f"orders_{shard_id:02d}",
                },
                {
                    "database": f"shards/events_{shard_id:02d}.db",
                    "logical_table": "events",
                    "role": "primary",
                    "shard_id": shard_id,
                    "table": f"events_{shard_id:02d}",
                },
                {
                    "database": f"shards/standby/events_{shard_id:02d}_bak.db",
                    "logical_table": "events",
                    "role": "standby",
                    "shard_id": shard_id,
                    "table": f"events_{shard_id:02d}",
                },
            ]
        )
    return {
        "logical_tables": [
            {
                "columns": orders_columns(),
                "generates_columns": False,
                "name": "orders",
                "rule_type": "mod",
                "shard_count": 4,
                "shard_key": "customer_id",
            },
            {
                "boundaries": [0, 1000, 2000, 3000, 4000],
                "columns": events_columns(),
                "generates_columns": True,
                "name": "events",
                "rule_type": "range",
                "shard_count": 4,
                "shard_key": "event_no",
            },
        ],
        "physical_mappings": mappings,
    }


def build_v2_snapshot() -> dict[str, Any]:
    snapshot = build_v1_snapshot()
    snapshot["logical_tables"][0]["shard_count"] = 2
    snapshot["logical_tables"][0]["rule_type"] = "mod"
    mappings: list[dict[str, Any]] = []
    for shard_id in range(2):
        mappings.extend(
            [
                {
                    "database": f"shards/orders_v2_{shard_id:02d}.db",
                    "logical_table": "orders",
                    "role": "primary",
                    "shard_id": shard_id,
                    "table": f"orders_v2_{shard_id:02d}",
                },
                {
                    "database": f"shards/standby/orders_v2_{shard_id:02d}_bak.db",
                    "logical_table": "orders",
                    "role": "standby",
                    "shard_id": shard_id,
                    "table": f"orders_v2_{shard_id:02d}",
                },
            ]
        )
    for shard_id in range(4):
        mappings.extend(
            [
                {
                    "database": f"shards/events_{shard_id:02d}.db",
                    "logical_table": "events",
                    "role": "primary",
                    "shard_id": shard_id,
                    "table": f"events_{shard_id:02d}",
                },
                {
                    "database": f"shards/standby/events_{shard_id:02d}_bak.db",
                    "logical_table": "events",
                    "role": "standby",
                    "shard_id": shard_id,
                    "table": f"events_{shard_id:02d}",
                },
            ]
        )
    snapshot["physical_mappings"] = mappings
    return snapshot


def rule_package(
    version: str,
    snapshot: dict[str, Any],
    effective_clock_ms: int,
    rollback_target: str | None = None,
) -> dict[str, Any]:
    validated = RuleSnapshot.from_dict(snapshot)
    return {
        "digest": validated.content_digest(),
        "effective_clock_ms": effective_clock_ms,
        "logical_tables": snapshot["logical_tables"],
        "physical_mappings": snapshot["physical_mappings"],
        "rollback_target": rollback_target,
        "version": version,
    }


def build_queries() -> list[dict[str, Any]]:
    return [
        {
            "default_params": {"shard_key": 6},
            "id": "orders_eq",
            "logical_table": "orders",
            "operation": "select",
            "order_by": ["customer_id", "id"],
            "route_kind": "eq",
            "sql": (
                "SELECT id, customer_id, amount, status FROM orders "
                "WHERE {{shard_predicate}} ORDER BY customer_id, id"
            ),
        },
        {
            "default_params": {"end": 3, "start": 2},
            "id": "orders_range",
            "logical_table": "orders",
            "operation": "select",
            "order_by": ["customer_id", "id"],
            "route_kind": "range",
            "sql": (
                "SELECT id, customer_id, amount, status FROM orders "
                "WHERE {{shard_predicate}} ORDER BY customer_id, id"
            ),
        },
        {
            "default_params": {"values": [6, 1, 10]},
            "id": "orders_in",
            "logical_table": "orders",
            "operation": "select",
            "order_by": ["customer_id", "id"],
            "route_kind": "in",
            "sql": (
                "SELECT id, customer_id, amount, status FROM orders "
                "WHERE {{shard_predicate}} ORDER BY customer_id, id"
            ),
        },
        {
            "default_params": {},
            "id": "orders_all",
            "logical_table": "orders",
            "operation": "select",
            "order_by": ["customer_id", "id"],
            "route_kind": "all",
            "sql": "SELECT id, customer_id, amount, status FROM orders ORDER BY customer_id, id",
        },
        {
            "default_params": {},
            "id": "orders_sum_count",
            "logical_table": "orders",
            "operation": "sum_count",
            "order_by": [],
            "route_kind": "all",
            "sql": "SELECT COALESCE(SUM(amount), 0) AS total_amount, COUNT(*) AS row_count FROM orders",
        },
        {
            "default_params": {"end": 3, "start": 2},
            "id": "orders_agg_range",
            "logical_table": "orders",
            "operation": "sum_count",
            "order_by": [],
            "route_kind": "range",
            "sql": (
                "SELECT COALESCE(SUM(amount), 0) AS total_amount, COUNT(*) AS row_count "
                "FROM orders WHERE {{shard_predicate}}"
            ),
        },
        {
            "default_params": {"shard_key": 1500},
            "id": "events_eq",
            "logical_table": "events",
            "operation": "select",
            "order_by": ["event_no", "event_id"],
            "route_kind": "eq",
            "sql": (
                "SELECT event_id, event_no, amount, kind, bucket FROM events "
                "WHERE {{shard_predicate}} ORDER BY event_no, event_id"
            ),
        },
        {
            "default_params": {"end": 2400, "start": 900},
            "id": "events_range",
            "logical_table": "events",
            "operation": "select",
            "order_by": ["event_no", "event_id"],
            "route_kind": "range",
            "sql": (
                "SELECT event_id, event_no, amount, kind, bucket FROM events "
                "WHERE {{shard_predicate}} ORDER BY event_no, event_id"
            ),
        },
        {
            "default_params": {"values": [500, 1500, 3500]},
            "id": "events_in",
            "logical_table": "events",
            "operation": "select",
            "order_by": ["event_no", "event_id"],
            "route_kind": "in",
            "sql": (
                "SELECT event_id, event_no, amount, kind, bucket FROM events "
                "WHERE {{shard_predicate}} ORDER BY event_no, event_id"
            ),
        },
    ]


def build_injections() -> dict[str, Any]:
    return {
        "format_version": 1,
        "scenarios": [
            {
                "code": "E_RULE_VERSION_CONFLICT",
                "details": {"expected_rule_version": "r2"},
                "id": "route-rule-conflict",
                "match": {"params": {"shard_key": 6}, "template_id": "orders_eq"},
                "stage": "route",
            },
            {
                "code": "E_SHARD_KEY_OUT_OF_RANGE",
                "details": {"offending_shard_key": 99},
                "id": "rewrite-out-of-range",
                "match": {"params": {"end": 3, "start": 2}, "template_id": "orders_range"},
                "stage": "rewrite",
            },
            {
                "code": "E_PHYSICAL_TABLE_MISSING",
                "details": {"table": "orders_02", "simulated": True},
                "id": "execute-missing-table-partial",
                "match": {"template_id": "orders_all"},
                "shard_id": 2,
                "stage": "execute",
            },
            {
                "code": "E_PHYSICAL_TABLE_MISSING",
                "details": {"table": "orders_02", "simulated": True},
                "id": "execute-missing-table-total",
                "match": {"params": {"shard_key": 6}, "template_id": "orders_eq"},
                "shard_id": 2,
                "stage": "execute",
            },
            {
                "code": "E_RESULT_DIGEST_MISMATCH",
                "details": {"simulated": True},
                "id": "merge-digest-mismatch",
                "match": {"template_id": "orders_sum_count"},
                "shard_id": 0,
                "stage": "merge",
            },
        ],
    }


def main() -> None:
    SHARDS.mkdir(parents=True, exist_ok=True)
    STANDBY.mkdir(parents=True, exist_ok=True)
    for shard_id in range(4):
        rows = orders_rows_for_mod(ORDERS, 4, shard_id)
        table = f"orders_{shard_id:02d}"
        write_db(SHARDS / f"{table}.db", table, rows)
        write_db(STANDBY / f"{table}_bak.db", table, rows)
        event_rows = events_rows_for_range(shard_id)
        event_table = f"events_{shard_id:02d}"
        write_db(SHARDS / f"{event_table}.db", event_table, event_rows, generated=True)
        write_db(STANDBY / f"{event_table}_bak.db", event_table, event_rows, generated=True)
    for shard_id in range(2):
        rows = orders_rows_for_mod(ORDERS, 2, shard_id)
        table = f"orders_v2_{shard_id:02d}"
        write_db(SHARDS / f"{table}.db", table, rows)
        write_db(STANDBY / f"{table}_bak.db", table, rows)

    sample = {
        "format_version": 1,
        "initial_version": "r1",
        "query_templates": build_queries(),
        "rule_versions": [
            rule_package("r1", build_v1_snapshot(), 1000),
            rule_package("r2", build_v2_snapshot(), 2000),
            rule_package("r3-rollback", build_v1_snapshot(), 3000, rollback_target="r1"),
        ],
    }
    (SAMPLES / "sample.json").write_text(
        json.dumps(sample, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (SAMPLES / "injections.json").write_text(
        json.dumps(build_injections(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"built sample: {SAMPLES / 'sample.json'}")
    print(f"built injections: {SAMPLES / 'injections.json'}")


if __name__ == "__main__":
    main()

