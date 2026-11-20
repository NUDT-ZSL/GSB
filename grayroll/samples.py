from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .util import (
    canonical_json,
    canonical_line,
    sha256_bytes,
    sha256_file,
    tier_digest,
    write_json_file,
)

SERVICE = "checkout.svc"
V2_CLOCK = 1700000000100
V1_CLOCK = 1699999999000


def sample_defaults() -> dict[str, Any]:
    return {
        "clock_tolerance_ms": 100,
        "rollback_step_ms": 500,
        "schema": "compatibility-defaults-v1",
        "source_priority": {"v1": 1, "v2": 2},
        "v1_defaults": {
            "criteria": {
                "min_samples": 2,
                "rules": [
                    {"metric": "error_rate", "op": "<=", "value": 0.02},
                    {"metric": "latency_p95_ms", "op": "<=", "value": 300},
                ],
                "unknown_metric": "CONTINUE",
            },
            "generation": 1,
            "rollback_window": {
                "duration_ms": 6000,
                "start_offset_ms": 0,
            },
        },
    }


def sample_v1() -> dict[str, Any]:
    return {
        "batches": {
            "1": {
                "min_interval_seconds": 0,
                "observation_points": [0.1, 0.5],
                "tiers": [0.1, 0.5, 1.0],
            },
            "2": {
                "min_interval_seconds": 1,
                "observation_points": [0.2, 1.0],
                "tiers": [0.2, 0.6, 1.0],
            },
            "3": {
                "min_interval_seconds": 2,
                "observation_points": [0.25, 0.75],
                "tiers": [0.25, 0.75, 1.0],
            },
        },
        "format": "rhythm-v1",
        "logical_clock_ms": V1_CLOCK,
        "schema_version": 1,
        "service": SERVICE,
    }


def sample_v2_shards() -> dict[str, dict[str, Any]]:
    criteria = {
        "min_samples": 2,
        "rules": [
            {"metric": "error_rate", "op": "<=", "value": 0.01},
            {"metric": "latency_p95_ms", "op": "<=", "value": 250},
        ],
        "unknown_metric": "CONTINUE",
    }
    return {
        "v2/shards/batch-1.json": {
            "absolute_rollout_ms": 1700000002000,
            "batch_id": "1",
            "criteria": criteria,
            "depends_on": [],
            "format": "rhythm-v2",
            "generation": 2,
            "logical_clock_ms": V2_CLOCK,
            "observation_markers": [0.1, 0.5],
            "rollback_step_ms": 500,
            "rollback_window": {
                "end_ms": 1700000016000,
                "start_ms": 1700000002000,
            },
            "schema_version": 2,
            "service": SERVICE,
            "tiers": [0.1, 0.5, 1.0],
        },
        "v2/shards/batch-2.json": {
            "absolute_rollout_ms": 1700000004000,
            "batch_id": "2",
            "criteria": criteria,
            "depends_on": ["1"],
            "format": "rhythm-v2",
            "generation": 2,
            "logical_clock_ms": V2_CLOCK,
            "observation_markers": [0.2, 1.0],
            "rollback_step_ms": 500,
            "rollback_window": {
                "end_ms": 1700000018000,
                "start_ms": 1700000004000,
            },
            "schema_version": 2,
            "service": SERVICE,
            "tiers": [0.2, 0.6, 1.0],
        },
        "v2/shards/batch-4.json": {
            "absolute_rollout_ms": 1700000008000,
            "batch_id": "4",
            "criteria": criteria,
            "depends_on": ["2"],
            "format": "rhythm-v2",
            "generation": 2,
            "logical_clock_ms": V2_CLOCK,
            "observation_markers": [0.15, 0.4],
            "rollback_step_ms": 500,
            "rollback_window": {
                "end_ms": 1700000024000,
                "start_ms": 1700000008000,
            },
            "schema_version": 2,
            "service": SERVICE,
            "tiers": [0.15, 0.4, 1.0],
        },
    }


def sample_events() -> list[dict[str, Any]]:
    def snapshot(
        event_id: str,
        logical_ms: int,
        batch_id: str,
        tier_ratio: float,
        sample_count: int,
        error_rate: float = 0.003,
        latency: float = 120,
    ) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "event_id": event_id,
            "logical_ms": logical_ms,
            "metrics": {
                "error_rate": error_rate,
                "latency_p95_ms": latency,
            },
            "sample_count": sample_count,
            "service": SERVICE,
            "tier_ratio": tier_ratio,
            "type": "snapshot",
        }

    return [
        snapshot("obs-001", 1700000002100, "1", 0.1, 1),
        snapshot("obs-002", 1700000002200, "1", 0.1, 3),
        snapshot("obs-003", 1700000002300, "1", 0.5, 3),
        snapshot("obs-004", 1700000004100, "2", 0.2, 3),
        snapshot("obs-005", 1700000004200, "2", 1.0, 3),
        snapshot("obs-006", 1700000004300, "3", 0.25, 1),
        snapshot("obs-007", 1700000004400, "3", 0.25, 3),
        snapshot("obs-008", 1700000004500, "3", 0.75, 3),
        snapshot("obs-009", 1700000008100, "4", 0.15, 3),
        snapshot("obs-010", 1700000008200, "4", 0.4, 3, error_rate=0.05),
        {
            "batch_id": "1",
            "event_id": "abort-001",
            "logical_ms": 1700000010000,
            "reason": "operator_abort",
            "service": SERVICE,
            "type": "manual_abort",
        },
    ]


