#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from walcp.checkpoints import table_summary
from walcp.models import SnapshotRow, WalRow
from walcp.util import atomic_write_json, canonical_json, sha256_obj
from walcp.wal import compute_content_hash

BASE_MS = 1_700_000_000_000
ANCHOR_MS = 1_700_000_100_000
CHECKPOINT_1 = 8
CHECKPOINT_2 = 14


def rows_to_snapshot(state: dict[tuple[str, str], dict[str, Any]]) -> list[SnapshotRow]:
    return [
        SnapshotRow(
            table=table,
            key=key,
            version=value["version"],
            txn_id=value["txn_id"],
            deleted=value["deleted"],
            value=value["value"],
        )
        for (table, key), value in sorted(state.items())
    ]


def apply_row(
    state: dict[tuple[str, str], dict[str, Any]],
    table: str,
    key: str,
    op: str,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    row_key = (table, key)
    current = state.get(row_key)
    if op == "insert":
        version = 1 if current is None else current["version"] + 1
        value = after
        deleted = False
    elif op == "update":
        if current is None or current["deleted"]:
            raise RuntimeError(f"sample update targets a missing row: {row_key}")
        version = current["version"] + 1
        value = after
        deleted = False
    elif op == "delete":
        if current is None or current["deleted"]:
            raise RuntimeError(f"sample delete targets a missing row: {row_key}")
        version = current["version"] + 1
        value = current["value"]
        deleted = True
    else:
        raise RuntimeError(op)
    return {"version": version, "txn_id": 0, "deleted": deleted, "value": value}


def build_wal() -> tuple[list[WalRow], dict[int, dict[tuple[str, str], dict[str, Any]]]]:
    operations = [
        ("insert", "accounts", "acct-alice", None, {"balance": 100}),
        ("insert", "accounts", "acct-bob", None, {"balance": 50}),
        ("insert", "inventory", "sku-1", None, {"qty": 5}),
        ("update", "accounts", "acct-alice", {"balance": 100}, {"balance": 110}),
        ("update", "inventory", "sku-1", {"qty": 5}, {"qty": 4}),
        ("insert", "accounts", "acct-carol", None, {"balance": 70}),
        ("update", "accounts", "acct-alice", {"balance": 110}, {"balance": 105}),
        ("delete", "inventory", "sku-1", {"qty": 4}, None),
        ("insert", "inventory", "sku-2", None, {"qty": 8}),
        ("update", "accounts", "acct-bob", {"balance": 50}, {"balance": 60}),
        ("insert", "accounts", "acct-dan", None, {"balance": 20}),
        ("update", "accounts", "acct-carol", {"balance": 70}, {"balance": 75}),
        ("delete", "accounts", "acct-dan", {"balance": 20}, None),
        ("update", "inventory", "sku-2", {"qty": 8}, {"qty": 6}),
        ("insert", "accounts", "acct-erin", None, {"balance": 40}),
        ("update", "accounts", "acct-alice", {"balance": 105}, {"balance": 95}),
        ("insert", "accounts", "acct-frank", None, {"balance": 30}),
        ("update", "accounts", "acct-frank", {"balance": 30}, {"balance": 35}),
        ("update", "inventory", "sku-2", {"qty": 6}, {"qty": 7}),
        ("delete", "accounts", "acct-erin", {"balance": 40}, None),
        ("insert", "accounts", "acct-grace", None, {"balance": 80}),
        ("update", "accounts", "acct-frank", {"balance": 35}, {"balance": 40}),
    ]
    segments = [
        ("wal-000001.jsonl", range(1, 9)),
        ("wal-000002.jsonl", range(9, 15)),
        ("wal-000003.jsonl", range(15, 23)),
    ]
    state: dict[tuple[str, str], dict[str, Any]] = {}
    snapshots: dict[int, dict[tuple[str, str], dict[str, Any]]] = {}
    rows: list[WalRow] = []
    line_no = 0
    for segment_index, (segment_name, txns) in enumerate(segments, start=1):
        line_no = 0
        for txn_id in txns:
            line_no += 1
            op, table, key, before, after = operations[txn_id - 1]
            current = state.get((table, key))
            if op == "delete":
                next_row = {
                    "version": (current["version"] if current else 0) + 1,
                    "txn_id": txn_id,
                    "deleted": True,
                    "value": before,
                }
            else:
                next_row = {
                    "version": (current["version"] if current else 0) + 1,
                    "txn_id": txn_id,
                    "deleted": False,
                    "value": after,
                }
            state[(table, key)] = next_row
            if txn_id in (CHECKPOINT_1, CHECKPOINT_2):
                snapshots[txn_id] = deepcopy(state)
            record = {
                "txn_id": txn_id,
                "table": table,
                "key": key,
                "op": op,
                "before": before,
                "after": after,
                "checkpoint_gen": 1 if txn_id <= CHECKPOINT_2 else 2,
                "commit_ms": BASE_MS + txn_id * 1000,
            }
            record["content_hash"] = compute_content_hash(record)
            rows.append(
                WalRow(
                    segment=segment_name,
                    segment_no=segment_index,
                    line_no=line_no,
                    txn_id=txn_id,
                    table=table,
                    key=key,
                    op=op,
                    before=before,
                    after=after,
                    checkpoint_gen=record["checkpoint_gen"],
                    content_hash=record["content_hash"],
                    commit_ms=record["commit_ms"],
                )
            )
    return rows, snapshots


def write_wal(samples: Path, rows: list[WalRow]) -> None:
    wal_dir = samples / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[WalRow]] = {}
    for row in rows:
        grouped.setdefault(row.segment, []).append(row)
    for segment, segment_rows in sorted(grouped.items()):
        lines = [canonical_json(row.as_record()) for row in segment_rows]
        path = wal_dir / segment
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def write_v1(
    samples: Path, snapshot: list[SnapshotRow], generation: int, relative_seconds: float
) -> None:
    summary = table_summary(snapshot)
    checkpoint = {
        "format": "walcheckpoint-v1",
        "generation": generation,
        "replay_generation": 5,
        "applied_watermark": CHECKPOINT_1,
        "saved_relative_seconds": relative_seconds,
        "tables": {
            table: {
                "row_count": summary[table]["row_count"],
                "digest": summary[table]["content_digest"],
            }
            for table in sorted(summary)
        },
        "snapshot_rows": [row.as_dict() for row in snapshot],
    }
    path = samples / "checkpoints" / "v1" / "cp-0001-v1.json"
    atomic_write_json(path, checkpoint)


