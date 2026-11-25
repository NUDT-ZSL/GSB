from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from .util import canonical_hash, sha256_file, stable_json, write_stable_json


SEED_ID = "logchain-fixed-seed-438"
SEED_VERSION = 438
CLOCK_MS = 1_767_225_605_000
BASE_MS = 1_767_225_600_000
LEDGER_SCALE = 100
TOLERANCE_MS = 2


def with_digest(row: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in row.items() if key != "digest"}
    result = dict(row)
    result["digest"] = canonical_hash(payload)
    return result


def _v1() -> dict[str, Any]:
    entries = [
        {
            "entry_id": "E101",
            "txn_seq": 101,
            "table_name": "accounts",
            "row_key": "A-001",
            "op": "upsert",
            "before": None,
            "after": {"owner": "Alice", "balance": 100, "currency": "CNY"},
            "relative_commit_seconds": 1,
            "record_clock": 10,
        },
        {
            "entry_id": "E102",
            "txn_seq": 102,
            "table_name": "accounts",
            "row_key": "A-002",
            "op": "upsert",
            "before": None,
            "after": {"owner": "Bob", "balance": 240, "currency": "CNY"},
            "relative_commit_seconds": 2,
            "record_clock": 11,
        },
        {
            "entry_id": "E103",
            "txn_seq": 103,
            "table_name": "orders",
            "row_key": "O-001",
            "op": "upsert",
            "before": None,
            "after": {"account": "A-001", "item": "keyboard", "qty": 1},
            "relative_commit_seconds": 3,
            "record_clock": 20,
        },
        {
            "entry_id": "E104",
            "txn_seq": 104,
            "table_name": "orders",
            "row_key": "O-002",
            "op": "upsert",
            "before": None,
            "after": {"account": "A-002", "item": "mouse", "qty": 2},
            "relative_commit_seconds": 3,
            "record_clock": 21,
        },
    ]
    replay_order = {
        str(index): entry["entry_id"] for index, entry in enumerate(entries)
    }
    return {
        "format": "logchain-v1-json",
        "schema_version": 1,
        "snapshot_id": "v1-single-file",
        "seed_id": SEED_ID,
        "relative_time_origin": "manifest.v1_zero_ms",
        "entries": entries,
        "replay_order": replay_order,
    }


def _v2_shards() -> list[dict[str, Any]]:
    shard_zero = {
        "format": "logchain-v2-shard",
        "schema_version": 2,
        "shard_id": "shard-000-generation-1",
        "generation_min": 1,
        "generation_max": 1,
        "entries": [
            {
                "entry_id": "E101",
                "canonical_seq": 101,
                "txn_seq": 101,
                "generation": 1,
                "table_name": "accounts",
                "row_key": "A-001",
                "op": "upsert",
                "before": None,
                "after": {"owner": "Alice", "balance": 101, "currency": "CNY"},
                "commit_ms": BASE_MS + 1000,
                "record_clock": 11,
                "checkpoint_id": "cp-g1",
                "checkpoint_watermark": 199,
            },
            {
                "entry_id": "E103",
                "canonical_seq": 103,
                "txn_seq": 103,
                "generation": 1,
                "table_name": "orders",
                "row_key": "O-001",
                "op": "upsert",
                "before": None,
                "after": {"account": "A-001", "item": "keyboard", "qty": 1},
                "commit_ms": BASE_MS + 3000,
                "record_clock": 20,
                "checkpoint_id": "cp-g1",
                "checkpoint_watermark": 199,
            },
            {
                "entry_id": "E199",
                "canonical_seq": 199,
                "txn_seq": 199,
                "generation": 1,
                "table_name": "accounts",
                "row_key": "A-TMP-INVALID",
                "op": "insert",
                "before": None,
                "after": {"owner": "Stale Draft", "balance": -1},
                "commit_ms": BASE_MS + 2500,
                "record_clock": 19,
                "checkpoint_id": "cp-g1",
                "checkpoint_watermark": 199,
                "tombstone_id": "tomb-g1-199",
            },
        ],
    }
    shard_one = {
        "format": "logchain-v2-shard",
        "schema_version": 2,
        "shard_id": "shard-001-generation-2",
        "generation_min": 2,
        "generation_max": 2,
        "entries": [
            {
                "entry_id": "E201",
                "canonical_seq": 201,
                "txn_seq": 201,
                "generation": 2,
                "table_name": "accounts",
                "row_key": "A-003",
                "op": "insert",
                "before": None,
                "after": {"owner": "Chen", "balance": 77, "currency": "CNY"},
                "commit_ms": BASE_MS + 3001,
                "record_clock": 31,
                "checkpoint_id": "cp-g2",
                "checkpoint_watermark": 299,
            },
            {
                "entry_id": "E202",
                "canonical_seq": 202,
                "txn_seq": 202,
                "generation": 2,
                "table_name": "orders",
                "row_key": "O-003",
                "op": "insert",
                "before": None,
                "after": {"account": "A-003", "item": "monitor", "qty": 1},
                "commit_ms": BASE_MS + 3001,
                "record_clock": 32,
                "checkpoint_id": "cp-g2",
                "checkpoint_watermark": 299,
            },
        ],
    }
    return [shard_zero, shard_one]


