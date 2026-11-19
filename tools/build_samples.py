#!/usr/bin/env python3
"""Build the deterministic local v1/v2/package/log fixtures."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replication_diag.util import sha256_file, stable_json_text  # noqa: E402
from replication_diag.v2_parser import metadata_digest  # noqa: E402

SAMPLES = ROOT / "samples"
CONFIG = ROOT / "config"

V1_DELAYS = [5, 9, 5, 11, 8, 15, 15, 3]
V1_BYTES = [100, 120, 90, 140, 110, 160, 130, 70]
V1_COMMITTED = [True] * 7 + [False]

V2_DELAYS = [7, 14, 14, 2, 12, 2, 12, 4]
V2_BYTES = [110, 160, 130, 70, 120, 80, 110, 90]
V2_GENERATIONS = [0, 1, 1, 2, 2, 3, 3, 4]

ALL_BYTES = [100, 120, 90, 140, 110, 160, 130, 70, 120, 80, 110, 90]
FULL_DELAYS = [4, 10, 6, 12, 8, 13, 15, 1, 13, 1, 13, 5]
FULL_GENERATIONS = [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stable_json_text(value, pretty=True), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def canonical(number: int) -> str:
    return f"{number:020d}"


def build_v2_dataset(
    directory: Path,
    *,
    package_version: str,
    package_type: str,
    first_tx: int,
    records_spec: list[dict[str, Any]],
    shard_size: int = 4,
) -> None:
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    metadata_path = directory / "metadata.sqlite"
    connection = sqlite3.connect(metadata_path)
    try:
        connection.execute(
            """
            CREATE TABLE tx_metadata (
                tx_id TEXT PRIMARY KEY,
                tx_num INTEGER NOT NULL UNIQUE,
                commit_time_ms INTEGER NOT NULL,
                replay_time_ms INTEGER NOT NULL,
                source_node TEXT NOT NULL,
                replica_node TEXT NOT NULL,
                replay_generation INTEGER NOT NULL,
                tombstone_applied INTEGER NOT NULL,
                logical_clock INTEGER NOT NULL,
                payload_bytes INTEGER NOT NULL,
                committed INTEGER NOT NULL
            )
            """
        )
        rows: list[dict[str, Any]] = []
        for spec in records_spec:
            tx_num = spec["tx_num"]
            row = {
                "tx_id": canonical(tx_num),
                "tx_num": tx_num,
                "commit_time_ms": spec["logical_clock"],
                "replay_time_ms": spec["logical_clock"] + spec["delay_ms"],
                "source_node": spec.get("source_node", "primary-a"),
                "replica_node": spec.get("replica_node", "replica-a"),
                "replay_generation": spec["replay_generation"],
                "tombstone_applied": 1 if spec.get("tombstone_applied", False) else 0,
                "logical_clock": spec["logical_clock"],
                "payload_bytes": spec["payload_bytes"],
                "committed": 1 if spec.get("committed", True) else 0,
            }
            rows.append(row)
            connection.execute(
                """
                INSERT INTO tx_metadata(
                    tx_id, tx_num, commit_time_ms, replay_time_ms, source_node,
                    replica_node, replay_generation, tombstone_applied,
                    logical_clock, payload_bytes, committed
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                tuple(row[key] for key in (
                    "tx_id",
                    "tx_num",
                    "commit_time_ms",
                    "replay_time_ms",
                    "source_node",
                    "replica_node",
                    "replay_generation",
                    "tombstone_applied",
                    "logical_clock",
                    "payload_bytes",
                    "committed",
                )),
            )
        connection.commit()
    finally:
        connection.close()

    shard_entries: list[dict[str, Any]] = []
    for shard_index, start in enumerate(range(0, len(rows), shard_size), 1):
        shard_rows = rows[start : start + shard_size]
        shard_path = directory / "shards" / f"shard-{shard_index:04d}.json"
        payload = {
            "format": "v2-shard",
            "shard_id": f"{shard_index:04d}",
            "records": [
                {
                    "tx_id": row["tx_id"],
                    "payload_bytes": row["payload_bytes"],
                    "logical_clock": row["logical_clock"],
                    "metadata_digest": metadata_digest(row),
                }
                for row in shard_rows
            ],
        }
        write_json(shard_path, payload)
        shard_entries.append(
            {
                "path": f"shards/{shard_path.name}",
                "sha256": sha256_file(shard_path),
            }
        )

    assert records_spec
    tx_min = canonical(records_spec[0]["tx_num"])
    tx_max = canonical(records_spec[-1]["tx_num"])
    assert int(tx_min) == first_tx
    manifest = {
        "format_version": 2,
        "package_version": package_version,
        "package_type": package_type,
        "logical_clock_floor": 1000,
        "tx_min": tx_min,
        "tx_max": tx_max,
        "shards": shard_entries,
        "metadata_db": {
            "path": "metadata.sqlite",
            "sha256": sha256_file(metadata_path),
        },
    }
    write_json(directory / "manifest.json", manifest)


