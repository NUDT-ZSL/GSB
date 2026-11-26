#!/usr/bin/env python3
"""Generate the deterministic local sample used by the acceptance tests."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gsb_pipeline.sqlplan import parse_sql_plan  # noqa: E402
from gsb_pipeline.utils import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_text,
    canonical_json,
    pretty_json,
    sha256_bytes,
)

BASE_CLOCK_MS = 1_700_000_000_000

SOURCES = {
    "orders_raw": [
        "order_id",
        "customer_id",
        "amount",
        "status",
        "created_at",
        "channel",
        "region_code",
    ],
    "profiles_raw": ["customer_id", "segment", "region"],
    "adjustments_raw": ["adjustment_id", "order_id", "amount"],
}

VIEWS = {
    "raw_orders": (
        "SELECT order_id, customer_id, amount, status, created_at, channel "
        "FROM orders_raw"
    ),
    "paid_orders": (
        "SELECT order_id, customer_id, amount, created_at, channel "
        "FROM raw_orders WHERE status IN ('paid', 'settled')"
    ),
    "customer_totals": (
        "SELECT customer_id, SUM(amount) AS total_amount, "
        "COUNT(order_id) AS paid_count FROM paid_orders GROUP BY customer_id"
    ),
    "high_value_customers": (
        "SELECT customer_id, total_amount FROM customer_totals "
        "WHERE total_amount >= 100"
    ),
    "customer_profiles": (
        "SELECT customer_id, segment, region FROM profiles_raw"
    ),
    "customer_labels": (
        "SELECT c.customer_id, c.total_amount, p.segment AS segment "
        "FROM customer_totals c JOIN customer_profiles p "
        "ON c.customer_id = p.customer_id"
    ),
    "order_adjustments": (
        "SELECT a.order_id, SUM(a.amount) AS adjustment_amount "
        "FROM adjustments_raw a GROUP BY a.order_id"
    ),
    "net_orders": (
        "SELECT o.order_id, o.customer_id, "
        "o.amount - a.adjustment_amount AS net_amount "
        "FROM paid_orders o JOIN order_adjustments a "
        "ON o.order_id = a.order_id"
    ),
    "legacy_orders": "SELECT order_id, customer_id FROM raw_orders",
}

V1_VIEWS = {
    "raw_orders": {"generated_seconds": 5, "sql": VIEWS["raw_orders"]},
    "paid_orders": {
        "generated_seconds": 1,
        "sql": (
            "SELECT order_id, customer_id, amount, created_at, channel "
            "FROM raw_orders WHERE status = 'paid'"
        ),
    },
    "customer_totals": {
        "generated_seconds": 2,
        "sql": VIEWS["customer_totals"],
    },
    "high_value_customers": {
        "generated_seconds": 3,
        "sql": VIEWS["high_value_customers"],
    },
    "legacy_orders": {"generated_seconds": 4, "sql": VIEWS["legacy_orders"]},
}


def _columns_dependencies(sql: str) -> tuple[list[str], dict[str, list[dict[str, str]]], dict[str, Any]]:
    plan = parse_sql_plan(
        "sample",
        sql,
        {
            **SOURCES,
            **{
                name: parse_sql_plan(name, view_sql, SOURCES)["output_columns"]
                for name, view_sql in _ordered_view_sql()
            },
        },
    )
    return plan["output_columns"], plan["column_dependencies"], plan


def _ordered_view_sql() -> list[tuple[str, str]]:
    return [
        ("raw_orders", VIEWS["raw_orders"]),
        ("paid_orders", VIEWS["paid_orders"]),
        ("customer_totals", VIEWS["customer_totals"]),
        ("high_value_customers", VIEWS["high_value_customers"]),
        ("customer_profiles", VIEWS["customer_profiles"]),
        ("customer_labels", VIEWS["customer_labels"]),
        ("order_adjustments", VIEWS["order_adjustments"]),
        ("net_orders", VIEWS["net_orders"]),
        ("legacy_orders", VIEWS["legacy_orders"]),
    ]


def _build_plan_schemas() -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    schemas: dict[str, list[str]] = {name: list(columns) for name, columns in SOURCES.items()}
    plans: dict[str, dict[str, Any]] = {}
    for name, sql in _ordered_view_sql():
        plan = parse_sql_plan(name, sql, schemas, dependency_names=set(VIEWS))
        plans[name] = plan
        schemas[name] = plan["output_columns"]
    return plans, schemas


def _v1_snapshot(plans: dict[str, dict[str, Any]]) -> dict[str, Any]:
    views: list[dict[str, Any]] = []
    column_map: list[dict[str, Any]] = []
    for name, item in V1_VIEWS.items():
        plan = parse_sql_plan(
            name,
            item["sql"],
            {
                **SOURCES,
                **{
                    other_name: plans[other_name]["output_columns"]
                    for other_name in plans
                },
            },
            dependency_names=set(VIEWS),
        )
        views.append(
            {
                "name": name,
                "sql": item["sql"],
                "generated_seconds": item["generated_seconds"],
            }
        )
        for column in plan["output_columns"]:
            for dependency in plan["column_dependencies"][column]:
                column_map.append(
                    {
                        "view": name,
                        "column": column,
                        "upstream_view": dependency["source"],
                        "upstream_column": dependency["column"],
                        "generated_seconds": item["generated_seconds"],
                    }
                )
    return {
        "format_version": 1,
        "snapshot_generation": 1,
        "logical_clock_ms": BASE_CLOCK_MS + 5000,
        "injected_clock_ms": BASE_CLOCK_MS,
        "views": sorted(views, key=lambda item: item["name"]),
        "column_map": sorted(
            column_map,
            key=lambda item: (
                item["view"],
                item["column"],
                item["upstream_view"],
                item["upstream_column"],
            ),
        ),
    }


def _v2_view(
    name: str,
    plan: dict[str, Any],
    *,
    plan_generation: int,
    generated_at_ms: int,
) -> dict[str, Any]:
    return {
        "canonical_name": name,
        "plan_generation": plan_generation,
        "generated_at_ms": generated_at_ms,
        "logical_clock_ms": generated_at_ms,
        "sql": VIEWS[name],
        "columns": plan["output_columns"],
        "column_dependencies": plan["column_dependencies"],
    }


def _write_v2(
    sample_dir: Path,
    plans: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    shards_dir = sample_dir / "v2_shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    shards = [
        {
            "format_version": 2,
            "shard_id": "shard-0001",
            "plan_generation": 2,
            "logical_clock_ms": BASE_CLOCK_MS + 5000,
            "generated_at_ms": BASE_CLOCK_MS + 5000,
            "views": [
                _v2_view(
                    "raw_orders",
                    plans["raw_orders"],
                    plan_generation=2,
                    generated_at_ms=BASE_CLOCK_MS + 5000,
                ),
                _v2_view(
                    "customer_profiles",
                    plans["customer_profiles"],
                    plan_generation=2,
                    generated_at_ms=BASE_CLOCK_MS + 5000,
                ),
            ],
        },
        {
            "format_version": 2,
            "shard_id": "shard-0002",
            "plan_generation": 3,
            "logical_clock_ms": BASE_CLOCK_MS + 6000,
            "generated_at_ms": BASE_CLOCK_MS + 6000,
            "views": [
                _v2_view(
                    name,
                    plans[name],
                    plan_generation=3,
                    generated_at_ms=BASE_CLOCK_MS + 6000,
                )
                for name in [
                    "paid_orders",
                    "customer_totals",
                    "customer_labels",
                    "order_adjustments",
                    "net_orders",
                ]
            ],
        },
        {
            "format_version": 2,
            "shard_id": "shard-0003",
            "plan_generation": 4,
            "logical_clock_ms": BASE_CLOCK_MS + 7000,
            "generated_at_ms": BASE_CLOCK_MS + 7000,
            "views": [],
        },
    ]
    shard_meta: list[dict[str, Any]] = []
    view_meta: list[dict[str, Any]] = []
    for shard in shards:
        file_name = f"{shard['shard_id']}.json"
        payload = pretty_json(shard).encode("utf-8")
        atomic_write_bytes(shards_dir / file_name, payload)
        shard_meta.append(
            {
                "shard_id": shard["shard_id"],
                "file_name": file_name,
                "sha256": sha256_bytes(payload),
                "plan_generation": shard["plan_generation"],
                "logical_clock_ms": shard["logical_clock_ms"],
                "generated_at_ms": shard["generated_at_ms"],
            }
        )
        for view in shard["views"]:
            view_meta.append(
                {
                    "canonical_name": view["canonical_name"],
                    "plan_generation": view["plan_generation"],
                    "logical_clock_ms": view["logical_clock_ms"],
                    "generated_at_ms": view["generated_at_ms"],
                    "column_dependencies_json": canonical_json(
                        view["column_dependencies"]
                    ),
                    "tombstone": 0,
                    "shard_id": shard["shard_id"],
                }
            )
    view_meta.append(
        {
            "canonical_name": "legacy_orders",
            "plan_generation": 4,
            "logical_clock_ms": BASE_CLOCK_MS + 7000,
            "generated_at_ms": BASE_CLOCK_MS + 7000,
            "column_dependencies_json": "{}",
            "tombstone": 1,
            "shard_id": "shard-0003",
        }
    )
    tombstones = [
        {
            "view_name": "legacy_orders",
            "plan_generation": 4,
            "logical_clock_ms": BASE_CLOCK_MS + 7000,
            "generated_at_ms": BASE_CLOCK_MS + 7000,
            "reason": "retired_in_v2",
        }
    ]
    return shard_meta, view_meta, tombstones


def _write_meta_db(
    path: Path,
    shard_meta: list[dict[str, Any]],
    view_meta: list[dict[str, Any]],
    tombstones: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE shards (
                shard_id TEXT PRIMARY KEY,
                file_name TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                plan_generation INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                generated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE view_meta (
                canonical_name TEXT NOT NULL,
                plan_generation INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                column_dependencies_json TEXT NOT NULL,
                tombstone INTEGER NOT NULL,
                shard_id TEXT NOT NULL,
                PRIMARY KEY (canonical_name, plan_generation)
            );
            CREATE TABLE tombstones (
                view_name TEXT NOT NULL,
                plan_generation INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY (view_name, plan_generation)
            );
            """
        )
        meta = {
            "format_version": "2",
            "current_plan_generation": "4",
            "logical_clock_ms": str(BASE_CLOCK_MS + 7000),
            "clock_base_ms": str(BASE_CLOCK_MS),
        }
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            sorted(meta.items()),
        )
        connection.executemany(
            "INSERT INTO shards(shard_id, file_name, sha256, plan_generation, "
            "logical_clock_ms, generated_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    row["shard_id"],
                    row["file_name"],
                    row["sha256"],
                    row["plan_generation"],
                    row["logical_clock_ms"],
                    row["generated_at_ms"],
                )
                for row in shard_meta
            ],
        )
        connection.executemany(
            "INSERT INTO view_meta(canonical_name, plan_generation, logical_clock_ms, "
            "generated_at_ms, column_dependencies_json, tombstone, shard_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["canonical_name"],
                    row["plan_generation"],
                    row["logical_clock_ms"],
                    row["generated_at_ms"],
                    row["column_dependencies_json"],
                    row["tombstone"],
                    row["shard_id"],
                )
                for row in view_meta
            ],
        )
        connection.executemany(
            "INSERT INTO tombstones(view_name, plan_generation, logical_clock_ms, "
            "generated_at_ms, reason) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    row["view_name"],
                    row["plan_generation"],
                    row["logical_clock_ms"],
                    row["generated_at_ms"],
                    row["reason"],
                )
                for row in tombstones
            ],
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