def _wal_segments() -> dict[str, list[dict[str, Any]]]:
    g1_rows = [
        {
            "generation": 1,
            "txn_seq": 5001,
            "table_name": "accounts",
            "row_key": "A-001",
            "op": "update",
            "before": {"balance": 101},
            "after": {"owner": "Alice", "balance": 150, "currency": "CNY"},
        },
        {
            "generation": 1,
            "txn_seq": 5002,
            "table_name": "accounts",
            "row_key": "A-101",
            "op": "insert",
            "before": None,
            "after": {"owner": "Dora", "balance": 320, "currency": "CNY"},
        },
        {
            "generation": 1,
            "txn_seq": 5003,
            "table_name": "orders",
            "row_key": "O-001",
            "op": "update",
            "before": {"qty": 1},
            "after": {"account": "A-001", "item": "keyboard", "qty": 2},
        },
        {
            "generation": 1,
            "txn_seq": 5004,
            "table_name": "accounts",
            "row_key": "A-002",
            "op": "update",
            "before": {"balance": 240},
            "after": {"owner": "Bob", "balance": 199, "currency": "CNY"},
        },
    ]
    g2_rows = [
        {
            "generation": 2,
            "txn_seq": 6001,
            "table_name": "accounts",
            "row_key": "A-201",
            "op": "insert",
            "before": None,
            "after": {"owner": "Eve", "balance": 888, "currency": "CNY"},
        },
        {
            "generation": 2,
            "txn_seq": 6002,
            "table_name": "accounts",
            "row_key": "A-101",
            "op": "update",
            "before": {"balance": 320},
            "after": {"owner": "Dora", "balance": 305, "currency": "CNY"},
        },
        {
            "generation": 2,
            "txn_seq": 6003,
            "table_name": "orders",
            "row_key": "O-002",
            "op": "delete",
            "before": {"account": "A-002", "item": "mouse", "qty": 2},
            "after": None,
        },
        {
            "generation": 2,
            "txn_seq": 6004,
            "table_name": "orders",
            "row_key": "O-003",
            "op": "update",
            "before": {"qty": 1},
            "after": {"account": "A-003", "item": "4K monitor", "qty": 1},
        },
    ]
    return {
        "wal-g1-0001": [with_digest(row) for row in g1_rows],
        "wal-g2-0002": [with_digest(row) for row in g2_rows],
    }