def build() -> None:
    (SAMPLES / "v1").mkdir(parents=True, exist_ok=True)
    v1_rows = [
        {
            "tx_id": str(tx_num),
            "relative_delay_ms": V1_DELAYS[tx_num - 1],
            "payload_bytes": V1_BYTES[tx_num - 1],
            "committed": V1_COMMITTED[tx_num - 1],
            "logical_clock": 1000 + tx_num - 1,
            "source_node": "primary-v1",
            "replica_node": "replica-v1",
        }
        for tx_num in range(1, 9)
    ]
    write_jsonl(SAMPLES / "v1" / "positions.jsonl", v1_rows)

    v2_source = [
        {
            "tx_num": tx_num,
            "delay_ms": V2_DELAYS[tx_num - 5],
            "payload_bytes": V2_BYTES[tx_num - 5],
            "logical_clock": 1000 + tx_num - 1,
            "replay_generation": V2_GENERATIONS[tx_num - 5],
            "tombstone_applied": tx_num == 10,
            "committed": True,
        }
        for tx_num in range(5, 13)
    ]
    build_v2_dataset(
        SAMPLES / "v2",
        package_version="2.0.0",
        package_type="source",
        first_tx=5,
        records_spec=v2_source,
    )

    log_rows = []
    for tx_num in range(1, 13):
        if tx_num <= 8:
            replay_time = 1000 + tx_num - 1 + (V2_DELAYS[tx_num - 5] if tx_num >= 5 else V1_DELAYS[tx_num - 1])
        else:
            replay_time = 1000 + tx_num - 1 + V2_DELAYS[tx_num - 5]
        log_rows.append(
            {
                "tx_id": canonical(tx_num),
                "payload_bytes": ALL_BYTES[tx_num - 1],
                "applied": tx_num <= 10,
                "replay_time_ms": replay_time,
            }
        )
    write_jsonl(SAMPLES / "logs" / "segments" / "0001.jsonl", log_rows)

    full_spec = [
        {
            "tx_num": tx_num,
            "delay_ms": FULL_DELAYS[tx_num - 1],
            "payload_bytes": ALL_BYTES[tx_num - 1],
            "logical_clock": 1000 + tx_num - 1,
            "replay_generation": FULL_GENERATIONS[tx_num - 1],
            "tombstone_applied": tx_num == 10,
            "committed": True,
            "source_node": "primary-full",
            "replica_node": "replica-full",
        }
        for tx_num in range(1, 13)
    ]
    build_v2_dataset(
        SAMPLES / "packages" / "full-2.2.0",
        package_version="2.2.0",
        package_type="full",
        first_tx=1,
        records_spec=full_spec,
    )
    build_v2_dataset(
        SAMPLES / "packages" / "rollback-1.9.0",
        package_version="1.9.0",
        package_type="full",
        first_tx=1,
        records_spec=full_spec[:8],
    )
    build_v2_dataset(
        SAMPLES / "packages" / "version-rollback-1.9.0",
        package_version="1.9.0",
        package_type="full",
        first_tx=1,
        records_spec=full_spec,
    )
    incremental_spec = [
        {
            "tx_num": 13,
            "delay_ms": 6,
            "payload_bytes": 95,
            "logical_clock": 1012,
            "replay_generation": 5,
            "committed": True,
            "source_node": "primary-a",
            "replica_node": "replica-a",
        },
        {
            "tx_num": 14,
            "delay_ms": 8,
            "payload_bytes": 115,
            "logical_clock": 1013,
            "replay_generation": 5,
            "committed": True,
            "source_node": "primary-a",
            "replica_node": "replica-a",
        },
    ]
    build_v2_dataset(
        SAMPLES / "packages" / "incremental-2.1.0",
        package_version="2.1.0",
        package_type="incremental",
        first_tx=13,
        records_spec=incremental_spec,
        shard_size=2,
    )

    config_payload = {
        "root": "..",
        "v1_path": "samples/v1/positions.jsonl",
        "v2_manifest": "samples/v2/manifest.json",
        "logs_path": "samples/logs",
        "v1_base_clock_ms": 1000,
        "v1_clock_step_ms": 1,
        "v1_scale": {"numerator": 1, "denominator": 1},
        "v1_rounding": "half_up",
        "window_size": 3,
        "window_stride": 1,
        "tolerance_ms": 1,
        "observation_error_ms": 1,
        "v1_observation_error_ms": 0,
        "require_contiguous": True,
        "strict_clock_order": True,
    }
    write_json(CONFIG / "sample.json", config_payload)


if __name__ == "__main__":
    build()
