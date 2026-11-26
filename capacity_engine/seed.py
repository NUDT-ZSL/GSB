from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from .validation import shard_content_hash


SEED = 20261127


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False, default=float) + "\n",
        encoding="utf-8",
    )


def _duration(service_index: int, window: int, local_index: int, rng: np.random.Generator) -> float:
    bases = [92.0, 176.0, 218.0]
    if local_index < 18:
        jitter = int(rng.integers(0, 11)) * 0.17
        wave = ((local_index * 7 + window * 3) % 5) * 0.23
        return round(bases[service_index] + jitter + wave, 3)
    sensitive_p95 = {
        (1, 1): 180.024,
        (1, 2): 180.026,
        (1, 3): 181.200,
    }.get((service_index, window))
    if local_index == 18 and sensitive_p95 is not None:
        return sensitive_p95
    if local_index == 18:
        return round(bases[service_index] + 5.0 + window * 0.35, 3)
    return round(bases[service_index] + 12.0 + window * 0.55 + local_index * 0.01, 3)


def generate_seed_directory(target: str | Path, *, clean: bool = True) -> Path:
    target = Path(target)
    if target.exists() and clean:
        shutil.rmtree(target)
    (target / "thresholds" / "v2").mkdir(parents=True, exist_ok=True)

    scenario = {
        "schema": "capacity-scenario/v1",
        "scenario_id": "deterministic-local-capacity",
        "logical_clock_origin_ms": 0,
        "seed": SEED,
        "services": [
            {
                "service_name": "gateway",
                "instance_id": "inst-gateway-1",
                "service_level": "platinum",
                "expected_request_rate_per_second": 2.0,
            },
            {
                "service_name": "checkout",
                "instance_id": "inst-checkout-1",
                "service_level": "gold",
                "expected_request_rate_per_second": 2.0,
            },
            {
                "service_name": "inventory",
                "instance_id": "inst-inventory-1",
                "service_level": "silver",
                "expected_request_rate_per_second": 2.0,
            },
        ],
    }
    write_json(target / "scenario.json", scenario)

    rng = np.random.default_rng(SEED)
    traces: list[dict[str, Any]] = []
    request_seq = 1
    for window in range(6):
        for service_index, service in enumerate(scenario["services"]):
            for local_index in range(20):
                clock = window * 10_000 + 250 + local_index * 470 + int(rng.integers(0, 37))
                duration = _duration(service_index, window, local_index, rng)
                success = "SUCCESS"
                dropped = "NOT_DROPPED"
                if service["service_name"] == "checkout" and window == 2 and local_index == 5:
                    success = "FAILURE"
                if service["service_name"] == "inventory" and window == 4 and local_index == 7:
                    success = "FAILURE"
                    dropped = "DROPPED"
                traces.append(
                    {
                        "request_seq": request_seq,
                        "service_name": service["service_name"],
                        "instance_id": service["instance_id"],
                        "logical_clock_ms": clock,
                        "duration_ms": duration,
                        "success_flag": success,
                        "drop_flag": dropped,
                    }
                )
                request_seq += 1
    rng.shuffle(traces)
    trace_text = "\n".join(json.dumps(record, ensure_ascii=False) for record in traces) + "\n"
    (target / "traces.jsonl").write_text(trace_text, encoding="utf-8")

    metrics = {
        "schema": "capacity-metric-definitions/v1",
        "window_length_ms": 10_000,
        "slide_ms": 10_000,
        "origin_ms": 0,
        "metrics": {
            "cpu_utilization": {
                "scale": 1000,
                "rounding": "half_up",
                "baseline_percent": "20.0",
                "duration_coefficient_per_ms": "0.006",
                "per_request_percent": "0.20",
                "pair_tolerance": "12.0",
                "threshold_margin_tolerance": "0.25",
                "higher_scale_multiplier": 100,
            },
            "memory_utilization": {
                "scale": 1000,
                "rounding": "half_up",
                "baseline_percent": "28.0",
                "per_request_percent": "1.60",
                "duration_coefficient_per_ms": "0.002",
                "pair_tolerance": "8.0",
                "threshold_margin_tolerance": "0.25",
                "higher_scale_multiplier": 100,
            },
            "latency_p95": {
                "scale": 10,
                "rounding": "half_up",
                "percentile": 95,
                "method": "nearest_rank_ceil",
                "pair_tolerance": "0.50",
                "threshold_margin_tolerance": "0.10",
                "higher_scale_multiplier": 100,
            },
            "throughput_attainment": {
                "scale": 10000,
                "rounding": "half_up",
                "pair_tolerance": "0.06",
                "threshold_margin_tolerance": "0.01",
                "higher_scale_multiplier": 100,
            },
        },
    }
    write_json(target / "metric_definitions.json", metrics)

    v1 = {
        "schema": "capacity-thresholds-v1-flat-json",
        "snapshot_id": "v1-legacy-flat",
        "logical_clock_ms": 3000,
        "generation": 3,
        "effective_after_seconds": 3,
        "thresholds": {
            "platinum.cpu_utilization": 48.0,
            "platinum.memory_utilization": 76.0,
            "platinum.latency_p95": 125.0,
            "platinum.throughput_attainment": 0.99,
            "gold.cpu_utilization": 55.0,
            "gold.memory_utilization": 76.0,
            "gold.latency_p95": 190.0,
            "gold.throughput_attainment": 0.98,
            "silver.cpu_utilization": 75.0,
            "silver.memory_utilization": 72.0,
            "silver.latency_p95": 240.0,
            "silver.throughput_attainment": 0.97,
        },
    }
    write_json(target / "thresholds" / "v1.json", v1)

    shard_0 = {
        "schema": "capacity-thresholds-v2-shard",
        "shard_id": "shard-000",
        "shard_index": 0,
        "generation": 2,
        "logical_clock_ms": 2000,
        "effective_at_ms": 2000,
        "thresholds": {
            "silver.cpu_utilization": 76.0,
            "silver.latency_p95": 245.0,
        },
        "dependencies": [],
        "tombstones": [
            {
                "service_level": "platinum",
                "metric": "memory_utilization",
                "generation": 2,
                "effective_at_ms": 2000,
            }
        ],
    }
    shard_1 = {
        "schema": "capacity-thresholds-v2-shard",
        "shard_id": "shard-001",
        "shard_index": 1,
        "generation": 3,
        "logical_clock_ms": 3000,
        "effective_at_ms": 3000,
        "thresholds": {
            "gold.cpu_utilization": 50.0,
            "gold.memory_utilization": 74.0,
        },
        "dependencies": [
            {"service_level": "gold", "depends_on_service_level": "platinum"}
        ],
        "tombstones": [],
    }
    shard_2 = {
        "schema": "capacity-thresholds-v2-shard",
        "shard_id": "shard-002",
        "shard_index": 2,
        "generation": 4,
        "logical_clock_ms": 4000,
        "effective_at_ms": 4000,
        "thresholds": {
            "gold.latency_p95": 180.05,
            "gold.throughput_attainment": 0.98,
            "platinum.latency_p95": 115.0,
        },
        "dependencies": [],
        "tombstones": [],
    }
    shards = [shard_0, shard_1, shard_2]
    for shard in shards:
        shard["sha256"] = shard_content_hash(shard)
        write_json(target / "thresholds" / "v2" / f"{shard['shard_id']}.json", shard)

    db_path = target / "thresholds" / "v2" / "metadata.sqlite"
    if db_path.exists():
        db_path.unlink()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE shards (
                shard_id TEXT PRIMARY KEY,
                shard_index INTEGER NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                generation INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                effective_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE dependencies (
                id INTEGER PRIMARY KEY,
                service_level TEXT NOT NULL,
                depends_on_service_level TEXT NOT NULL,
                source_shard_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE tombstones (
                id INTEGER PRIMARY KEY,
                shard_id TEXT NOT NULL,
                service_level TEXT NOT NULL,
                metric TEXT NOT NULL,
                generation INTEGER NOT NULL,
                effective_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO dependencies(id, service_level, depends_on_service_level, source_shard_id) VALUES (1, ?, ?, NULL)",
            ("silver", "shared"),
        )
        for shard in shards:
            conn.execute(
                "INSERT INTO shards VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    shard["shard_id"],
                    shard["shard_index"],
                    f"thresholds/v2/{shard['shard_id']}.json",
                    shard["sha256"],
                    shard["generation"],
                    shard["logical_clock_ms"],
                    shard["effective_at_ms"],
                ),
            )
            for index, dep in enumerate(shard.get("dependencies", []), start=10):
                conn.execute(
                    "INSERT INTO dependencies(id, service_level, depends_on_service_level, source_shard_id) VALUES (?, ?, ?, ?)",
                    (index, dep["service_level"], dep["depends_on_service_level"], shard["shard_id"]),
                )
            for index, tomb in enumerate(shard.get("tombstones", []), start=20):
                conn.execute(
                    "INSERT INTO tombstones(id, shard_id, service_level, metric, generation, effective_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        index,
                        shard["shard_id"],
                        tomb["service_level"],
                        tomb["metric"],
                        tomb["generation"],
                        tomb["effective_at_ms"],
                    ),
                )
        conn.commit()

    patches = {
        "schema": "capacity-patch-list/v1",
        "patches": [
            {
                "patch_id": "patch-001-gold-cpu-threshold",
                "kind": "threshold_update",
                "logical_clock_ms": 70000,
                "target": {"service_level": "gold", "metric": "cpu_utilization"},
                "threshold": {"operator": "max", "value": 42.0},
            },
            {
                "patch_id": "patch-002-gold-shared-dependency",
                "kind": "dependency_change",
                "logical_clock_ms": 71000,
                "dependency": {
                    "action": "add",
                    "service_level": "gold",
                    "depends_on_service_level": "shared-cache",
                },
            },
            {
                "patch_id": "patch-003-silver-memory-tombstone",
                "kind": "tombstone",
                "logical_clock_ms": 72000,
                "target": {"service_level": "silver", "metric": "memory_utilization"},
            },
            {
                "patch_id": "patch-004-cpu-scale-ten-thousand",
                "kind": "metric_parameter_update",
                "logical_clock_ms": 73000,
                "metric": "cpu_utilization",
                "parameters": {"scale": 10000},
            },
        ],
    }
    write_json(target / "patches.json", patches)

    anomalies = {
        "schema": "capacity-anomaly-injections/v1",
        "fixed_seed": SEED,
        "crash_exit_code": 86,
        "crash_stages": [
            "window_aggregation",
            "fixedpoint_conversion",
            "threshold_merge",
            "patch_commit",
        ],
        "validation_mutation_cases": [
            "duplicate_service_name",
            "duplicate_instance_id",
            "negative_duration",
            "non_finite_rate",
            "invalid_success_marker",
            "scale_not_positive_integer",
            "v1_negative_seconds",
            "v2_shard_hash_mismatch",
            "v2_generation_not_monotonic",
        ],
    }
    write_json(target / "anomalies.json", anomalies)
    return target