def shard_payload(
    generation: int,
    replay_generation: int,
    table: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "format": "walcheckpoint-v2-shard",
        "generation": generation,
        "replay_generation": replay_generation,
        "table": table,
        "row_count": summary["row_count"],
        "tombstone_count": summary["tombstone_count"],
        "row_versions": summary["row_versions"],
        "tombstones": summary["tombstones"],
        "table_digest": summary["content_digest"],
    }
    payload["shard_hash"] = sha256_obj(payload)
    return payload


def write_v2(
    samples: Path,
    snapshots: dict[int, list[SnapshotRow]],
    saved_at_by_generation: dict[int, int],
) -> None:
    package = samples / "checkpoints" / "v2"
    shard_dir = package / "shards"
    if package.exists():
        shutil.rmtree(package)
    shard_dir.mkdir(parents=True)
    db_path = package / "metadata.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE checkpoint_meta (
                generation INTEGER PRIMARY KEY,
                replay_generation INTEGER NOT NULL,
                replay_generation_floor INTEGER NOT NULL,
                applied_watermark INTEGER NOT NULL,
                saved_at_ms INTEGER NOT NULL,
                manifest_version INTEGER NOT NULL,
                base_generation INTEGER NOT NULL,
                base_manifest_hash TEXT,
                manifest_hash TEXT NOT NULL,
                manifest_json TEXT NOT NULL
            );
            CREATE TABLE shards (
                generation INTEGER NOT NULL,
                shard_name TEXT NOT NULL,
                shard_hash TEXT NOT NULL,
                payload_path TEXT NOT NULL,
                table_name TEXT NOT NULL,
                PRIMARY KEY (generation, shard_name)
            );
            CREATE TABLE snapshot_rows (
                generation INTEGER NOT NULL,
                table_name TEXT NOT NULL,
                row_key TEXT NOT NULL,
                version INTEGER NOT NULL,
                txn_id INTEGER NOT NULL,
                deleted INTEGER NOT NULL,
                value_json TEXT,
                PRIMARY KEY (generation, table_name, row_key)
            );
            """
        )
        previous_manifest_hash: str | None = None
        previous_replay = 0
        for generation in (1, 2):
            replay_generation = 10 * generation
            snapshot = snapshots[
                CHECKPOINT_1 if generation == 1 else CHECKPOINT_2
            ]
            summary = table_summary(snapshot)
            shard_records = []
            for table in sorted(summary):
                file_name = f"gen-{generation:04d}-{table}.json"
                payload = shard_payload(
                    generation, replay_generation, table, summary[table]
                )
                atomic_write_json(shard_dir / file_name, payload)
                relative_path = f"shards/{file_name}"
                shard_records.append(
                    {
                        "name": file_name,
                        "shard_hash": payload["shard_hash"],
                        "payload_path": relative_path,
                        "table": table,
                        "row_count": summary[table]["row_count"],
                        "table_digest": summary[table]["content_digest"],
                    }
                )
                connection.execute(
                    """
                    INSERT INTO shards
                    (generation, shard_name, shard_hash, payload_path, table_name)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        generation,
                        file_name,
                        payload["shard_hash"],
                        relative_path,
                        table,
                    ),
                )
            manifest = {
                "format": "walcheckpoint-v2-manifest",
                "manifest_version": generation,
                "generation": generation,
                "replay_generation": replay_generation,
                "replay_generation_floor": previous_replay,
                "applied_watermark": (
                    CHECKPOINT_1 if generation == 1 else CHECKPOINT_2
                ),
                "saved_at_ms": saved_at_by_generation[generation],
                "base_generation": 0 if generation == 1 else generation - 1,
                "base_manifest_hash": previous_manifest_hash,
                "shard_root_hash": sha256_obj(
                    [record["shard_hash"] for record in shard_records]
                ),
                "shards": [
                    {
                        "name": record["name"],
                        "shard_hash": record["shard_hash"],
                        "table": record["table"],
                    }
                    for record in shard_records
                ],
            }
            manifest_hash = sha256_obj(manifest)
            manifest["manifest_hash"] = manifest_hash
            connection.execute(
                """
                INSERT INTO checkpoint_meta
                (generation, replay_generation, replay_generation_floor,
                 applied_watermark, saved_at_ms, manifest_version,
                 base_generation, base_manifest_hash, manifest_hash,
                 manifest_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation,
                    replay_generation,
                    previous_replay,
                    manifest["applied_watermark"],
                    manifest["saved_at_ms"],
                    manifest["manifest_version"],
                    manifest["base_generation"],
                    manifest["base_manifest_hash"],
                    manifest_hash,
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                ),
            )
            for row in snapshot:
                connection.execute(
                    """
                    INSERT INTO snapshot_rows
                    (generation, table_name, row_key, version, txn_id,
                     deleted, value_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generation,
                        row.table,
                        row.key,
                        row.version,
                        row.txn_id,
                        1 if row.deleted else 0,
                        (
                            json.dumps(
                                row.value, ensure_ascii=False, sort_keys=True
                            )
                            if row.value is not None
                            else None
                        ),
                    ),
                )
            previous_manifest_hash = manifest_hash
            previous_replay = replay_generation
        connection.commit()
    finally:
        connection.close()