def generate(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    samples = root / "samples"
    snapshots = samples / "snapshots"
    v2_dir = samples / "v2"
    shards_dir = v2_dir / "shards"
    wal_dir = samples / "wal"
    crash_dir = samples / "crashes"
    for path in (snapshots, shards_dir, wal_dir / "orphans", crash_dir):
        path.mkdir(parents=True, exist_ok=True)

    v1 = _v1()
    v1_path = snapshots / "v1_snapshot.json"
    write_stable_json(v1_path, v1)

    shard_records = _v2_shards()
    shard_manifests: list[dict[str, Any]] = []
    for shard in shard_records:
        shard_path = shards_dir / f"{shard['shard_id']}.json"
        write_stable_json(shard_path, shard)
        shard_manifests.append(
            {
                "path": str(shard_path.relative_to(v2_dir)),
                "shard_id": shard["shard_id"],
                "generation_min": shard["generation_min"],
                "generation_max": shard["generation_max"],
                "sha256": sha256_file(shard_path),
                "set_digest": canonical_hash(shard["entries"]),
            }
        )

    v2_manifest = {
        "format": "logchain-v2-package",
        "manifest_version": 1,
        "package_version": 1,
        "seed_id": SEED_ID,
        "generation_min": 1,
        "generation_max": 2,
        "shards": shard_manifests,
        "set_digest": canonical_hash(
            [entry for shard in shard_records for entry in shard["entries"]]
        ),
    }
    write_stable_json(v2_dir / "manifest.json", v2_manifest)

    meta_path = v2_dir / "meta.sqlite"
    if meta_path.exists():
        meta_path.unlink()
    connection = sqlite3.connect(meta_path)
    try:
        connection.execute(
            """
            CREATE TABLE checkpoint_refs (
                checkpoint_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                watermark INTEGER NOT NULL,
                commit_ms INTEGER NOT NULL,
                note TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE tombstones (
                tombstone_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                entry_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                row_key TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO checkpoint_refs VALUES (?, ?, ?, ?, ?)",
            [
                ("cp-g1", 1, 199, BASE_MS + 3000, "generation 1 fixed checkpoint"),
                ("cp-g2", 2, 299, BASE_MS + 3001, "adjacent precision-sensitive checkpoint"),
            ],
        )
        connection.execute(
            "INSERT INTO tombstones VALUES (?, ?, ?, ?, ?, ?)",
            (
                "tomb-g1-199",
                1,
                "E199",
                "accounts",
                "A-TMP-INVALID",
                "deterministic invalid draft",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    segments = _wal_segments()
    wal_manifest_segments: list[dict[str, Any]] = []
    for segment_id, rows in segments.items():
        generation = int(segment_id.split("-g")[1].split("-")[0])
        segment_path = wal_dir / f"generation={generation}" / f"{segment_id}.jsonl"
        segment_path.parent.mkdir(parents=True, exist_ok=True)
        segment_path.write_text(
            "".join(stable_json(row, indent=None) + "\n" for row in rows),
            encoding="utf-8",
        )
        wal_manifest_segments.append(
            {
                "segment_id": segment_id,
                "generation": generation,
                "version": 1,
                "path": str(segment_path.relative_to(samples)),
                "sha256": sha256_file(segment_path),
                "record_count": len(rows),
                "record_digest": canonical_hash(rows),
            }
        )

    wal_manifest = {
        "format": "logchain-wal-manifest",
        "seed_id": SEED_ID,
        "segments": sorted(wal_manifest_segments, key=lambda item: item["segment_id"]),
    }
    write_stable_json(wal_dir / "manifest.json", wal_manifest)

    orphan_rows = [
        with_digest(
            {
                "generation": 9,
                "txn_seq": 9901,
                "table_name": "orphan_table",
                "row_key": "ORPHAN-1",
                "op": "insert",
                "before": None,
                "after": {"note": "unreferenced but digest-valid"},
            }
        ),
        with_digest(
            {
                "generation": 9,
                "txn_seq": 9902,
                "table_name": "orphan_table",
                "row_key": "ORPHAN-2",
                "op": "insert",
                "before": None,
                "after": {"note": "safe to archive after verification"},
            }
        ),
    ]
    orphan_path = wal_dir / "orphans" / "orphan-valid.jsonl"
    orphan_path.write_text(
        "".join(stable_json(row, indent=None) + "\n" for row in orphan_rows),
        encoding="utf-8",
    )
    partial_path = wal_dir / "orphans" / "half-written.jsonl.partial"
    partial_path.write_text(
        stable_json(with_digest(orphan_rows[0] | {"row_key": "PARTIAL-1"}), indent=None)
        + '\n{"generation": 9, "txn_seq": 9999, "broken": ',
        encoding="utf-8",
    )

    crash_plan = {
        "format": "logchain-crash-plan",
        "seed_id": SEED_ID,
        "logical_clock_ms": CLOCK_MS,
        "injections": [
            {
                "id": "crash-merge-staging",
                "stage": "merge",
                "point": "after_stage_write",
                "exit_signal": "SIGKILL",
            },
            {
                "id": "crash-replay-g1-mid",
                "stage": "replay",
                "point": "inside_segment",
                "generation": 1,
                "segment_id": "wal-g1-0001",
                "after_records": 2,
                "exit_signal": "SIGKILL",
            },
            {
                "id": "crash-checkpoint-g2-before-commit",
                "stage": "checkpoint",
                "point": "before_final_commit",
                "generation": 2,
                "exit_signal": "SIGKILL",
            },
        ],
    }
    write_stable_json(crash_dir / "crash_plan.json", crash_plan)

    seed_manifest = {
        "format": "logchain-seed-manifest",
        "seed_id": SEED_ID,
        "seed_version": SEED_VERSION,
        "logical_clock_ms": CLOCK_MS,
        "v1_zero_ms": BASE_MS,
        "ledger_scale": LEDGER_SCALE,
        "precision_tolerance_ms": TOLERANCE_MS,
        "paths": {
            "v1": "samples/snapshots/v1_snapshot.json",
            "v2_manifest": "samples/v2/manifest.json",
            "v2_meta": "samples/v2/meta.sqlite",
            "wal_manifest": "samples/wal/manifest.json",
            "crash_plan": "samples/crashes/crash_plan.json",
        },
        "expected_precision_pair": [1, 2],
    }
    write_stable_json(root / "seed_manifest.json", seed_manifest)
    return seed_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic log-chain samples")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    manifest = generate(args.root)
    print(stable_json({"ok": True, "seed_manifest": manifest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
