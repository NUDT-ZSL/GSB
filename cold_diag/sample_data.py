from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import Any

from .utils import stable_key_digest, write_json

SEED = 20261127


def function_declarations() -> list[dict[str, Any]]:
    return [
        {
            "name": "alpha",
            "function_name": "alpha",
            "cold_start_ms": 100,
            "concurrency_limit": 2,
            "warmup_window_ms": 600_000,
            "container_spec": "python3.10-serverless",
            "priority": 10,
        },
        {
            "name": "beta",
            "cold_start_ms": 80,
            "concurrency_limit": 1,
            "warmup_window_ms": 100,
            "container_spec": "python3.10-serverless",
            "priority": 20,
        },
        {
            "name": "gamma",
            "cold_start_ms": 40,
            "concurrency_limit": 2,
            "warmup_window_ms": 600_000,
            "container_spec": "node20-serverless",
            "priority": 30,
        },
        {
            "name": "delta",
            "cold_start_ms": 120,
            "concurrency_limit": 4,
            "warmup_window_ms": 600_000,
            "container_spec": "python3.10-data",
            "priority": 40,
        },
        {
            "name": "flaky",
            "cold_start_ms": 60,
            "concurrency_limit": 1,
            "warmup_window_ms": 600_000,
            "container_spec": "debug-runtime",
            "priority": 35,
            "warmup_failure": True,
            "warmup_failure_delay_ms": 25,
        },
    ]


