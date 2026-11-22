#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def tx(
    tx_id: int,
    tenant: str,
    start_clock_ms: int,
    writes: list[dict],
    isolation: str = "read_committed",
) -> dict:
    return {
        "tx_id": tx_id,
        "tenant": tenant,
        "start_clock_ms": start_clock_ms,
        "isolation": isolation,
        "writes": writes,
    }


def schema() -> dict:
    return {
        "format": "warbiter-schema-v1",
        "tenants": [
            {
                "name": "acme",
                "tables": [
                    {
                        "name": "inventory",
                        "primary_key": "sku",
                        "columns": [
                            {"name": "sku", "type": "string", "nullable": False},
                            {"name": "qty", "type": "integer", "nullable": True},
                            {"name": "price", "type": "number", "nullable": True},
                            {"name": "note", "type": "string", "nullable": True},
                            {"name": "status", "type": "string", "nullable": True},
                            {"name": "owner", "type": "string", "nullable": True},
                        ],
                        "conflict_policy": {
                            "version_label": "inventory-v1",
                            "row_strategy": "last_write_wins",
                            "row_overwrite_columns": ["price", "status"],
                            "column_strategies": {
                                "qty": "column_merge",
                                "note": "column_merge",
                                "owner": "last_write_wins",
                            },
                            "same_column_merge": "last_write_wins",
                            "allowed_strategies": [
                                "last_write_wins",
                                "column_merge",
                                "reject_rollback",
                                "manual_review",
                            ],
                        },
                    },
                    {
                        "name": "documents",
                        "primary_key": "doc_id",
                        "columns": [
                            {"name": "doc_id", "type": "string", "nullable": False},
                            {"name": "body", "type": "string", "nullable": True},
                            {"name": "reviewer", "type": "string", "nullable": True},
                        ],
                        "conflict_policy": {
                            "version_label": "documents-v1",
                            "row_strategy": "last_write_wins",
                            "row_overwrite_columns": ["body"],
                            "column_strategies": {"reviewer": "manual_review"},
                            "same_column_merge": "last_write_wins",
                            "allowed_strategies": [
                                "last_write_wins",
                                "reject_rollback",
                                "manual_review",
                            ],
                        },
                    },
                ],
            },
            {
                "name": "globex",
                "tables": [
                    {
                        "name": "orders",
                        "primary_key": "order_id",
                        "columns": [
                            {"name": "order_id", "type": "string", "nullable": False},
                            {"name": "amount", "type": "integer", "nullable": True},
                            {"name": "state", "type": "string", "nullable": True},
                            {"name": "memo", "type": "string", "nullable": True},
                        ],
                        "conflict_policy": {
                            "version_label": "orders-v1",
                            "row_strategy": "reject_rollback",
                            "row_overwrite_columns": ["amount", "state"],
                            "column_strategies": {"memo": "column_merge"},
                            "same_column_merge": "last_write_wins",
                            "allowed_strategies": [
                                "last_write_wins",
                                "column_merge",
                                "reject_rollback",
                            ],
                        },
                    },
                    {
                        "name": "metrics",
                        "primary_key": "id",
                        "columns": [
                            {"name": "id", "type": "integer", "nullable": False},
                            {"name": "value", "type": "integer", "nullable": True},
                            {"name": "label", "type": "string", "nullable": True},
                        ],
                        "conflict_policy": {
                            "version_label": "metrics-v1",
                            "row_strategy": "last_write_wins",
                            "row_overwrite_columns": ["label"],
                            "column_strategies": {"value": "column_merge"},
                            "same_column_merge": "last_write_wins",
                            "allowed_strategies": [
                                "last_write_wins",
                                "column_merge",
                            ],
                        },
                    },
                ],
            },
        ],
    }


def schema_policy_v2() -> dict:
    value = schema()
    for tenant in value["tenants"]:
        for table in tenant["tables"]:
            if tenant["name"] == "globex" and table["name"] == "orders":
                table["conflict_policy"]["version_label"] = "orders-v2"
                table["conflict_policy"]["row_strategy"] = "last_write_wins"
            if tenant["name"] == "acme" and table["name"] == "inventory":
                table["conflict_policy"]["version_label"] = "inventory-v2"
                table["conflict_policy"]["column_strategies"]["qty"] = "reject_rollback"
    return value


def config() -> dict:
    return {
        "format": "warbiter-config-v1",
        "conflict_tolerance_ms": 5,
        "logical_clock_step_ms": 10,
        "max_conflict_replay": 3,
        "ledger_retention_generations": 8,
        "assertion_failure_action": "rollback",
    }


