#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from walrec.canonical import (  # noqa: E402
    ZERO_HASH,
    canonical_json,
    dirty_pages,
    operation_digest,
    stable_json,
    tx_digest,
)
from walrec.checkpoint_io import (  # noqa: E402
    summary_hash_for_v1,
    write_v2_package,
)
from walrec.engine import replay_rows_to_watermark  # noqa: E402

SAMPLES = ROOT / "samples"

OPS: list[tuple[int, str, str, str, Any, Any, int, int]] = [
    (1, "accounts", "alice", "INSERT", None, {"balance": 100}, 1, 4),
    (2, "ledger", "L1", "INSERT", None, {"account": "alice", "amount": 10}, 1, 6),
    (3, "accounts", "bob", "INSERT", None, {"balance": 50}, 1, 3),
    (4, "accounts", "alice", "UPDATE", {"balance": 100}, {"balance": 110}, 1, 5),
    (
        5,
        "ledger",
        "L1",
        "UPDATE",
        {"account": "alice", "amount": 10},
        {"account": "alice", "amount": 20},
        1,
        4,
    ),
    (6, "accounts", "carol", "INSERT", None, {"balance": 30}, 2, 5),
    (7, "accounts", "bob", "DELETE", {"balance": 50}, None, 2, 3),
    (8, "accounts", "alice", "UPDATE", {"balance": 110}, {"balance": 120}, 2, 6),
    (9, "ledger", "L2", "INSERT", None, {"account": "carol", "amount": -5}, 2, 4),
    (10, "accounts", "carol", "UPDATE", {"balance": 30}, {"balance": 40}, 2, 5),
    (11, "accounts", "dave", "INSERT", None, {"balance": 70}, 2, 3),
    (
        12,
        "ledger",
        "L1",
        "UPDATE",
        {"account": "alice", "amount": 20},
        {"account": "alice", "amount": 25},
        2,
        6,
    ),
    (13, "accounts", "dave", "UPDATE", {"balance": 70}, {"balance": 80}, 2, 4),
    (14, "accounts", "alice", "DELETE", {"balance": 120}, None, 2, 5),
    (
        15,
        "ledger",
        "L3",
        "INSERT",
        None,
        {"account": "eve", "amount": 15},
        2,
        3,
    ),
    (16, "accounts", "eve", "INSERT", None, {"balance": 90}, 2, 4),
    (
        17,
        "ledger",
        "L3",
        "UPDATE",
        {"account": "eve", "amount": 15},
        {"account": "eve", "amount": 18},
        2,
        6,
    ),
]

SEGMENT_SIZES = (5, 6, 6)


@dataclass(frozen=True)
class ReplayRecord:
    logical_tx_id: int
    table: str
    row_key: str
    operation: str
    before: Any
    after: Any


def seal_record(record: dict[str, Any], *, reseal_content: bool = True) -> dict[str, Any]:
    if reseal_content:
        record["content_hash"] = operation_digest(
            record["table"],
            record["row_key"],
            record["operation"],
            record["before"],
            record["after"],
        )
    record["tx_hash"] = tx_digest(record)
    return record


def build_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    previous_id = 0
    previous_hash = ZERO_HASH
    segment = 0
    segment_offset = 0
    for index, (txid, table, key, operation, before, after, gen, duration) in enumerate(
        OPS
    ):
        while (
            segment + 1 < len(SEGMENT_SIZES)
            and index >= segment_offset + SEGMENT_SIZES[segment]
        ):
            segment_offset += SEGMENT_SIZES[segment]
            segment += 1
        record: dict[str, Any] = {
            "after": after,
            "before": before,
            "checkpoint_gen": gen,
            "content_hash": operation_digest(table, key, operation, before, after),
            "duration_ms": duration,
            "logical_tx_id": txid,
            "operation": operation,
            "prev_tx_hash": previous_hash,
            "prev_tx_id": previous_id,
            "row_key": key,
            "segment_no": segment + 1,
            "table": table,
        }
        previous_hash = seal_record(record)["tx_hash"]
        previous_id = txid
        records.append(record)
    return records


def write_wal(root: Path, records: list[dict[str, Any]]) -> None:
    wal_dir = root / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    for old in wal_dir.glob("*.jsonl"):
        old.unlink()
    by_segment: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        by_segment.setdefault(int(record["segment_no"]), []).append(record)
    for segment, segment_records in sorted(by_segment.items()):
        path = wal_dir / f"wal-{segment:04d}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for record in segment_records:
                handle.write(canonical_json(record) + "\n")


def write_v1(root: Path) -> None:
    raw: dict[str, Any] = {
        "applied_watermark": 0,
        "chain_hash": ZERO_HASH,
        "checkpoint_gen": 1,
        "conversion": {
            "base_logical_ms": 1000,
            "rounding": "half_up",
            "scale": "1.5",
        },
        "format": "checkpoint-v1",
        "logical_time": {"kind": "relative", "value_ms": 250},
        "manifest_version": 1,
        "tables": {"accounts": 0, "ledger": 0},
    }
    raw["summary_hash"] = summary_hash_for_v1(raw)
    path = root / "checkpoints" / "v1" / "checkpoint-v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stable_json(raw) + "\n", encoding="utf-8")


