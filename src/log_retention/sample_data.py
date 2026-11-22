from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

PRIMARY: list[dict[str, int]] = [
    {"txn_id": 1, "commit_ms": 1000, "byte_size": 1000, "segment_id": 1},
    {"txn_id": 2, "commit_ms": 2000, "byte_size": 1200, "segment_id": 1},
    {"txn_id": 3, "commit_ms": 3000, "byte_size": 800, "segment_id": 1},
    {"txn_id": 4, "commit_ms": 5000, "byte_size": 1100, "segment_id": 2},
    {"txn_id": 5, "commit_ms": 6000, "byte_size": 900, "segment_id": 2},
    {"txn_id": 6, "commit_ms": 7000, "byte_size": 1300, "segment_id": 2},
    {"txn_id": 7, "commit_ms": 11000, "byte_size": 1000, "segment_id": 3},
    {"txn_id": 8, "commit_ms": 12000, "byte_size": 1500, "segment_id": 3},
    {"txn_id": 9, "commit_ms": 13000, "byte_size": 1000, "segment_id": 3},
    {"txn_id": 10, "commit_ms": 15000, "byte_size": 1200, "segment_id": 4},
    {"txn_id": 11, "commit_ms": 17000, "byte_size": 800, "segment_id": 4},
    {"txn_id": 12, "commit_ms": 18000, "byte_size": 1000, "segment_id": 4},
    {"txn_id": 13, "commit_ms": 19000, "byte_size": 900, "segment_id": 5},
    {"txn_id": 14, "commit_ms": 19200, "byte_size": 1101, "segment_id": 5},
    {"txn_id": 15, "commit_ms": 19500, "byte_size": 1000, "segment_id": 5},
]


REPLAY: list[dict[str, Any]] = [
    {
        "txn_id": 1,
        "instance_id": "replica-a",
        "replay_ms": 1100,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 1,
        "instance_id": "replica-b",
        "replay_ms": 1200,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 1,
        "instance_id": "replica-c",
        "replay_ms": 1300,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 1,
        "instance_id": "replica-d",
        "replay_ms": 1400,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 2,
        "instance_id": "replica-d",
        "replay_ms": 2100,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 3,
        "instance_id": "replica-a",
        "replay_ms": 3100,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 3,
        "instance_id": "replica-b",
        "replay_ms": 3200,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 3,
        "instance_id": "replica-c",
        "replay_ms": 3300,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 3,
        "instance_id": "replica-d",
        "replay_ms": 3500,
        "result": "invalidated",
        "tombstone": True,
    },
    {
        "txn_id": 4,
        "instance_id": "replica-b",
        "replay_ms": 5100,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 4,
        "instance_id": "replica-c",
        "replay_ms": 5200,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 5,
        "instance_id": "replica-a",
        "replay_ms": 6100,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 5,
        "instance_id": "replica-b",
        "replay_ms": 6200,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 5,
        "instance_id": "replica-c",
        "replay_ms": 6300,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 6,
        "instance_id": "replica-a",
        "replay_ms": 7100,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 6,
        "instance_id": "replica-c",
        "replay_ms": 7200,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 7,
        "instance_id": "replica-c",
        "replay_ms": 11100,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 8,
        "instance_id": "replica-a",
        "replay_ms": 12500,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 8,
        "instance_id": "replica-c",
        "replay_ms": 12200,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 9,
        "instance_id": "replica-c",
        "replay_ms": 13200,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 10,
        "instance_id": "replica-c",
        "replay_ms": 15200,
        "result": "active",
        "tombstone": False,
    },
    {
        "txn_id": 11,
        "instance_id": "replica-c",
        "replay_ms": 17100,
        "result": "active",
        "tombstone": False,
    },
]