def transactions() -> dict:
    return {
        "format": "warbiter-script-v1",
        "transactions": [
            tx(
                1001,
                "acme",
                1000,
                [
                    {
                        "op": "insert",
                        "table": "inventory",
                        "row_key": "SKU-1",
                        "values": {
                            "sku": "SKU-1",
                            "qty": 12,
                            "price": 10.5,
                            "note": "seed",
                            "status": "available",
                            "owner": "ops",
                        },
                    }
                ],
            ),
            tx(
                1002,
                "acme",
                1002,
                [
                    {
                        "op": "update",
                        "table": "inventory",
                        "row_key": "SKU-1",
                        "values": {"qty": 7, "note": "hot"},
                    },
                    {
                        "op": "update",
                        "table": "inventory",
                        "row_key": "SKU-1",
                        "values": {"qty": 8},
                    },
                ],
            ),
            tx(
                1003,
                "acme",
                1004,
                [
                    {
                        "op": "conditional_update",
                        "table": "inventory",
                        "row_key": "SKU-1",
                        "values": {"status": "hold"},
                        "assertion": {
                            "column": "qty",
                            "op": "ge",
                            "value": 10,
                        },
                        "recorded_observation": {
                            "qty": 12,
                            "status": "available",
                        },
                    }
                ],
                isolation="serializable",
            ),
            tx(
                1004,
                "acme",
                1006,
                [
                    {
                        "op": "insert",
                        "table": "documents",
                        "row_key": "DOC-1",
                        "values": {
                            "doc_id": "DOC-1",
                            "body": "seed",
                            "reviewer": "ops",
                        },
                    }
                ],
            ),
            tx(
                1005,
                "acme",
                1008,
                [
                    {
                        "op": "update",
                        "table": "documents",
                        "row_key": "DOC-1",
                        "values": {"reviewer": "alice"},
                    }
                ],
            ),
            tx(
                1006,
                "acme",
                1010,
                [
                    {
                        "op": "insert",
                        "table": "inventory",
                        "row_key": "SKU-3",
                        "values": {
                            "sku": "SKU-3",
                            "qty": 1,
                            "price": 1,
                            "note": "base",
                            "status": "new",
                            "owner": "ops",
                        },
                    }
                ],
            ),
            tx(
                1007,
                "acme",
                1012,
                [
                    {
                        "op": "update",
                        "table": "inventory",
                        "row_key": "SKU-3",
                        "values": {"qty": 2},
                    }
                ],
            ),
            tx(
                1008,
                "acme",
                1014,
                [
                    {
                        "op": "insert",
                        "table": "inventory",
                        "row_key": "SKU-3",
                        "values": {
                            "sku": "SKU-3",
                            "qty": 9,
                            "price": 1.2,
                            "note": "replacement",
                            "status": "revised",
                            "owner": "ops",
                        },
                    }
                ],
            ),
            tx(
                1009,
                "acme",
                1015,
                [
                    {
                        "op": "update",
                        "table": "inventory",
                        "row_key": "SKU-1",
                        "values": {"price": 11.0},
                    }
                ],
            ),
            tx(
                1101,
                "globex",
                1017,
                [
                    {
                        "op": "insert",
                        "table": "orders",
                        "row_key": "O-1",
                        "values": {
                            "order_id": "O-1",
                            "amount": 100,
                            "state": "new",
                            "memo": "seed",
                        },
                    }
                ],
            ),
            tx(
                1102,
                "globex",
                1019,
                [
                    {
                        "op": "update",
                        "table": "orders",
                        "row_key": "O-1",
                        "values": {"state": "paid"},
                    }
                ],
            ),
            tx(
                1103,
                "globex",
                1021,
                [
                    {
                        "op": "update",
                        "table": "orders",
                        "row_key": "O-1",
                        "values": {"memo": "late"},
                    }
                ],
            ),
            tx(
                1201,
                "globex",
                1023,
                [
                    {
                        "op": "insert",
                        "table": "metrics",
                        "row_key": 1,
                        "values": {"id": 1, "value": 10, "label": "a"},
                    }
                ],
            ),
            tx(
                1202,
                "globex",
                1025,
                [
                    {
                        "op": "update",
                        "table": "metrics",
                        "row_key": 1,
                        "values": {"value": 11},
                    }
                ],
            ),
            tx(
                1203,
                "globex",
                1027,
                [
                    {
                        "op": "update",
                        "table": "metrics",
                        "row_key": 1,
                        "values": {"label": "b"},
                    }
                ],
            ),
            tx(
                1204,
                "globex",
                1029,
                [
                    {
                        "op": "conditional_update",
                        "table": "metrics",
                        "row_key": 1,
                        "values": {"value": 12},
                        "assertion": {
                            "column": "value",
                            "op": "ge",
                            "value": 10,
                        },
                        "recorded_observation": {"value": 10, "label": "a"},
                    }
                ],
            ),
            tx(
                1205,
                "globex",
                1031,
                [
                    {
                        "op": "insert",
                        "table": "metrics",
                        "row_key": 2,
                        "values": {"id": 2, "value": 20, "label": "x"},
                    }
                ],
            ),
            tx(
                1206,
                "globex",
                1033,
                [
                    {
                        "op": "delete",
                        "table": "metrics",
                        "row_key": 2,
                    }
                ],
            ),
        ],
    }


def concurrent_transactions() -> dict[int, dict]:
    result = {}
    for index, start in enumerate([2000, 2001, 2002, 2003], start=1):
        tx_id = 2000 + index
        result[tx_id] = {
            "format": "warbiter-script-v1",
            "transactions": [
                tx(
                    tx_id,
                    "globex",
                    start,
                    [
                        {
                            "op": "update",
                            "table": "metrics",
                            "row_key": 1,
                            "values": {"value": index},
                        }
                    ],
                )
            ],
        }
    return result


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    write_json(SAMPLES / "schema.json", schema())
    write_json(SAMPLES / "schema_policy_v2.json", schema_policy_v2())
    write_json(SAMPLES / "arbiter.json", config())
    write_json(SAMPLES / "transactions.json", transactions())
    concurrent_dir = SAMPLES / "transactions_concurrent"
    for tx_id, document in concurrent_transactions().items():
        write_json(concurrent_dir / f"tx_{tx_id}.json", document)
    manifest = {
        "schema": "warbiter-samples-v1",
        "files": {
            str(path.relative_to(SAMPLES)): sha256_file(path)
            for path in sorted(SAMPLES.rglob("*.json"))
            if path.name != "manifest.json"
        },
    }
    write_json(SAMPLES / "manifest.json", manifest)
    sys.stdout.write(
        json.dumps(
            {"generated": len(manifest["files"]), "manifest": "samples/manifest.json"},
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