def write_manifest(
    samples: Path, rows: list[WalRow], saved_at_by_generation: dict[int, int]
) -> None:
    manifest = {
        "format": "walcp-sample-manifest-v1",
        "base_commit_ms": BASE_MS,
        "v1_anchor_ms": ANCHOR_MS,
        "wal_segments": sorted({row.segment for row in rows}),
        "transaction_range": [rows[0].txn_id, rows[-1].txn_id],
        "checkpoint_generations": [1, 2],
        "checkpoint_saved_at_ms": {
            str(generation): saved
            for generation, saved in sorted(saved_at_by_generation.items())
        },
        "checkpoint_watermarks": {
            "1": CHECKPOINT_1,
            "2": CHECKPOINT_2,
        },
    }
    atomic_write_json(samples / "manifest.json", manifest)
    (samples / "README.md").write_text(
        "# Deterministic samples\n\n"
        "Generated by `python tools/generate_samples.py`.\n\n"
        "- `wal/`: 22 transactions in three WAL segments.\n"
        "- `checkpoints/v1/cp-0001-v1.json`: v1 generation 1 with a "
        "`saved_relative_seconds` value of 20.0.\n"
        "- `checkpoints/v2/`: v2 generations 1 and 2 in shards plus "
        "`metadata.sqlite`.\n\n"
        "Generation 1 deliberately exists in both formats. With the default "
        "v1 anchor of 1700000100000 ms, v2 wins by its later absolute clock.\n",
        encoding="utf-8",
    )


def main() -> None:
    samples = ROOT / "samples"
    rows, snapshots = build_wal()
    write_wal(samples, rows)
    snapshot_1 = rows_to_snapshot(snapshots[CHECKPOINT_1])
    snapshot_2 = rows_to_snapshot(snapshots[CHECKPOINT_2])
    write_v1(samples, snapshot_1, 1, 20.0)
    saved_at_by_generation = {
        1: BASE_MS + 130_000,
        2: BASE_MS + 140_000,
    }
    write_v2(
        samples,
        {CHECKPOINT_1: snapshot_1, CHECKPOINT_2: snapshot_2},
        saved_at_by_generation,
    )
    write_manifest(samples, rows, saved_at_by_generation)
    print(
        f"generated {len(rows)} WAL rows, {len(snapshots)} checkpoints at {samples}"
    )


if __name__ == "__main__":
    main()