V1_POLICIES: list[dict[str, Any]] = [
    {
        "schema_version": 1,
        "policy_id": "v1-a",
        "instance_id": "replica-a",
        "retention_window": "hot-8s",
        "relative_retention_seconds": 8,
        "effective_at_ms": 9000,
        "retention_generation": 1,
        "required_min_catchup_margin": 0,
        "policy_tombstone": False,
        "original_time_representation": {"kind": "relative_seconds", "value": 8},
    },
    {
        "schema_version": 1,
        "policy_id": "v1-b",
        "instance_id": "replica-b",
        "retention_window": "slow-replica-10s",
        "relative_retention_seconds": 10,
        "effective_at_ms": 8500,
        "retention_generation": 1,
        "required_min_catchup_margin": 0,
        "policy_tombstone": False,
        "original_time_representation": {"kind": "relative_seconds", "value": 10},
    },
    {
        "schema_version": 1,
        "policy_id": "v1-c",
        "instance_id": "replica-c",
        "retention_window": "hot-4s",
        "relative_retention_seconds": 4,
        "effective_at_ms": 10000,
        "retention_generation": 1,
        "required_min_catchup_margin": 0,
        "policy_tombstone": False,
        "original_time_representation": {"kind": "relative_seconds", "value": 4},
    },
]


V2_POLICIES: list[dict[str, Any]] = [
    {
        "policy_id": "v2-a-g1",
        "instance_id": "replica-a",
        "segment_id": None,
        "retention_generation": 1,
        "effective_at_ms": 9000,
        "absolute_retention_cutoff_ms": 12000,
        "parent_policy_id": None,
        "required_min_catchup_margin": 0,
        "policy_tombstone": False,
        "retention_window": "cross-version-a",
        "original_time_representation": {"kind": "absolute_ms", "value": 12000},
    },
    {
        "policy_id": "v2-a-g2",
        "instance_id": "replica-a",
        "segment_id": None,
        "retention_generation": 2,
        "effective_at_ms": 16000,
        "absolute_retention_cutoff_ms": 13000,
        "parent_policy_id": "v2-a-g1",
        "required_min_catchup_margin": 200000,
        "policy_tombstone": False,
        "retention_window": "current-a",
        "original_time_representation": {"kind": "absolute_ms", "value": 13000},
    },
    {
        "policy_id": "v2-b-root",
        "instance_id": "replica-b",
        "segment_id": None,
        "retention_generation": 2,
        "effective_at_ms": 12000,
        "absolute_retention_cutoff_ms": 11000,
        "parent_policy_id": "v1-b",
        "required_min_catchup_margin": 100000,
        "policy_tombstone": False,
        "retention_window": "current-b",
        "original_time_representation": {"kind": "absolute_ms", "value": 11000},
    },
    {
        "policy_id": "v2-b-seg3-old",
        "instance_id": "replica-b",
        "segment_id": 3,
        "retention_generation": 3,
        "effective_at_ms": 16000,
        "absolute_retention_cutoff_ms": 12000,
        "parent_policy_id": "v2-b-root",
        "required_min_catchup_margin": 100000,
        "policy_tombstone": False,
        "retention_window": "seg3-fork-old",
        "original_time_representation": {"kind": "absolute_ms", "value": 12000},
    },
    {
        "policy_id": "v2-b-seg3-new",
        "instance_id": "replica-b",
        "segment_id": 3,
        "retention_generation": 4,
        "effective_at_ms": 18000,
        "absolute_retention_cutoff_ms": 14000,
        "parent_policy_id": "v2-b-root",
        "required_min_catchup_margin": 250000,
        "policy_tombstone": False,
        "retention_window": "seg3-fork-new",
        "original_time_representation": {"kind": "absolute_ms", "value": 14000},
    },
    {
        "policy_id": "v2-c-g2",
        "instance_id": "replica-c",
        "segment_id": None,
        "retention_generation": 2,
        "effective_at_ms": 17000,
        "absolute_retention_cutoff_ms": 15000,
        "parent_policy_id": "v1-c",
        "required_min_catchup_margin": 50000,
        "policy_tombstone": False,
        "retention_window": "current-c",
        "original_time_representation": {"kind": "absolute_ms", "value": 15000},
    },
    {
        "policy_id": "v2-d-tombstone",
        "instance_id": "replica-d",
        "segment_id": None,
        "retention_generation": 1,
        "effective_at_ms": 5000,
        "absolute_retention_cutoff_ms": 8000,
        "parent_policy_id": None,
        "required_min_catchup_margin": 0,
        "policy_tombstone": True,
        "retention_window": "retired-d",
        "original_time_representation": {"kind": "absolute_ms", "value": 8000},
    },
]


CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "primary_stream": "primary/transactions.jsonl",
    "replay_stream": "replica/replay.jsonl",
    "v1_policy": "policies/v1/retention.jsonl",
    "v2_meta_db": "policies/v2/policy_meta.db",
    "v2_shards": [
        "policies/v2/shards/part-00.json",
        "policies/v2/shards/part-01.json",
    ],
    "ledger_db": "state/ledger.db",
    "injected_clock_ms": 20000,
    "archive_target_watermark_ms": 14500,
    "fixed_scale_units_per_ms": 1000,
    "clock_tolerance_ms": 250,
    "replay_rates_bytes_per_sec": {
        "replica-a": 4000,
        "replica-b": 2700,
        "replica-c": 9000,
        "replica-d": 1,
    },
    "source_priority": {"v1": 10, "v2": 20},
    "load_version": 1,
}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
        for record in records
    )
    path.write_text(text, encoding="utf-8")


def _write_meta_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=DELETE;
            CREATE TABLE policy_meta (
                policy_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                segment_id INTEGER,
                retention_generation INTEGER NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                absolute_retention_cutoff_ms INTEGER NOT NULL,
                parent_policy_id TEXT,
                required_min_catchup_margin INTEGER NOT NULL,
                policy_tombstone INTEGER NOT NULL CHECK (policy_tombstone IN (0, 1))
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO policy_meta (
                policy_id, instance_id, segment_id, retention_generation,
                effective_at_ms, absolute_retention_cutoff_ms, parent_policy_id,
                required_min_catchup_margin, policy_tombstone
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    p["policy_id"],
                    p["instance_id"],
                    p["segment_id"],
                    p["retention_generation"],
                    p["effective_at_ms"],
                    p["absolute_retention_cutoff_ms"],
                    p["parent_policy_id"],
                    p["required_min_catchup_margin"],
                    1 if p["policy_tombstone"] else 0,
                )
                for p in V2_POLICIES
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _segment_summary() -> list[dict[str, int]]:
    segment_ids = np.asarray([row["segment_id"] for row in PRIMARY], dtype=np.int64)
    commit_ms = np.asarray([row["commit_ms"] for row in PRIMARY], dtype=np.int64)
    byte_size = np.asarray([row["byte_size"] for row in PRIMARY], dtype=np.int64)
    summary: list[dict[str, int]] = []
    for segment_id in sorted(set(segment_ids.tolist())):
        mask = segment_ids == segment_id
        summary.append(
            {
                "segment_id": int(segment_id),
                "txn_count": int(mask.sum()),
                "bytes": int(byte_size[mask].sum()),
                "max_commit_ms": int(commit_ms[mask].max()),
            }
        )
    return summary


def generate(root: Path) -> None:
    root = root.resolve()
    if root.name == "" or root == Path("/"):
        raise ValueError("refusing to replace an unsafe sample path")
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    _write_jsonl(root / CONFIG["primary_stream"], PRIMARY)
    _write_jsonl(root / CONFIG["replay_stream"], REPLAY)
    _write_jsonl(root / CONFIG["v1_policy"], V1_POLICIES)
    _write_json(
        root / CONFIG["v2_shards"][0],
        {
            "schema_version": 2,
            "shard_id": "part-00",
            "policies": V2_POLICIES[:4],
        },
    )
    _write_json(
        root / CONFIG["v2_shards"][1],
        {
            "schema_version": 2,
            "shard_id": "part-01",
            "policies": V2_POLICIES[4:],
        },
    )
    _write_meta_db(root / CONFIG["v2_meta_db"])
    _write_json(root / "config.json", CONFIG)
    _write_json(
        root / "sample_summary.json",
        {
            "description": "Deterministic retention sample with one segment-3 policy fork conflict.",
            "segments": _segment_summary(),
            "injected_clock_ms": CONFIG["injected_clock_ms"],
            "archive_target_watermark_ms": CONFIG["archive_target_watermark_ms"],
            "conflict": {
                "instance_id": "replica-b",
                "segment_id": 3,
                "winning_policy_id": "v2-b-seg3-new",
                "invalidated_policy_id": "v2-b-seg3-old",
                "common_ancestor_policy_id": "v2-b-root",
            },
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic retention sample data."
    )
    parser.add_argument(
        "--root", default="sample", help="sample directory (default: sample)"
    )
    args = parser.parse_args(argv)
    generate(Path(args.root))
    print(json.dumps({"generated": str(Path(args.root).resolve())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
