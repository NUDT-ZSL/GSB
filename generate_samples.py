#!/usr/bin/env python3
"""Generate deterministic lock-wait laboratory samples.

The generated directory is part of the deliverable.  Re-running this script
with the same seed must produce byte-identical JSON and SQLite sample data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_SEED = 424242


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def create_sample_sqlite(path: Path, database: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        for table in database["tables"]:
            columns: list[str] = []
            for column in table["columns"]:
                definition = (
                    f"{quote_identifier(column['name'])} {column['type']}"
                )
                if column.get("nullable") is False:
                    definition += " NOT NULL"
                columns.append(definition)
            if table["primary_key"]:
                columns.append(
                    "PRIMARY KEY ("
                    + ", ".join(quote_identifier(name) for name in table["primary_key"])
                    + ")"
                )
            for foreign_key in table.get("foreign_keys", []):
                target = foreign_key["references"]
                columns.append(
                    "FOREIGN KEY ("
                    + ", ".join(
                        quote_identifier(name) for name in foreign_key["columns"]
                    )
                    + ") REFERENCES "
                    + quote_identifier(target["table"])
                    + " ("
                    + ", ".join(
                        quote_identifier(name) for name in target["columns"]
                    )
                    + ")"
                )
            connection.execute(
                f"CREATE TABLE {quote_identifier(table['name'])} "
                f"({', '.join(columns)})"
            )
            for index in table.get("indexes", []):
                unique = "UNIQUE " if index.get("unique") else ""
                connection.execute(
                    f"CREATE {unique}INDEX {quote_identifier(index['name'])} "
                    f"ON {quote_identifier(table['name'])} "
                    f"({', '.join(quote_identifier(c) for c in index['columns'])})"
                )
        for table in database["tables"]:
            if not table["rows"]:
                continue
            column_names = [column["name"] for column in table["columns"]]
            placeholders = ", ".join("?" for _ in column_names)
            connection.executemany(
                f"INSERT INTO {quote_identifier(table['name'])} "
                f"({', '.join(quote_identifier(c) for c in column_names)}) "
                f"VALUES ({placeholders})",
                [
                    tuple(row[column] for column in column_names)
                    for row in table["rows"]
                ],
            )
        connection.commit()
    finally:
        connection.close()


def build_database(rng: random.Random) -> dict[str, Any]:
    branches = [
        {"id": 1, "name": "north", "region": "N"},
        {"id": 2, "name": "south", "region": "S"},
        {"id": 3, "name": "west", "region": "W"},
    ]
    owners = [
        "alice",
        "bob",
        "carol",
        "dave",
        "erin",
        "frank",
        "grace",
        "heidi",
    ]
    accounts = [
        {
            "id": index,
            "branch_id": ((index - 1) % len(branches)) + 1,
            "owner": owner,
            "balance": 1000 + rng.randrange(0, 900),
            "status": "active" if index != 6 else "review",
        }
        for index, owner in enumerate(owners, start=1)
    ]
    ledger_entries = [
        {
            "id": index,
            "account_id": index,
            "amount": rng.randrange(-50, 151),
            "note": f"seed-{index:02d}",
        }
        for index in range(1, 6)
    ]
    return {
        "format_version": 1,
        "database": "lock_wait_lab",
        "ledger": {
            "duration_unit": "nanosecond",
            "duration_scale": 3,
            "rounding": "HALF_UP",
        },
        "tables": [
            {
                "name": "branches",
                "columns": [
                    {"name": "id", "type": "INTEGER", "nullable": False},
                    {"name": "name", "type": "TEXT", "nullable": False},
                    {"name": "region", "type": "TEXT", "nullable": False},
                ],
                "primary_key": ["id"],
                "foreign_keys": [],
                "indexes": [
                    {
                        "name": "ux_branches_name",
                        "unique": True,
                        "columns": ["name"],
                    }
                ],
                "rows": branches,
            },
            {
                "name": "accounts",
                "columns": [
                    {"name": "id", "type": "INTEGER", "nullable": False},
                    {"name": "branch_id", "type": "INTEGER", "nullable": False},
                    {"name": "owner", "type": "TEXT", "nullable": False},
                    {"name": "balance", "type": "INTEGER", "nullable": False},
                    {"name": "status", "type": "TEXT", "nullable": False},
                ],
                "primary_key": ["id"],
                "foreign_keys": [
                    {
                        "columns": ["branch_id"],
                        "references": {"table": "branches", "columns": ["id"]},
                    }
                ],
                "indexes": [
                    {
                        "name": "ux_accounts_owner",
                        "unique": True,
                        "columns": ["owner"],
                    },
                    {
                        "name": "ix_accounts_branch",
                        "unique": False,
                        "columns": ["branch_id"],
                    },
                ],
                "rows": accounts,
            },
            {
                "name": "ledger_entries",
                "columns": [
                    {"name": "id", "type": "INTEGER", "nullable": False},
                    {"name": "account_id", "type": "INTEGER", "nullable": False},
                    {"name": "amount", "type": "INTEGER", "nullable": False},
                    {"name": "note", "type": "TEXT", "nullable": False},
                ],
                "primary_key": ["id"],
                "foreign_keys": [
                    {
                        "columns": ["account_id"],
                        "references": {"table": "accounts", "columns": ["id"]},
                    }
                ],
                "indexes": [
                    {
                        "name": "ix_ledger_account",
                        "unique": False,
                        "columns": ["account_id"],
                    },
                    {
                        "name": "ux_ledger_note",
                        "unique": True,
                        "columns": ["note"],
                    },
                ],
                "rows": ledger_entries,
            },
        ],
    }


def operation(
    op_id: str,
    kind: str,
    table: str | None,
    row_id: int | None,
    lock: str,
    duration_ns: int,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "op_id": op_id,
        "kind": kind,
        "lock": lock,
        "duration_ns": duration_ns,
    }
    if table is not None:
        result["table"] = table
        result["key"] = {"id": row_id}
    if values is not None:
        result["values"] = values
    return result


def build_transactions() -> list[dict[str, Any]]:
    scripts = [
        {
            "tx_id": "T01",
            "start_at_ms": 0,
            "isolation": "SERIALIZABLE",
            "retry_limit": 0,
            "operations": [
                operation(
                    "T01-01",
                    "WRITE",
                    "accounts",
                    1,
                    "EXCLUSIVE",
                    1_200_300,
                    {"owner": "alice-v1", "balance": 1300},
                ),
                operation("T01-02", "READ", "accounts", 2, "SHARED", 600_400),
            ],
        },
        {
            "tx_id": "T02",
            "start_at_ms": 0,
            "isolation": "REPEATABLE_READ",
            "retry_limit": 0,
            "operations": [
                operation("T02-01", "READ", "accounts", 1, "SHARED", 700_100),
                operation("T02-02", "READ", "accounts", 2, "SHARED", 300_200),
            ],
        },
        {
            "tx_id": "T03",
            "start_at_ms": 0,
            "isolation": "SERIALIZABLE",
            "retry_limit": 0,
            "operations": [
                operation(
                    "T03-01",
                    "WRITE",
                    "accounts",
                    3,
                    "EXCLUSIVE",
                    700_000,
                    {"balance": 1403},
                ),
                operation(
                    "T03-02",
                    "WRITE",
                    "accounts",
                    4,
                    "EXCLUSIVE",
                    500_000,
                    {"balance": 1404},
                ),
            ],
        },
        {
            "tx_id": "T04",
            "start_at_ms": 0,
            "isolation": "SERIALIZABLE",
            "retry_limit": 0,
            "operations": [
                operation(
                    "T04-01",
                    "WRITE",
                    "accounts",
                    4,
                    "EXCLUSIVE",
                    700_400,
                    {"balance": 1504},
                ),
                operation(
                    "T04-02",
                    "WRITE",
                    "accounts",
                    3,
                    "EXCLUSIVE",
                    600_000,
                    {"balance": 1503},
                ),
            ],
        },
        {
            "tx_id": "T05",
            "start_at_ms": 0,
            "isolation": "READ_COMMITTED",
            "retry_limit": 0,
            "operations": [
                operation(
                    "T05-01",
                    "WRITE",
                    "accounts",
                    5,
                    "EXCLUSIVE",
                    3_500_400,
                    {"status": "active", "balance": 1605},
                )
            ],
        },
        {
            "tx_id": "T06",
            "start_at_ms": 0,
            "isolation": "REPEATABLE_READ",
            "retry_limit": 0,
            "operations": [
                operation("T06-01", "READ", "accounts", 5, "SHARED", 100_000)
            ],
        },
        {
            "tx_id": "T07",
            "start_at_ms": 0,
            "isolation": "READ_COMMITTED",
            "retry_limit": 1,
            "operations": [
                operation(
                    "T07-01",
                    "WRITE",
                    "accounts",
                    6,
                    "EXCLUSIVE",
                    500_000,
                    {"status": "active"},
                ),
                operation("T07-02", "READ", "branches", 1, "SHARED", 250_000),
            ],
        },
        {
            "tx_id": "T08",
            "start_at_ms": 0,
            "isolation": "SERIALIZABLE",
            "retry_limit": 1,
            "operations": [
                operation("T08-01", "READ", "accounts", 6, "SHARED", 300_000),
                operation(
                    "T08-02", "READ", "ledger_entries", 1, "SHARED", 200_000
                ),
            ],
        },
        {
            "tx_id": "T09",
            "start_at_ms": 0,
            "isolation": "SERIALIZABLE",
            "retry_limit": 0,
            "operations": [operation("T09-01", "ROLLBACK", None, None, "NONE", 0)],
        },
        {
            "tx_id": "T10",
            "start_at_ms": 3,
            "isolation": "REPEATABLE_READ",
            "retry_limit": 0,
            "operations": [
                operation(
                    "T10-01",
                    "WRITE",
                    "accounts",
                    5,
                    "EXCLUSIVE",
                    200_000,
                    {"balance": 1705},
                )
            ],
        },
        {
            "tx_id": "T11",
            "start_at_ms": 4,
            "isolation": "SERIALIZABLE",
            "retry_limit": 0,
            "operations": [
                operation("T11-01", "READ", "accounts", 7, "SHARED", 150_000),
                operation(
                    "T11-02", "READ", "ledger_entries", 2, "SHARED", 100_000
                ),
            ],
        },
        {
            "tx_id": "T12",
            "start_at_ms": 75,
            "isolation": "READ_COMMITTED",
            "retry_limit": 0,
            "operations": [
                operation("T12-01", "READ", "accounts", 8, "SHARED", 50_000)
            ],
        },
    ]
    for index, tx in enumerate(scripts):
        tx["format_version"] = 1
        tx["script_seq"] = index
    return scripts


def policy_summary(payload: dict[str, Any]) -> str:
    return digest(payload)


def build_policies(
    transactions: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], str]:
    tx_ids = {tx["tx_id"] for tx in transactions}
    if not {"T06", "T10"}.issubset(tx_ids):
        raise AssertionError("policy samples require T06 and T10")

    v1_payload = {
        "generation": 1,
        "effective_ms": 0,
        "default_budget_ms": 5,
        "retry_multiplier": 2,
        "flat_mapping": {
            "resource:accounts#id=1": 4,
            "table:accounts": 6,
            "table:ledger_entries": 3,
            "tx:T06": 2,
        },
        "rounding": {"scale": 3, "mode": "HALF_UP"},
        "tolerance_ns": 2_000_000,
    }
    v1 = {"format_version": 1, **v1_payload, "summary": policy_summary(v1_payload)}

    entries = [
        {
            "scope": "default",
            "budget_ms": 8,
            "retry_multiplier": 3,
            "tombstone": False,
        },
        {
            "scope": "table:accounts",
            "budget_ms": 7,
            "retry_multiplier": 3,
            "tombstone": False,
        },
        {
            "scope": "table:ledger_entries",
            "budget_ms": 5,
            "retry_multiplier": 3,
            "tombstone": False,
        },
        {
            "scope": "table:branches",
            "budget_ms": 1,
            "retry_multiplier": 3,
            "tombstone": True,
        },
        {
            "scope": "tx:T10",
            "budget_ms": 1,
            "retry_multiplier": 3,
            "tombstone": False,
        },
        {
            "scope": "tx:T06",
            "budget_ms": 9,
            "retry_multiplier": 3,
            "tombstone": True,
        },
    ]
    entries.sort(key=lambda entry: entry["scope"])
    v2_payload = {
        "generation": 2,
        "effective_ms": 2,
        "entries": entries,
        "rounding": {"scale": 4, "mode": "HALF_UP"},
        "tolerance_ns": 1_500_000,
    }
    v2_summary = policy_summary(v2_payload)
    v2 = {
        "format_version": 2,
        "generation": 2,
        "effective_ms": 2,
        "summary": v2_summary,
        "rounding": {"scale": 4, "mode": "HALF_UP"},
        "tolerance_ns": 1_500_000,
        "shards": [],
        "tombstones": [
            {
                "scope": "table:branches",
                "reason": "legacy branch budget retired",
            },
            {"scope": "tx:T06", "reason": "transaction retired in generation 2"},
        ],
    }
    return v1, v2, entries, v2_summary


def build_policy_metadata(
    path: Path,
    v1: dict[str, Any],
    v2: dict[str, Any],
    entries: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE policy_generations (
                generation INTEGER PRIMARY KEY,
                effective_ms INTEGER NOT NULL,
                summary TEXT NOT NULL,
                tombstone INTEGER NOT NULL CHECK (tombstone IN (0, 1))
            );
            CREATE TABLE policy_entries (
                generation INTEGER NOT NULL,
                scope TEXT NOT NULL,
                budget_ms INTEGER NOT NULL,
                retry_multiplier INTEGER NOT NULL,
                tombstone INTEGER NOT NULL CHECK (tombstone IN (0, 1)),
                shard_no INTEGER NOT NULL,
                PRIMARY KEY (generation, scope)
            );
            CREATE TABLE policy_tombstones (
                generation INTEGER NOT NULL,
                scope TEXT NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY (generation, scope)
            );
            """
        )
        connection.execute(
            "INSERT INTO policy_generations VALUES (?, ?, ?, ?)",
            (1, 0, v1["summary"], 1),
        )
        connection.execute(
            "INSERT INTO policy_generations VALUES (?, ?, ?, ?)",
            (2, v2["effective_ms"], v2["summary"], 0),
        )
        for entry in entries:
            shard_no = 0 if entry["scope"].startswith("table:") or entry["scope"] == "default" else 1
            connection.execute(
                "INSERT INTO policy_entries VALUES (?, ?, ?, ?, ?, ?)",
                (
                    v2["generation"],
                    entry["scope"],
                    entry["budget_ms"],
                    entry["retry_multiplier"],
                    int(entry["tombstone"]),
                    shard_no,
                ),
            )
        for tombstone in v2["tombstones"]:
            connection.execute(
                "INSERT INTO policy_tombstones VALUES (?, ?, ?)",
                (v2["generation"], tombstone["scope"], tombstone["reason"]),
            )
        connection.commit()
    finally:
        connection.close()