def _patch_events() -> list[dict[str, Any]]:
    raw_orders_with_region = (
        "SELECT order_id, customer_id, amount, status, created_at, channel, "
        "region_code FROM orders_raw"
    )
    paid_orders_with_region = (
        "SELECT order_id, customer_id, amount, created_at, channel, region_code "
        "FROM raw_orders WHERE status IN ('paid', 'settled')"
    )
    return [
        {
            "patch_id": "patch-0001",
            "sequence": 1,
            "logical_clock_ms": BASE_CLOCK_MS + 8000,
            "op": "modify",
            "view": "raw_orders",
            "sql": raw_orders_with_region,
        },
        {
            "patch_id": "patch-0002",
            "sequence": 2,
            "logical_clock_ms": BASE_CLOCK_MS + 8100,
            "op": "modify",
            "view": "paid_orders",
            "sql": paid_orders_with_region,
        },
        {
            "patch_id": "patch-0003",
            "sequence": 3,
            "logical_clock_ms": BASE_CLOCK_MS + 8200,
            "op": "add",
            "view": "daily_orders",
            "sql": (
                "SELECT order_id, customer_id, amount, region_code "
                "FROM paid_orders WHERE amount > 0"
            ),
        },
        {
            "patch_id": "patch-0004",
            "sequence": 4,
            "logical_clock_ms": BASE_CLOCK_MS + 8300,
            "op": "modify",
            "view": "customer_totals",
            "sql": (
                "SELECT customer_id, SUM(amount) AS gross_amount, "
                "COUNT(order_id) AS paid_count FROM paid_orders "
                "GROUP BY customer_id"
            ),
        },
        {
            "patch_id": "patch-0005",
            "sequence": 5,
            "logical_clock_ms": BASE_CLOCK_MS + 8400,
            "op": "modify",
            "view": "customer_totals",
            "sql": (
                "SELECT customer_id, SUM(amount) AS total_amount, "
                "COUNT(DISTINCT order_id) AS paid_count FROM paid_orders "
                "GROUP BY customer_id"
            ),
        },
        {
            "patch_id": "patch-0006",
            "sequence": 6,
            "logical_clock_ms": BASE_CLOCK_MS + 8500,
            "op": "delete",
            "view": "high_value_customers",
        },
        {
            "patch_id": "patch-0007",
            "sequence": 7,
            "logical_clock_ms": BASE_CLOCK_MS + 8600,
            "op": "add",
            "view": "region_summary",
            "sql": (
                "SELECT region_code, SUM(amount) AS region_amount "
                "FROM raw_orders GROUP BY region_code"
            ),
        },
        {
            "patch_id": "patch-0003",
            "sequence": 3,
            "logical_clock_ms": BASE_CLOCK_MS + 8200,
            "op": "add",
            "view": "daily_orders",
            "sql": (
                "SELECT order_id, customer_id, amount, region_code "
                "FROM paid_orders WHERE amount > 0"
            ),
        },
        {
            "patch_id": "patch-0008",
            "sequence": 8,
            "logical_clock_ms": BASE_CLOCK_MS + 8700,
            "op": "modify",
            "view": "net_orders",
            "sql": (
                "SELECT o.order_id, o.customer_id, "
                "o.amount - a.adjustment_amount AS net_total "
                "FROM paid_orders o JOIN order_adjustments a "
                "ON o.order_id = a.order_id"
            ),
        },
        {
            "patch_id": "patch-0009",
            "sequence": 9,
            "logical_clock_ms": BASE_CLOCK_MS + 7000,
            "op": "add",
            "view": "clock_rollback_probe",
            "sql": "SELECT order_id FROM paid_orders",
        },
        {
            "patch_id": "patch-0010",
            "sequence": 10,
            "logical_clock_ms": BASE_CLOCK_MS + 8800,
            "op": "modify",
            "view": "paid_orders",
            "sql": (
                "SELECT customer_id AS order_id, customer_id, amount, "
                "created_at, channel, region_code FROM raw_orders "
                "WHERE status IN ('paid', 'settled')"
            ),
        },
    ]


