"""Deterministic local sample generator.

The sample is intentionally explicit rather than random so generated files and
scores are reproducible bit-for-bit.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .util import sha256_file, write_json

SEED_NUMBER = 20261126


def _observation(instance_id: str, level: int, seq: int, clock_ms: int, good: bool) -> dict[str, Any]:
    if good:
        return {
            "instance_id": instance_id,
            "level": level,
            "seq": seq,
            "clock_ms": clock_ms,
            "successes": 100,
            "failures": 0,
            "avg_latency_ms": 8 + level,
        }
    return {
        "instance_id": instance_id,
        "level": level,
        "seq": seq,
        "clock_ms": clock_ms,
        "successes": 8,
        "failures": 92,
        "avg_latency_ms": 240 + level * 10,
    }


def _window(instance_id: str, window_no: int, good: bool, clock_ms: int | None = None) -> list[dict[str, Any]]:
    clock = clock_ms if clock_ms is not None else window_no * 1000
    return [_observation(instance_id, level, window_no, clock, good) for level in (1, 2, 3)]


def build_registry() -> dict[str, Any]:
    instances = [
        {
            "instance_id": "inst-steady",
            "endpoint": "http://127.0.0.1:18081",
            "state_generation": 11,
            "levels": [1, 2, 3],
            "window_ms": 1000,
            "timeout_ms": 2500,
        },
        {
            "instance_id": "inst-flap",
            "endpoint": "http://127.0.0.1:18082",
            "state_generation": 12,
            "levels": [1, 2, 3],
            "window_ms": 1000,
            "timeout_ms": 2500,
        },
        {
            "instance_id": "inst-outage",
            "endpoint": "http://127.0.0.1:18083",
            "state_generation": 13,
            "levels": [1, 2, 3],
            "window_ms": 1000,
            "timeout_ms": 2500,
        },
        {
            "instance_id": "inst-timeout",
            "endpoint": "http://127.0.0.1:18084",
            "state_generation": 14,
            "levels": [1, 2, 3],
            "window_ms": 1000,
            "timeout_ms": 2500,
        },
    ]
    return {
        "schema": "hsm.registry/v1",
        "seed": SEED_NUMBER,
        "services": [
            {
                "service_id": "svc-health",
                "service_name": "local-health-probe",
                "instances": instances,
            }
        ],
    }


def build_observations() -> dict[str, Any]:
    observations: list[dict[str, Any]] = []

    for window_no in range(1, 6):
        observations.extend(_window("inst-steady", window_no, True))

    flap_pattern = {1: True, 2: False, 3: False, 4: True, 5: True, 6: True}
    for window_no, good in flap_pattern.items():
        observations.extend(_window("inst-flap", window_no, good))

    for window_no in range(1, 11):
        good = window_no == 1 or window_no >= 7
        observations.extend(_window("inst-outage", window_no, good))

    observations.extend(_window("inst-timeout", 1, True, clock_ms=1000))
    observations.extend(_window("inst-timeout", 2, True, clock_ms=5000))
    observations.extend(_window("inst-timeout", 3, True, clock_ms=6000))
    observations.extend(_window("inst-timeout", 4, True, clock_ms=7000))

    for index, observation in enumerate(observations, start=1):
        observation["stream_seq"] = index
    return {"schema": "hsm.observations/v1", "seed": SEED_NUMBER, "observations": observations}


def build_v1_thresholds() -> dict[str, Any]:
    instance_ids = ["inst-steady", "inst-flap", "inst-outage", "inst-timeout"]
    return {
        "schema": "hsm.thresholds.v1",
        "snapshot_id": "v1-fixed-001",
        "format_priority": 1,
        "threshold_generation": 1,
        "logical_clock_ms": 1000,
        "time_base_ms": 0,
        "effective_after_seconds": 1,
        "time_conversion_policy": {"scale": 1000, "rounding": "floor"},
        "failure_thresholds": {instance_id: [2, 3, 4] for instance_id in instance_ids},
        "weights": {instance_id: [5, 3, 2] for instance_id in instance_ids},
        "min_score_bp": {instance_id: [7000, 6500, 6000] for instance_id in instance_ids},
        "latency_budget_ms": {instance_id: [100, 120, 150] for instance_id in instance_ids},
        "state_policy": {
            "healthy_score_bp": 8500,
            "degraded_after_failures": 1,
            "lost_after_failures": 2,
            "isolated_after_failures": 4,
            "degraded_recovery_windows": 2,
            "degraded_recovery_duration_ms": 1000,
            "lost_recovery_windows": 3,
            "lost_recovery_duration_ms": 2000,
            "isolated_recovery_windows": 4,
            "isolated_recovery_duration_ms": 3000,
        },
    }


def _v2_entry(
    instance_id: str,
    level: int,
    *,
    generation: int,
    clock_ms: int,
    weight: int,
    failure_limit: int,
    min_score_bp: int,
    latency_budget_ms: int,
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "level": level,
        "generation": generation,
        "logical_clock_ms": clock_ms,
        "effective_at_ms": clock_ms,
        "weight": weight,
        "failure_limit": failure_limit,
        "min_score_bp": min_score_bp,
        "latency_budget_ms": latency_budget_ms,
        "failure_counts": [failure_limit, failure_limit + 2, failure_limit + 4],
    }


def build_v2_shards() -> list[dict[str, Any]]:
    shard_zero = {
        "schema": "hsm.threshold_shard/v2",
        "shard_id": "shard-00",
        "thresholds": [
            _v2_entry("inst-steady", 1, generation=2, clock_ms=3000, weight=6, failure_limit=2, min_score_bp=7000, latency_budget_ms=80),
            _v2_entry("inst-steady", 2, generation=2, clock_ms=3000, weight=3, failure_limit=3, min_score_bp=6500, latency_budget_ms=100),
            _v2_entry("inst-steady", 3, generation=2, clock_ms=3000, weight=1, failure_limit=4, min_score_bp=6000, latency_budget_ms=120),
            _v2_entry("inst-flap", 1, generation=1, clock_ms=1000, weight=5, failure_limit=2, min_score_bp=7000, latency_budget_ms=100),
            _v2_entry("inst-flap", 2, generation=1, clock_ms=1000, weight=3, failure_limit=3, min_score_bp=6500, latency_budget_ms=120),
            _v2_entry("inst-flap", 3, generation=1, clock_ms=1000, weight=2, failure_limit=4, min_score_bp=6000, latency_budget_ms=150),
        ],
    }
    shard_one = {
        "schema": "hsm.threshold_shard/v2",
        "shard_id": "shard-01",
        "thresholds": [
            _v2_entry("inst-outage", 1, generation=2, clock_ms=2000, weight=5, failure_limit=2, min_score_bp=7000, latency_budget_ms=100),
            _v2_entry("inst-outage", 2, generation=2, clock_ms=2000, weight=3, failure_limit=3, min_score_bp=6500, latency_budget_ms=120),
            _v2_entry("inst-outage", 3, generation=2, clock_ms=2000, weight=2, failure_limit=4, min_score_bp=6000, latency_budget_ms=150),
        ],
    }
    return [shard_zero, shard_one]


V2_META_SCHEMA = """
CREATE TABLE v2_shards (
    shard_id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    shard_order INTEGER NOT NULL,
    threshold_generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL
);
CREATE TABLE v2_generation_ledger (
    instance_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    shard_id TEXT NOT NULL,
    PRIMARY KEY (instance_id, level, generation)
);
CREATE TABLE v2_tombstones (
    instance_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    deleted_at_ms INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (instance_id, level, generation)
);
"""


def build_patches() -> dict[str, dict[str, Any]]:
    adjust_flap = {
        "schema": "hsm.patch/v1",
        "patch_id": "patch-adjust-flap-level1",
        "op": "upsert_threshold",
        "instance_id": "inst-flap",
        "level": 1,
        "generation": 10,
        "logical_clock_ms": 11000,
        "effective_at_ms": 11000,
        "weight": 9,
        "failure_limit": 999,
        "min_score_bp": 100,
        "latency_budget_ms": 5000,
        "failure_counts": [999, 1001, 1003],
        "reason": "widen level-1 threshold for closure test",
    }
    rename_steady = {
        "schema": "hsm.patch/v1",
        "patch_id": "patch-rename-steady",
        "op": "rename_instance",
        "old_instance_id": "inst-steady",
        "new_instance_id": "inst-steady-renamed",
        "logical_clock_ms": 12000,
        "reason": "demonstrate instance-id dependency closure",
    }
    tombstone_timeout = {
        "schema": "hsm.patch/v1",
        "patch_id": "patch-tombstone-timeout-level3",
        "op": "tombstone",
        "instance_id": "inst-timeout",
        "level": 3,
        "generation": 20,
        "deleted_at_ms": 13000,
        "logical_clock_ms": 13000,
        "reason": "retire level 3 probe",
    }
    return {
        "01_adjust_flap.json": adjust_flap,
        "02_rename_steady.json": rename_steady,
        "03_tombstone_timeout_level3.json": tombstone_timeout,
    }


def generate_sample(root: str | Path = "samples") -> dict[str, str]:
    root = Path(root)
    v2_dir = root / "thresholds_v2"
    patch_dir = root / "patches"
    v2_dir.mkdir(parents=True, exist_ok=True)
    patch_dir.mkdir(parents=True, exist_ok=True)

    registry_path = root / "registry.json"
    observations_path = root / "observations.json"
    v1_path = root / "thresholds_v1.json"
    write_json(registry_path, build_registry())
    write_json(observations_path, build_observations())
    write_json(v1_path, build_v1_thresholds())

    shards = build_v2_shards()
    shard_paths: list[Path] = []
    for shard in shards:
        path = v2_dir / f"{shard['shard_id']}.json"
        write_json(path, shard)
        shard_paths.append(path)

    meta_path = v2_dir / "meta.sqlite"
    if meta_path.exists():
        meta_path.unlink()
    conn = sqlite3.connect(meta_path)
    try:
        conn.executescript(V2_META_SCHEMA)
        for order, (shard, path) in enumerate(zip(shards, shard_paths)):
            generation = 2 if shard["shard_id"] == "shard-00" else 2
            clock = 3000 if order == 0 else 2000
            conn.execute(
                "INSERT INTO v2_shards VALUES (?, ?, ?, ?, ?, ?)",
                (shard["shard_id"], path.name, sha256_file(path), order, generation, clock),
            )
            for entry in shard["thresholds"]:
                conn.execute(
                    "INSERT INTO v2_generation_ledger VALUES (?, ?, ?, ?, ?)",
                    (
                        entry["instance_id"],
                        entry["level"],
                        entry["generation"],
                        entry["logical_clock_ms"],
                        shard["shard_id"],
                    ),
                )
        conn.execute(
            "INSERT INTO v2_generation_ledger VALUES (?, ?, ?, ?, ?)",
            ("inst-outage", 3, 3, 4000, "tombstone"),
        )
        conn.execute(
            "INSERT INTO v2_tombstones VALUES (?, ?, ?, ?, ?)",
            ("inst-outage", 3, 3, 4000, "retire noisy level 3"),
        )
        conn.commit()
    finally:
        conn.close()

    patch_paths: dict[str, str] = {}
    for filename, patch in build_patches().items():
        path = patch_dir / filename
        write_json(path, patch)
        patch_paths[filename] = str(path)

    return {
        "root": str(root),
        "registry": str(registry_path),
        "observations": str(observations_path),
        "thresholds_v1": str(v1_path),
        "thresholds_v2_dir": str(v2_dir),
        "thresholds_v2_meta": str(meta_path),
        "patches": str(patch_dir),
    }


def main() -> int:
    paths = generate_sample()
    for key, value in paths.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