def write_v2_snapshot(root: Path, records: list[dict[str, Any]]) -> None:
    rows = replay_rows_to_watermark(
        [
            ReplayRecord(
                logical_tx_id=record["logical_tx_id"],
                table=record["table"],
                row_key=record["row_key"],
                operation=record["operation"],
                before=record["before"],
                after=record["after"],
            )
            for record in records
        ],
        5,
    )
    by_table = {
        table: [row.as_dict() for row in table_rows]
        for table, table_rows in rows.items()
    }
    manifest_base = {
        "applied_watermark": 5,
        "chain_hash": records[4]["tx_hash"],
        "checkpoint_gen": 2,
        "commit_ts_ms": 1500,
        "dirty_pages": dirty_pages(by_table),
        "logical_clock_ms": 1500,
        "manifest_version": 2,
        "min_replay_generation": 1,
        "parent_checkpoint_gen": 1,
        "replay_generation": 2,
        "update_kind": "full",
    }
    write_v2_package(root / "checkpoints" / "v2" / "gen-000002", manifest_base, by_table)


def write_policy(root: Path, *, budget: int = 24, max_concurrency: int = 3) -> None:
    policy = {
        "checkpoint_interval_ms": 30,
        "clock_step_ms": 8,
        "clock_tolerance_ms": 4,
        "fail_round_on_error": True,
        "inject_failure_tx_ids": [],
        "max_concurrent_txs": max_concurrency,
        "round_budget_ms": budget,
    }
    (root / "policy.json").write_text(stable_json(policy) + "\n", encoding="utf-8")


def load_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / "wal").glob("wal-*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                records.append(json.loads(line))
    return records


def copy_base(destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(SAMPLES / "normal", destination)


def reseal_from(records: list[dict[str, Any]], txid: int) -> None:
    previous_hash: str | None = None
    for record in records:
        if record["logical_tx_id"] < txid:
            previous_hash = record["tx_hash"]
            continue
        if previous_hash is not None:
            record["prev_tx_hash"] = previous_hash
        seal_record(record)
        previous_hash = record["tx_hash"]


def make_abnormal() -> None:
    base = SAMPLES / "abnormal"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)

    bad_wal = base / "bad_wal"
    copy_base(bad_wal)
    records = load_records(bad_wal)
    records[2]["operation"] = "MERGE"
    reseal_from(records, 3)
    write_wal(bad_wal, records)

    chain = base / "chain_break"
    copy_base(chain)
    records = load_records(chain)
    records[7]["prev_tx_id"] = 777
    reseal_from(records, 8)
    write_wal(chain, records)

    summary = base / "summary_mismatch"
    copy_base(summary)
    records = load_records(summary)
    records[7]["after"] = {"balance": 999}
    write_wal(summary, records)

    missing = base / "checkpoint_missing"
    copy_base(missing)
    records = load_records(missing)
    records[6]["checkpoint_gen"] = 99
    reseal_from(records, 7)
    write_wal(missing, records)

    rollback = base / "version_rollback"
    copy_base(rollback)
    rows_at_five = replay_rows_to_watermark(
        [
            ReplayRecord(
                logical_tx_id=record["logical_tx_id"],
                table=record["table"],
                row_key=record["row_key"],
                operation=record["operation"],
                before=record["before"],
                after=record["after"],
            )
            for record in load_records(rollback)
        ],
        5,
    )
    by_table = {
        table: [row.as_dict() for row in table_rows]
        for table, table_rows in rows_at_five.items()
    }
    manifest_base = {
        "applied_watermark": 5,
        "chain_hash": load_records(rollback)[4]["tx_hash"],
        "checkpoint_gen": 1,
        "commit_ts_ms": 900,
        "dirty_pages": dirty_pages(by_table),
        "logical_clock_ms": 900,
        "manifest_version": 1,
        "min_replay_generation": 0,
        "parent_checkpoint_gen": 0,
        "replay_generation": 1,
        "update_kind": "full",
    }
    write_v2_package(
        rollback / "checkpoints" / "incoming" / "rollback",
        manifest_base,
        by_table,
    )

    replay = base / "replay_generation"
    copy_base(replay)
    manifest_base["checkpoint_gen"] = 3
    manifest_base["manifest_version"] = 3
    manifest_base["parent_checkpoint_gen"] = 2
    manifest_base["commit_ts_ms"] = 1600
    manifest_base["logical_clock_ms"] = 1600
    manifest_base["replay_generation"] = 1
    write_v2_package(replay / "checkpoints" / "v2" / "gen-000003", manifest_base, by_table)


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    normal = SAMPLES / "normal"
    if normal.exists():
        shutil.rmtree(normal)
    normal.mkdir()
    records = build_records()
    write_wal(normal, records)
    write_v1(normal)
    write_v2_snapshot(normal, records)
    write_policy(normal)

    budget = SAMPLES / "budget"
    if budget.exists():
        shutil.rmtree(budget)
    shutil.copytree(normal, budget)
    write_policy(budget, budget=8, max_concurrency=2)

    make_abnormal()
    print("samples generated")


if __name__ == "__main__":
    main()