def request_stream(seed: int = SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    rows = [
        {
            "seq": 1,
            "function_name": "alpha",
            "logical_clock_ms": 0,
            "priority": 50,
            "expected_latency_ms": 120,
            "call_status": "submitted",
            "duration_ms": 120,
        },
        {
            "seq": 2,
            "function_name": "alpha",
            "logical_clock_ms": 4,
            "priority": 50,
            "expected_latency_ms": 130,
            "call_status": "submitted",
            "duration_ms": 130,
        },
        {
            "seq": 3,
            "function_name": "beta",
            "logical_clock_ms": 20,
            "priority": 40,
            "expected_latency_ms": 20,
            "call_status": "submitted",
            "duration_ms": 20,
        },
        {
            "seq": 4,
            "function_name": "gamma",
            "logical_clock_ms": 90,
            "priority": 60,
            "expected_latency_ms": 15,
            "call_status": "submitted",
            "duration_ms": 15,
        },
        {
            "seq": 5,
            "function_name": "alpha",
            "logical_clock_ms": 110,
            "priority": 50,
            "expected_latency_ms": 50,
            "call_status": "submitted",
            "duration_ms": 50,
        },
        {
            "seq": 6,
            "function_name": "beta",
            "logical_clock_ms": 120,
            "priority": 40,
            "expected_latency_ms": 30,
            "call_status": "submitted",
            "duration_ms": 30,
        },
        {
            "seq": 7,
            "function_name": "gamma",
            "logical_clock_ms": 125,
            "priority": 60,
            "expected_latency_ms": 20,
            "call_status": "submitted",
            "duration_ms": 20,
        },
        {
            "seq": 8,
            "function_name": "delta",
            "logical_clock_ms": 150,
            "priority": 70,
            "expected_latency_ms": 10,
            "call_status": "submitted",
            "duration_ms": 10,
        },
    ]
    for row in rows:
        row["seeded_jitter_code"] = rng.randrange(1_000_000)
    return rows


def v1_policy() -> dict[str, Any]:
    return {
        "format": "v1",
        "schema": "coldstart-policy-v1",
        "version": "v1-seed-1",
        "declared_clock_ms": 1_000,
        "policies": [
            {
                "key": "pool:alpha",
                "function_name": "alpha",
                "effective_relative_seconds": 30,
                "generation": 1,
                "priority": 10,
                "min_ready": 1,
                "max_instances": 3,
                "warmup_batch": 1,
                "idle_ttl_ms": 600_000,
            },
            {
                "key": "pool:beta",
                "function_name": "beta",
                "effective_relative_seconds": 15,
                "generation": 1,
                "priority": 20,
                "min_ready": 1,
                "max_instances": 2,
                "warmup_batch": 1,
                "idle_ttl_ms": 100,
            },
            {
                "key": "pool:shared-cache",
                "function_name": "alpha",
                "effective_relative_seconds": 60,
                "generation": 1,
                "min_ready": 0,
                "max_instances": 1,
                "cache_key": "shared-python-layer",
            },
        ],
    }


def v2_shard_records() -> list[list[dict[str, Any]]]:
    shard_zero = [
        {
            "key": "pool:beta",
            "function_name": "beta",
            "generation": 1,
            "declared_clock_ms": 16_000,
            "effective_at_ms": 16_000,
            "tombstone": False,
            "priority": 20,
            "min_ready": 1,
            "max_instances": 2,
            "warmup_batch": 1,
        },
        {
            "key": "pool:gamma",
            "function_name": "gamma",
            "generation": 1,
            "effective_at_ms": 20_000,
            "tombstone": False,
            "priority": 30,
            "min_ready": 1,
            "max_instances": 2,
        },
    ]
    shard_one = [
        {
            "key": "pool:alpha",
            "function_name": "alpha",
            "generation": 2,
            "effective_at_ms": 32_000,
            "tombstone": False,
            "priority": 10,
            "min_ready": 2,
            "max_instances": 4,
            "warmup_batch": 2,
        },
        {
            "key": "pool:delta",
            "function_name": "delta",
            "generation": 2,
            "effective_at_ms": 21_000,
            "tombstone": False,
            "priority": 40,
            "min_ready": 0,
            "max_instances": 5,
        },
        {
            "key": "pool:flaky",
            "function_name": "flaky",
            "generation": 2,
            "effective_at_ms": 22_000,
            "tombstone": False,
            "priority": 50,
            "min_ready": 0,
            "max_instances": 1,
        },
    ]
    return [shard_zero, shard_one]


def scenario() -> dict[str, Any]:
    return {
        "seed": SEED,
        "horizon_ms": 320,
        "engine": {
            "global_cold_start_limit": 2,
            "round_budget_ms": 220,
            "precision_tolerance_ms": 10,
            "fixed_point_scale": 100,
            "fixed_point_unit_ms": 1000,
            "fixed_point_rounding": "floor",
            "recycle_grace_ms": 20,
            "capacity_shortage_policy": "hold_first_then_abandon_tail",
            "budget_shortage_policy": "abandon",
        },
        "rounds": [
            {
                "id": "warm-r1",
                "after_seq": 0,
                "clock_ms": 0,
                "budget_ms": 400,
                "global_cold_start_limit": 2,
                "candidates": [
                    {
                        "candidate_id": "alpha-1",
                        "function_name": "alpha",
                        "request_seq": 1,
                        "function_priority": 10,
                        "request_priority": 50,
                        "cold_start_ms": 100,
                    },
                    {
                        "candidate_id": "beta-1",
                        "function_name": "beta",
                        "request_seq": 3,
                        "function_priority": 20,
                        "request_priority": 40,
                        "cold_start_ms": 80,
                    },
                    {
                        "candidate_id": "gamma-1",
                        "function_name": "gamma",
                        "request_seq": 4,
                        "function_priority": 30,
                        "request_priority": 60,
                        "cold_start_ms": 40,
                    },
                    {
                        "candidate_id": "delta-1",
                        "function_name": "delta",
                        "request_seq": 8,
                        "function_priority": 40,
                        "request_priority": 70,
                        "cold_start_ms": 120,
                    },
                ],
            },
            {
                "id": "warm-r2",
                "after_seq": 7,
                "clock_ms": 140,
                "budget_ms": 100,
                "global_cold_start_limit": 2,
                "candidates": [
                    {
                        "candidate_id": "flaky-1",
                        "function_name": "flaky",
                        "request_seq": None,
                        "function_priority": 35,
                        "request_priority": 100,
                        "cold_start_ms": 60,
                    },
                    {
                        "candidate_id": "delta-2",
                        "function_name": "delta",
                        "request_seq": 8,
                        "function_priority": 40,
                        "request_priority": 70,
                        "cold_start_ms": 120,
                    },
                ],
            },
            {
                "id": "warm-r3",
                "after_seq": 8,
                "clock_ms": 170,
                "budget_ms": 220,
                "global_cold_start_limit": 2,
                "candidates": [],
            },
            {
                "id": "warm-r4",
                "after_seq": 8,
                "clock_ms": 180,
                "budget_ms": 220,
                "global_cold_start_limit": 2,
                "candidates": [
                    {
                        "candidate_id": "flaky-2",
                        "function_name": "flaky",
                        "request_seq": None,
                        "function_priority": 35,
                        "request_priority": 100,
                        "cold_start_ms": 60,
                    }
                ],
            },
        ],
    }


def _write_v2_baseline(root: Path) -> None:
    package = root / "policies_v2"
    shard_dir = package / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    shards = v2_shard_records()
    descriptors: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []

    for index, records in enumerate(shards):
        relative = f"shards/shard-{index:03d}.json"
        path = package / relative
        write_json(path, {"shard": index, "records": records})
        from .utils import sha256_file

        digest = sha256_file(path)
        generations = [int(record["generation"]) for record in records]
        descriptors.append(
            {
                "path": relative,
                "sha256": digest,
                "generation_min": min(generations),
                "generation_max": max(generations),
            }
        )
        all_records.extend(records)

    key_digest = stable_key_digest([record["key"] for record in all_records])
    manifest = {
        "format": "v2",
        "schema": "coldstart-policy-v2-sharded",
        "version": 2,
        "package_version": 2,
        "generation_floor": 1,
        "metadata_db": "metadata.sqlite",
        "key_set_digest": key_digest,
        "shards": descriptors,
    }
    write_json(package / "manifest.json", manifest)

    db_path = package / "metadata.sqlite"
    if db_path.exists():
        db_path.unlink()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE policy_metadata (
                key TEXT NOT NULL,
                generation INTEGER NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                tombstone INTEGER NOT NULL DEFAULT 0,
                shard_path TEXT NOT NULL,
                shard_hash TEXT NOT NULL,
                PRIMARY KEY(key, generation)
            )
            """
        )
        for descriptor, records in zip(descriptors, shards):
            for record in records:
                conn.execute(
                    """
                    INSERT INTO policy_metadata(
                        key, generation, effective_at_ms, tombstone,
                        shard_path, shard_hash
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["key"],
                        int(record["generation"]),
                        int(record["effective_at_ms"]),
                        1 if record.get("tombstone") else 0,
                        descriptor["path"],
                        descriptor["sha256"],
                    ),
                )
        conn.commit()


def generate_sample(root: str | Path = "data", seed: int = SEED) -> dict[str, str]:
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    write_json(root_path / "functions.json", {"functions": function_declarations()})
    write_json(root_path / "requests.json", {"seed": seed, "requests": request_stream(seed)})
    write_json(root_path / "policies_v1.json", v1_policy())
    write_json(root_path / "scenario.json", scenario())
    _write_v2_baseline(root_path)
    return {
        "root": str(root_path),
        "functions": str(root_path / "functions.json"),
        "requests": str(root_path / "requests.json"),
        "policies_v1": str(root_path / "policies_v1.json"),
        "policies_v2_manifest": str(root_path / "policies_v2" / "manifest.json"),
        "scenario": str(root_path / "scenario.json"),
    }


if __name__ == "__main__":
    paths = generate_sample()
    for key, value in paths.items():
        print(f"{key}={value}")