def write_policies(out: Path, v1: dict[str, Any], v2: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    policies = out / "policies"
    write_json(policies / "v1.json", v1)

    v2_dir = policies / "v2"
    shard_groups: dict[int, list[dict[str, Any]]] = {0: [], 1: []}
    for entry in entries:
        shard_no = 0 if entry["scope"].startswith("table:") or entry["scope"] == "default" else 1
        shard_groups[shard_no].append(entry)

    shard_manifest: list[dict[str, Any]] = []
    for shard_no in sorted(shard_groups):
        shard = {
            "format_version": 2,
            "generation": v2["generation"],
            "effective_ms": v2["effective_ms"],
            "shard": shard_no,
            "entries": sorted(
                shard_groups[shard_no], key=lambda entry: entry["scope"]
            ),
        }
        filename = f"shard-{shard_no}.json"
        write_json(v2_dir / filename, shard)
        shard_manifest.append(
            {
                "shard": shard_no,
                "file": filename,
                "sha256": digest(shard),
            }
        )

    v2 = dict(v2)
    v2["shards"] = sorted(shard_manifest, key=lambda item: item["shard"])
    write_json(v2_dir / "manifest.json", v2)
    build_policy_metadata(v2_dir / "policy_meta.sqlite", v1, v2, entries)


def generate(out: Path, seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "policies").exists():
        shutil.rmtree(out / "policies")
    database = build_database(rng)
    transactions = build_transactions()
    v1, v2, entries, v2_summary = build_policies(transactions)

    write_json(out / "sample_db.json", database)
    create_sample_sqlite(out / "sample.sqlite", database)
    with (out / "transactions.jsonl").open("w", encoding="utf-8") as handle:
        for transaction in transactions:
            handle.write(
                json.dumps(
                    transaction,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                + "\n"
            )
    write_policies(out, v1, v2, entries)
    clock = {
        "format_version": 1,
        "seed": seed,
        "logical_unit": "nanosecond",
        "ledger_scale": 3,
        "tolerance_ns": 2_000_000,
        "max_logical_ms": 10,
        "parallelism": 6,
        "round_budget_ms": 2,
        "policy_switch_at_ms": 2,
    }
    write_json(out / "clock.json", clock)

    manifest = {
        "format_version": 1,
        "seed": seed,
        "generated_files": [
            "clock.json",
            "policies/v1.json",
            "policies/v2/manifest.json",
            "policies/v2/policy_meta.sqlite",
            "policies/v2/shard-0.json",
            "policies/v2/shard-1.json",
            "sample.sqlite",
            "sample_db.json",
            "transactions.jsonl",
        ],
        "policy_summaries": {
            "1": v1["summary"],
            "2": v2_summary,
        },
        "transaction_count": len(transactions),
    }
    manifest["sha256"] = digest(
        {
            "seed": seed,
            "database": database,
            "transactions": transactions,
            "clock": clock,
            "policy_summaries": manifest["policy_summaries"],
        }
    )
    write_json(out / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("samples"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    manifest = generate(args.out, args.seed)
    print(json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