def generate(sample_dir: Path) -> dict[str, Any]:
    sample_dir.mkdir(parents=True, exist_ok=True)
    plans, _schemas = _build_plan_schemas()
    definitions = {
        "format_version": 1,
        "sources": SOURCES,
        "views": [
            {
                "name": name,
                "sql": VIEWS[name],
                "upstreams": [],
            }
            for name in sorted(VIEWS)
        ],
    }
    atomic_write_text(sample_dir / "view_definitions.json", pretty_json(definitions))
    atomic_write_text(sample_dir / "v1_snapshot.json", pretty_json(_v1_snapshot(plans)))
    shard_meta, view_meta, tombstones = _write_v2(sample_dir, plans)
    _write_meta_db(sample_dir / "sqlite_meta.db", shard_meta, view_meta, tombstones)
    events = _patch_events()
    atomic_write_text(
        sample_dir / "patch_events.jsonl",
        "".join(canonical_json(event) + "\n" for event in events),
    )
    manifest = {
        "format": "gsb-seed-sample-v1",
        "seed": 20261127,
        "base_clock_ms": BASE_CLOCK_MS,
        "shard_hashes": {
            item["shard_id"]: item["sha256"] for item in shard_meta
        },
        "patch_count": len(events),
        "view_count": len(VIEWS),
    }
    atomic_write_text(sample_dir / "sample_manifest.json", pretty_json(manifest))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="sample", help="output sample directory")
    args = parser.parse_args()
    manifest = generate(Path(args.out))
    print(pretty_json({"ok": True, "sample_dir": str(Path(args.out)), "manifest": manifest}), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