def _write_v2_meta(
    path: Path,
    shards: dict[str, dict[str, Any]],
    shard_hashes: dict[str, str],
    digest: str,
    *,
    package_version: int,
    generation_floor: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE shards(
                path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                service TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                absolute_rollout_ms INTEGER NOT NULL,
                tiers_json TEXT NOT NULL,
                observation_markers_json TEXT NOT NULL,
                criteria_json TEXT NOT NULL,
                depends_json TEXT NOT NULL,
                rollback_start_ms INTEGER NOT NULL,
                rollback_end_ms INTEGER NOT NULL,
                UNIQUE(service, batch_id)
            );
            """
        )
        meta = {
            "generation_floor": str(generation_floor),
            "package_version": str(package_version),
            "service": SERVICE,
            "tier_digest": digest,
        }
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            sorted(meta.items()),
        )
        for shard_path in sorted(shards):
            document = shards[shard_path]
            connection.execute(
                """
                INSERT INTO shards(
                    path, sha256, service, batch_id, generation,
                    logical_clock_ms, absolute_rollout_ms, tiers_json,
                    observation_markers_json, criteria_json, depends_json,
                    rollback_start_ms, rollback_end_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shard_path,
                    shard_hashes[shard_path],
                    document["service"],
                    document["batch_id"],
                    document["generation"],
                    document["logical_clock_ms"],
                    document["absolute_rollout_ms"],
                    canonical_json(document["tiers"]),
                    canonical_json(document["observation_markers"]),
                    canonical_json(document["criteria"]),
                    canonical_json(document["depends_on"]),
                    document["rollback_window"]["start_ms"],
                    document["rollback_window"]["end_ms"],
                ),
            )
        connection.commit()
    finally:
        connection.close()


def write_package_snapshot(
    root: Path,
    *,
    package_version: int,
    generation_floor: int,
    v1: dict[str, Any],
    shards: dict[str, dict[str, Any]],
    created_clock_ms: int = V2_CLOCK,
) -> dict[str, Any]:
    (root / "v1").mkdir(parents=True, exist_ok=True)
    (root / "v2" / "shards").mkdir(parents=True, exist_ok=True)
    write_json_file(root / "v1" / "rhythm.json", v1)
    v1_hash = sha256_file(root / "v1" / "rhythm.json")
    shard_hashes: dict[str, str] = {}
    for relative, document in shards.items():
        target = root / relative
        write_json_file(target, document)
        shard_hashes[relative] = sha256_file(target)
    digest = tier_digest(
        (
            document["service"],
            document["batch_id"],
            document["tiers"],
        )
        for document in shards.values()
    )
    _write_v2_meta(
        root / "v2" / "meta.db",
        shards,
        shard_hashes,
        digest,
        package_version=package_version,
        generation_floor=generation_floor,
    )
    manifest = {
        "created_clock_ms": created_clock_ms,
        "generation_floor": generation_floor,
        "package_version": package_version,
        "schema": "rhythm-package-manifest-v1",
        "service": SERVICE,
        "shards": [
            {"path": relative, "sha256": shard_hashes[relative]}
            for relative in sorted(shard_hashes)
        ],
        "tier_digest": digest,
        "v1": {"path": "v1/rhythm.json", "sha256": v1_hash},
        "v2_meta": {
            "meta_path": "v2/meta.db",
            "meta_sha256": sha256_file(root / "v2" / "meta.db"),
        },
    }
    write_json_file(root / "manifest.json", manifest)
    return manifest


def generate_samples(root: Path) -> dict[str, Any]:
    if root.exists():
        shutil.rmtree(root)
    defaults = sample_defaults()
    v1 = sample_v1()
    shards = sample_v2_shards()
    events = sample_events()

    write_json_file(root / "defaults.json", defaults)
    write_package_snapshot(
        root,
        package_version=1,
        generation_floor=2,
        v1=v1,
        shards=shards,
    )
    with (root / "events.jsonl").open("wb") as handle:
        for event in events:
            handle.write(canonical_line(event))
    summary = {
        "defaults": len(defaults),
        "events": len(events),
        "manifest_sha256": sha256_bytes((root / "manifest.json").read_bytes()),
        "root": str(root),
        "shards": len(shards),
        "v1_batches": len(v1["batches"]),
    }
    return summary
