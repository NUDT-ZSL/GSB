#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slowplan.normalize import normalize_sql  # noqa: E402
from slowplan.stats import read_catalog, snapshot_content_digest  # noqa: E402


SAMPLES_DIR = Path(__file__).resolve().parent
DB_PATH = SAMPLES_DIR / "sample_db.sqlite"


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def build_database() -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                segment TEXT NOT NULL,
                region TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX idx_customers_segment ON customers(segment);
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                total_cents INTEGER NOT NULL
            );
            CREATE INDEX idx_orders_customer ON orders(customer_id);
            CREATE INDEX idx_orders_status ON orders(status);
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                category_id INTEGER NOT NULL,
                price_cents INTEGER NOT NULL
            );
            CREATE INDEX idx_products_category ON products(category_id);
            CREATE TABLE feature_flags (
                id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL,
                name TEXT NOT NULL
            );
            CREATE INDEX idx_flags_enabled ON feature_flags(enabled);
            """
        )
        segments = ["basic", "plus", "vip"]
        regions = ["east", "west", "north", "south"]
        statuses = ["paid", "pending", "cancelled", "refunded", "error"]
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO customers(id, segment, region, created_at) VALUES(?, ?, ?, ?)",
            [
                (index, segments[index % len(segments)], regions[index % len(regions)], 1600000000000 + index * 1000)
                for index in range(1, 601)
            ],
        )
        conn.executemany(
            "INSERT INTO orders(id, customer_id, status, created_at, total_cents) VALUES(?, ?, ?, ?, ?)",
            [
                (
                    index,
                    (index % 120) + 1,
                    statuses[index % len(statuses)],
                    1700000000000 + (index % 365) * 86400000,
                    500 + (index * 37) % 90000,
                )
                for index in range(1, 1201)
            ],
        )
        conn.executemany(
            "INSERT INTO products(id, category_id, price_cents) VALUES(?, ?, ?)",
            [
                (index, (index % 20) + 1, 100 + (index * 13) % 50000)
                for index in range(1, 401)
            ],
        )
        conn.executemany(
            "INSERT INTO feature_flags(id, enabled, name) VALUES(?, ?, ?)",
            [
                (index, 0 if index % 25 == 0 else 1, f"feature_{index:03d}")
                for index in range(1, 51)
            ],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def snapshot_for(catalog) -> dict:
    tables = {
        "customers": {
            "row_count": 600,
            "columns": {
                "id": {"ndv": 600},
                "segment": {"ndv": 3},
                "region": {"ndv": 4},
                "created_at": {"ndv": 600},
            },
            "indexes": {
                "idx_customers_segment": {
                    "columns": ["segment"],
                    "prefix_selectivity": 0.333333,
                }
            },
        },
        "orders": {
            "row_count": 1200,
            "columns": {
                "id": {"ndv": 1200},
                "customer_id": {"ndv": 120},
                "status": {"ndv": 5},
                "created_at": {"ndv": 1200},
                "total_cents": {"ndv": 1200},
            },
            "indexes": {
                "idx_orders_customer": {
                    "columns": ["customer_id"],
                    "prefix_selectivity": 0.008333,
                },
                "idx_orders_status": {
                    "columns": ["status"],
                    "prefix_selectivity": 0.2,
                },
            },
        },
        "products": {
            "row_count": 400,
            "columns": {
                "id": {"ndv": 400},
                "category_id": {"ndv": 20},
                "price_cents": {"ndv": 400},
            },
            "indexes": {
                "idx_products_category": {
                    "columns": ["category_id"],
                    "prefix_selectivity": 0.075,
                }
            },
        },
        "feature_flags": {
            "row_count": 50,
            "columns": {
                "id": {"ndv": 50},
                "enabled": {"ndv": 2},
                "name": {"ndv": 50},
            },
            "indexes": {
                "idx_flags_enabled": {
                    "columns": ["enabled"],
                    "prefix_selectivity": 0.96,
                }
            },
        },
    }
    snapshot = {
        "version": 1,
        "collected_at_ms": 0,
        "effective_at_ms": 0,
        "database_digest": catalog.digest,
        "tables": tables,
    }
    snapshot["content_digest"] = snapshot_content_digest(snapshot)
    return snapshot


def patched_snapshot(
    base: dict,
    target_version: int,
    collected_at_ms: int,
    effective_at_ms: int,
    changes: list[dict],
) -> tuple[dict, list[dict]]:
    result = copy.deepcopy(base)
    result["version"] = target_version
    result["collected_at_ms"] = collected_at_ms
    result["effective_at_ms"] = effective_at_ms
    for change in changes:
        kind = change["kind"]
        table = change["table"]
        if kind == "table_row_count":
            result["tables"][table]["row_count"] = change["value"]
        elif kind == "column_ndv":
            result["tables"][table]["columns"][change["column"]]["ndv"] = change["value"]
        elif kind == "index_prefix_selectivity":
            result["tables"][table]["indexes"][change["index"]]["prefix_selectivity"] = change["value"]
        else:
            raise ValueError(kind)
    result.pop("content_digest", None)
    result["content_digest"] = snapshot_content_digest(result)
    return result, changes


def sample(
    seq: int,
    clock: int,
    sql: str,
    duration: int,
    scanned: int,
    returned: int,
    index_hit: str | None,
    session: str,
) -> dict:
    normalized = normalize_sql(sql)
    return {
        "sample_seq": seq,
        "collected_at_ms": clock,
        "sql": sql,
        "template_id": normalized.template_id,
        "duration_ms": duration,
        "rows_scanned": scanned,
        "rows_returned": returned,
        "index_hit": index_hit,
        "session_id": session,
    }


def initial_samples() -> list[dict]:
    return [
        sample(1, 1000, "SELECT * FROM orders WHERE customer_id = 17", 81, 10, 10, "idx_orders_customer", "s1"),
        sample(
            2,
            1050,
            "SELECT * FROM orders WHERE status = 'paid' AND created_at >= 1700000000000",
            190,
            80,
            24,
            "idx_orders_status",
            "s1",
        ),
        sample(3, 1100, "SELECT * FROM products WHERE category_id IN (1, 2, 3)", 60, 30, 30, "idx_products_category", "s2"),
        sample(4, 1200, "SELECT * FROM orders WHERE customer_id = 101", 91, 11, 11, "idx_orders_customer", "s2"),
        sample(5, 1300, "SELECT * FROM feature_flags WHERE enabled = 1", 20, 48, 48, "idx_flags_enabled", "s3"),
        sample(
            6,
            1400,
            "SELECT * FROM orders WHERE status = 'pending' AND created_at >= 1700086400000",
            210,
            84,
            25,
            "idx_orders_status",
            "s1",
        ),
        sample(
            7,
            1500,
            "SELECT * FROM products WHERE category_id IN (2, 4, 6, 8, 10)",
            72,
            31,
            31,
            "idx_products_category",
            "s2",
        ),
        sample(8, 1600, "SELECT * FROM orders WHERE customer_id = 55", 76, 9, 9, "idx_orders_customer", "s3"),
        sample(
            9,
            1700,
            "SELECT * FROM orders WHERE status = 'refunded' AND created_at >= 1700172800000",
            205,
            79,
            24,
            "idx_orders_status",
            "s2",
        ),
        sample(10, 1800, "SELECT * FROM feature_flags WHERE enabled = 0", 18, 50, 50, "idx_flags_enabled", "s3"),
    ]


def refreshed_samples() -> list[dict]:
    return [
        sample(11, 2500, "SELECT * FROM orders WHERE customer_id = 7", 70, 10, 10, "idx_orders_customer", "s4"),
        sample(
            12,
            2600,
            "SELECT * FROM orders WHERE status = 'paid' AND created_at >= 1700259200000",
            150,
            24,
            24,
            "idx_orders_status",
            "s4",
        ),
        sample(13, 2700, "SELECT * FROM products WHERE category_id IN (3, 6, 9)", 55, 30, 30, "idx_products_category", "s5"),
        sample(14, 3100, "SELECT * FROM orders WHERE customer_id = 93", 73, 10, 10, "idx_orders_customer", "s5"),
        sample(
            15,
            3200,
            "SELECT * FROM orders WHERE status = 'error' AND created_at >= 1700345600000",
            145,
            25,
            25,
            "idx_orders_status",
            "s4",
        ),
        sample(16, 3300, "SELECT * FROM feature_flags WHERE enabled = 1", 15, 46, 46, "idx_flags_enabled", "s5"),
    ]


def main() -> None:
    build_database()
    catalog = read_catalog(str(DB_PATH))
    snapshot = snapshot_for(catalog)
    write_json(SAMPLES_DIR / "stats_snapshot.json", snapshot)

    initial = initial_samples()
    refreshed = refreshed_samples()
    write_json(SAMPLES_DIR / "slow_queries.json", {"samples": initial})
    write_json(SAMPLES_DIR / "slow_queries_after_refresh.json", {"samples": refreshed})
    invalid_sql = "SELECT * FROM orders WHERE note = 'unterminated"
    write_json(
        SAMPLES_DIR / "slow_queries_invalid.json",
        {
            "samples": [
                {
                    "sample_seq": 1,
                    "collected_at_ms": 1000,
                    "sql": invalid_sql,
                    "template_id": "tpl_invalid",
                    "duration_ms": 10,
                    "rows_scanned": 1,
                    "rows_returned": 1,
                    "index_hit": None,
                    "session_id": "bad",
                }
            ]
        },
    )

    patch_changes = [
        {"kind": "column_ndv", "table": "orders", "column": "status", "value": 8},
        {"kind": "index_prefix_selectivity", "table": "orders", "index": "idx_orders_status", "value": 0.06},
        {"kind": "index_prefix_selectivity", "table": "feature_flags", "index": "idx_flags_enabled", "value": 0.92},
    ]
    v2_snapshot, _ = patched_snapshot(snapshot, 2, 2200, 2500, patch_changes)
    patch_v2 = {
        "target_version": 2,
        "base_version": 1,
        "collected_at_ms": 2200,
        "effective_at_ms": 2500,
        "content_digest": v2_snapshot["content_digest"],
        "changes": patch_changes,
    }
    write_json(SAMPLES_DIR / "stats_patch_v2.json", patch_v2)

    zero_changes = [
        {
            "kind": "index_prefix_selectivity",
            "table": "orders",
            "index": "idx_orders_customer",
            "value": 0.0,
        }
    ]
    zero_snapshot, _ = patched_snapshot(snapshot, 2, 2200, 2500, zero_changes)
    write_json(
        SAMPLES_DIR / "stats_patch_zero_estimate.json",
        {
            "target_version": 2,
            "base_version": 1,
            "collected_at_ms": 2200,
            "effective_at_ms": 2500,
            "content_digest": zero_snapshot["content_digest"],
            "changes": zero_changes,
        },
    )
    write_json(
        SAMPLES_DIR / "slow_queries_zero_case.json",
        {
            "samples": [
                sample(
                    101,
                    3400,
                    "SELECT * FROM orders WHERE customer_id = 8",
                    75,
                    10,
                    10,
                    "idx_orders_customer",
                    "zero",
                )
            ]
        },
    )

    write_json(
        SAMPLES_DIR / "stats_patch_rollback.json",
        {
            "target_version": 1,
            "base_version": 2,
            "collected_at_ms": 2200,
            "effective_at_ms": 2500,
            "content_digest": snapshot["content_digest"],
            "changes": [
                {"kind": "column_ndv", "table": "orders", "column": "status", "value": 5}
            ],
        },
    )
    write_json(
        SAMPLES_DIR / "stats_patch_unknown_column.json",
        {
            "target_version": 2,
            "base_version": 1,
            "collected_at_ms": 2200,
            "effective_at_ms": 2500,
            "content_digest": "0" * 64,
            "changes": [
                {"kind": "column_ndv", "table": "orders", "column": "does_not_exist", "value": 5}
            ],
        },
    )
    write_json(
        SAMPLES_DIR / "stats_patch_conflict.json",
        {
            "target_version": 3,
            "base_version": 1,
            "collected_at_ms": 2300,
            "effective_at_ms": 2600,
            "content_digest": "1" * 64,
            "changes": [
                {"kind": "column_ndv", "table": "orders", "column": "status", "value": 9}
            ],
        },
    )
    bad_digest = copy.deepcopy(snapshot)
    bad_digest["database_digest"] = "0" * 64
    bad_digest.pop("content_digest", None)
    bad_digest["content_digest"] = snapshot_content_digest(bad_digest)
    write_json(SAMPLES_DIR / "stats_snapshot_bad_digest.json", bad_digest)

    print(
        json.dumps(
            {
                "sample_db": str(DB_PATH),
                "database_digest": catalog.digest,
                "stats_version": snapshot["version"],
                "initial_samples": len(initial),
                "refreshed_samples": len(refreshed),
                "templates": sorted({item["template_id"] for item in initial + refreshed}),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
