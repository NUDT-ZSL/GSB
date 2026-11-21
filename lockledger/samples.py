"""Deterministic bundled sample generator."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .database import build_sample_database
from .util import atomic_write_text, canonical_json
from .validation import validate_scenario

SAMPLE_SEED = 20260101


def _database() -> dict[str, Any]:
    rng = random.Random(SAMPLE_SEED)
    accounts = [
        {
            "id": index,
            "owner": f"owner-{index:02d}",
            "balance": 1000 + rng.randrange(0, 5000),
        }
        for index in range(1, 9)
    ]
    jobs = [
        {
            "id": index,
            "label": f"job-{index:02d}",
            "state": "ready" if index % 2 else "queued",
        }
        for index in range(1, 9)
    ]
    inventory = [
        {"id": index, "sku": f"SKU-{index:02d}", "qty": 10 * index}
        for index in range(1, 5)
    ]
    events = [
        {
            "id": index,
            "kind": "range" if index % 2 else "point",
            "amount": index * 7,
        }
        for index in range(1, 21)
    ]
    return {
        "tables": [
            {
                "name": "accounts",
                "primary_key": "id",
                "columns": [
                    {"name": "id", "type": "INTEGER"},
                    {"name": "owner", "type": "TEXT"},
                    {"name": "balance", "type": "INTEGER"},
                ],
            },
            {
                "name": "jobs",
                "primary_key": "id",
                "columns": [
                    {"name": "id", "type": "INTEGER"},
                    {"name": "label", "type": "TEXT"},
                    {"name": "state", "type": "TEXT"},
                ],
            },
            {
                "name": "inventory",
                "primary_key": "id",
                "columns": [
                    {"name": "id", "type": "INTEGER"},
                    {"name": "sku", "type": "TEXT"},
                    {"name": "qty", "type": "INTEGER"},
                ],
            },
            {
                "name": "events",
                "primary_key": "id",
                "columns": [
                    {"name": "id", "type": "INTEGER"},
                    {"name": "kind", "type": "TEXT"},
                    {"name": "amount", "type": "INTEGER"},
                ],
            },
        ],
        "rows": {
            "accounts": accounts,
            "jobs": jobs,
            "inventory": inventory,
            "events": events,
        },
    }


def main_scenario() -> dict[str, Any]:
    config = {
        "clock_step_ms": 1,
        "scale": "2/5",
        "tolerance": 1,
        "error_budget": 2,
        "max_concurrency": 12,
        "deadlock_check_period_ms": 1,
        "max_rounds": 200,
    }
    transactions = [
        {
            "tx_id": "T01_LONG_JOB_HOLDER",
            "tx_no": 1,
            "isolation": "SERIALIZABLE",
            "wait_budget_ms": 20,
            "max_retries": 1,
            "operations": [
                {"type": "write", "table": "jobs", "row": 1,
                 "values": {"state": "running"}},
                {"type": "write", "table": "jobs", "row": 2,
                 "values": {"state": "running"}},
                {"type": "write", "table": "jobs", "row": 3,
                 "values": {"state": "running"}},
                {"type": "write", "table": "jobs", "row": 4,
                 "values": {"state": "running"}},
                {"type": "write", "table": "jobs", "row": 5,
                 "values": {"state": "running"}},
                {"type": "write", "table": "jobs", "row": 6,
                 "values": {"state": "running"}},
            ],
        },
        {
            "tx_id": "T02_TIMEOUT",
            "tx_no": 2,
            "isolation": "READ_COMMITTED",
            "wait_budget_ms": 2,
            "max_retries": 1,
            "operations": [
                {"type": "write", "table": "jobs", "row": 7,
                 "values": {"state": "held_by_timeout_tx"}},
                {"type": "read", "table": "jobs", "row": 1,
                 "columns": ["state"]},
            ],
        },
        {
            "tx_id": "T03_DEADLOCK_LOW_TXNO",
            "tx_no": 3,
            "isolation": "REPEATABLE_READ",
            "wait_budget_ms": 10,
            "max_retries": 2,
            "operations": [
                {"type": "write", "table": "accounts", "row": 2,
                 "values": {"balance": 2222}},
                {"type": "write", "table": "accounts", "row": 3,
                 "values": {"balance": 3333}},
                {"type": "read", "table": "accounts", "row": 1,
                 "columns": ["balance"]},
            ],
        },
        {
            "tx_id": "T04_DEADLOCK_HIGH_TXNO",
            "tx_no": 4,
            "isolation": "SERIALIZABLE",
            "wait_budget_ms": 10,
            "max_retries": 1,
            "operations": [
                {"type": "write", "table": "accounts", "row": 1,
                 "values": {"balance": 1111}},
                {"type": "read", "table": "accounts", "row": 2,
                 "columns": ["balance"]},
            ],
        },
        {
            "tx_id": "T05_INVENTORY_HOLDER",
            "tx_no": 5,
            "isolation": "REPEATABLE_READ",
            "wait_budget_ms": 10,
            "max_retries": 1,
            "operations": [
                {"type": "write", "table": "inventory", "row": 1,
                 "values": {"qty": 99}},
                {"type": "write", "table": "inventory", "row": 2,
                 "values": {"qty": 88}},
                {"type": "write", "table": "inventory", "row": 3,
                 "values": {"qty": 77}},
            ],
        },
        {
            "tx_id": "T06_RANGE_READER",
            "tx_no": 6,
            "isolation": "SERIALIZABLE",
            "wait_budget_ms": 10,
            "max_retries": 1,
            "operations": [
                {"type": "range_scan", "table": "events",
                 "range": {"lo": 10, "hi": 20}, "column": "id"},
                {"type": "range_scan", "table": "events",
                 "range": {"lo": 1, "hi": 5}, "column": "id"},
            ],
        },
        {
            "tx_id": "T07_INVENTORY_WAITER",
            "tx_no": 7,
            "isolation": "READ_COMMITTED",
            "wait_budget_ms": 10,
            "max_retries": 1,
            "operations": [
                {"type": "read", "table": "inventory", "row": 1,
                 "columns": ["qty"]},
            ],
        },
        {
            "tx_id": "T08_RANGE_WRITER",
            "tx_no": 8,
            "isolation": "READ_COMMITTED",
            "wait_budget_ms": 10,
            "max_retries": 1,
            "operations": [
                {"type": "write", "table": "events", "row": 15,
                 "values": {"amount": 1500}},
            ],
        },
        {
            "tx_id": "T09_ACTIVE_ROLLBACK",
            "tx_no": 9,
            "isolation": "REPEATABLE_READ",
            "wait_budget_ms": 10,
            "max_retries": 1,
            "rollback_after_ops": 1,
            "operations": [
                {"type": "read", "table": "accounts", "row": 8,
                 "columns": ["balance"]},
                {"type": "write", "table": "accounts", "row": 8,
                 "values": {"balance": 8888}},
            ],
        },
        {
            "tx_id": "T10_DECLARED_LOCK",
            "tx_no": 10,
            "isolation": "SNAPSHOT",
            "wait_budget_ms": 10,
            "max_retries": 1,
            "operations": [
                {"type": "lock", "table": "events", "mode": "S",
                 "range": {"lo": 6, "hi": 8}},
                {"type": "read", "table": "events", "row": 7,
                 "columns": ["amount"]},
                {"type": "read", "table": "events", "row": 7,
                 "columns": ["amount"]},
            ],
        },
    ]
    return {
        "schema_version": 1,
        "name": "deterministic-main",
        "database": _database(),
        "transactions": transactions,
        "config": config,
    }


def notstarted_scenario() -> dict[str, Any]:
    document = main_scenario()
    document["name"] = "not-started-horizon"
    document["transactions"] = document["transactions"][:2]
    document["config"] = {
        **document["config"],
        "max_concurrency": 1,
        "logical_horizon_ms": 1,
    }
    return document


def write_samples(out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Any] = {"seed": SAMPLE_SEED, "files": []}
    for name, document in (
        ("scenario.json", main_scenario()),
        ("notstarted.json", notstarted_scenario()),
    ):
        scenario = validate_scenario(document, source_path=str(out_dir / name))
        text = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        path = out_dir / name
        atomic_write_text(path, text)
        outputs["files"].append(
            {"path": name, "sha256": scenario.source_sha256}
        )
    main = validate_scenario(main_scenario(), source_path=str(out_dir / "scenario.json"))
    database_summary = build_sample_database(main.database, out_dir / "sample.sqlite")
    outputs["database"] = database_summary
    manifest = {
        "seed": SAMPLE_SEED,
        "scenario_sha256": main.source_sha256,
        "database": database_summary,
    }
    atomic_write_text(out_dir / "manifest.json", canonical_json(manifest) + "\n")
    return outputs

