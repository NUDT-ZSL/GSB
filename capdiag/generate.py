from __future__ import annotations

import json
import random
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .jsonutil import dump_json, sha256_file


SEED = 20261126
WINDOW_COUNT = 6
WINDOW_MS = 10_000


def _write_trace(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def _duration_list(rng: random.Random, count: int, service: str, window: int) -> list[int]:
    if service == "svc-alpha":
        low, high = 42, 118
    elif service == "svc-beta":
        low, high = 35, 105
    else:
        low, high = 20, 90
    values = [rng.randint(low, high) for _ in range(count)]
    if service == "svc-beta" and window == 3:
        for index in range(3):
            values[index] = rng.randint(178, 196)
    if service == "svc-alpha" and window == 4:
        values[0] = 132
        values[1] = 141
    return values


def _statuses(count: int, success_count: int, dropped_count: int) -> list[tuple[bool, bool]]:
    failed = count - success_count
    if dropped_count > failed:
        raise ValueError("dropped records must be a subset of unsuccessful records")
    statuses: list[tuple[bool, bool]] = []
    for index in range(count):
        if index < dropped_count:
            statuses.append((False, True))
        elif index < failed:
            statuses.append((False, False))
        else:
            statuses.append((True, False))
    return statuses


def generate_sample_workspace(root: str | Path) -> dict[str, str]:
    root = Path(root)
    samples = root / "samples"
    if samples.exists():
        shutil.rmtree(samples)
    v2_dir = samples / "thresholds_v2"
    v2_next_dir = samples / "thresholds_v2_next"
    v2_dir.mkdir(parents=True)
    v2_next_dir.mkdir(parents=True)

    rng = random.Random(SEED)

    service_specs = [
        {
            "service_name": "svc-alpha",
            "service_level": "gold",
            "expected_request_rate": "12.0",
            "instances": [
                {"instance_id": "alpha-1", "expected_request_rate": "6.0"},
                {"instance_id": "alpha-2", "expected_request_rate": "6.0"},
            ],
            "count": 60,
        },
        {
            "service_name": "svc-beta",
            "service_level": "silver",
            "expected_request_rate": "8.0",
            "instances": [
                {"instance_id": "beta-1", "expected_request_rate": "4.0"},
                {"instance_id": "beta-2", "expected_request_rate": "4.0"},
            ],
            "count": 40,
        },
        {
            "service_name": "svc-gamma",
            "service_level": "bronze",
            "expected_request_rate": "5.0",
            "instances": [
                {"instance_id": "gamma-1", "expected_request_rate": "2.5"},
                {"instance_id": "gamma-2", "expected_request_rate": "2.5"},
            ],
            "count": 25,
        },
    ]

    scenario = {
        "schema": "capacity-scenario/v1",
        "scenario_id": "deterministic-capacity-431",
        "seed": SEED,
        "origin_logical_ms": 0,
        "precision_tolerance": "0.00005",
        "services": service_specs,
    }
    dump_json(samples / "scenario.json", scenario)

    trace_rows: list[dict[str, Any]] = []
    seq_by_instance: dict[str, int] = {}

    for window in range(WINDOW_COUNT):
        window_start = window * WINDOW_MS
        for service_index, service in enumerate(service_specs):
            service_name = service["service_name"]
            count_per_instance = int(service["count"])
            for instance_index, instance in enumerate(service["instances"]):
                instance_id = instance["instance_id"]
                count = count_per_instance
                offsets = sorted(rng.sample(range(250, WINDOW_MS - 250), count))
                durations = _duration_list(rng, count, service_name, window)

                if service_name == "svc-alpha":
                    # Reuse one duration vector and introduce exactly 1ms total
                    # difference, creating a precision-sensitive instance pair.
                    if instance_index == 0:
                        alpha_duration_template = list(durations)
                    else:
                        durations = list(alpha_duration_template)
                        durations[0] += 1

                if service_name == "svc-alpha":
                    if window == 2:
                        success_count, dropped_count = count - 1, 1
                    elif window == 4:
                        success_count, dropped_count = count - 2, 1
                    else:
                        success_count, dropped_count = count, 0
                elif service_name == "svc-beta":
                    success_count, dropped_count = count - 3, 2
                else:
                    if window % 2 == 0:
                        success_count, dropped_count = count - 4, 3
                    else:
                        success_count, dropped_count = count - 2, 1

                statuses = _statuses(count, success_count, dropped_count)
                for local_index in range(count):
                    seq_by_instance[instance_id] = seq_by_instance.get(instance_id, 0) + 1
                    success, dropped = statuses[local_index]
                    trace_rows.append(
                        {
                            "request_seq": seq_by_instance[instance_id],
                            "service_name": service_name,
                            "instance_id": instance_id,
                            "logical_clock_ms": window_start + offsets[local_index],
                            "duration_ms": durations[local_index],
                            "success": success,
                            "dropped": dropped,
                        }
                    )

    _write_trace(samples / "traces.jsonl", trace_rows)

    metrics = {
        "schema": "capacity-metric-definitions/v1",
        "window_count": WINDOW_COUNT,
        "metrics": {
            "cpu_utilization": {
                "scale": 1000,
                "window_length_ms": WINDOW_MS,
                "window_step_ms": WINDOW_MS,
                "kind": "linear_ratio",
                "direction": "higher_is_worse",
                "probe_scale_multiplier": 10000,
                "params": {
                    "base_units": 180000,
                    "count_coefficient": 3000,
                    "duration_coefficient": 10,
                    "success_coefficient": 0,
                    "dropped_coefficient": 0,
                    "denominator": 1000000,
                },
            },
            "memory_utilization": {
                "scale": 1000,
                "window_length_ms": WINDOW_MS,
                "window_step_ms": WINDOW_MS,
                "kind": "linear_ratio",
                "direction": "higher_is_worse",
                "probe_scale_multiplier": 10000,
                "params": {
                    "base_units": 350000,
                    "count_coefficient": 0,
                    "success_coefficient": 2500,
                    "duration_coefficient": 5,
                    "dropped_coefficient": 4000,
                    "denominator": 1000000,
                },
            },
            "latency_p95_ms": {
                "scale": 100,
                "window_length_ms": WINDOW_MS,
                "window_step_ms": WINDOW_MS,
                "kind": "latency_p95",
                "direction": "higher_is_worse",
                "probe_scale_multiplier": 1000,
                "params": {},
            },
            "throughput_attainment": {
                "scale": 10000,
                "window_length_ms": WINDOW_MS,
                "window_step_ms": WINDOW_MS,
                "kind": "throughput_ratio",
                "direction": "lower_is_worse",
                "probe_scale_multiplier": 1000,
                "params": {},
            },
        },
    }
    dump_json(samples / "metrics.json", metrics)

    high_scale_metrics = json.loads(json.dumps(metrics))
    high_scale_metrics["metrics"]["cpu_utilization"]["scale"] = 1_000_000
    high_scale_metrics["schema"] = "capacity-metric-definitions-high-scale/v1"
    dump_json(samples / "metrics_cpu_high_scale.json", high_scale_metrics)

    v1_thresholds = {
        "schema": "capacity-thresholds-v1/v1",
        "snapshot_id": "v1-flat-baseline",
        "threshold_generation": 1,
        "effective_relative_seconds": 0,
        "thresholds": {
            "default:cpu_utilization:warn": "0.70",
            "default:cpu_utilization:fail": "0.88",
            "svc-alpha:cpu_utilization:warn": "0.67",
            "svc-alpha:cpu_utilization:fail": "0.85",
            "default:memory_utilization:warn": "0.80",
            "default:memory_utilization:fail": "0.92",
            "svc-beta:memory_utilization:warn": "0.80",
            "svc-beta:memory_utilization:fail": "0.92",
            "svc-gamma:latency_p95_ms:warn": "170",
            "default:latency_p95_ms:warn": "150",
            "default:latency_p95_ms:fail": "260",
            "default:throughput_attainment:warn": "0.95",
            "default:throughput_attainment:fail": "0.85",
        },
    }
    dump_json(samples / "thresholds_v1.json", v1_thresholds)

    v2_entries = [
        {"scope": "default", "metric": "cpu_utilization", "level": "warn", "value": "0.70"},
        {"scope": "default", "metric": "cpu_utilization", "level": "fail", "value": "0.88"},
        {"scope": "svc-alpha", "metric": "cpu_utilization", "level": "warn", "value": "0.65"},
        {"scope": "svc-alpha", "metric": "cpu_utilization", "level": "fail", "value": "0.85"},
        {"scope": "default", "metric": "memory_utilization", "level": "warn", "value": "0.78"},
        {"scope": "default", "metric": "memory_utilization", "level": "fail", "value": "0.92"},
        {"scope": "svc-beta", "metric": "memory_utilization", "level": "warn", "value": "0.76"},
        {"scope": "svc-beta", "metric": "memory_utilization", "level": "fail", "value": "0.92"},
        {"scope": "default", "metric": "latency_p95_ms", "level": "warn", "value": "150"},
        {"scope": "default", "metric": "latency_p95_ms", "level": "fail", "value": "260"},
        {"scope": "default", "metric": "throughput_attainment", "level": "warn", "value": "0.95"},
        {"scope": "default", "metric": "throughput_attainment", "level": "fail", "value": "0.85"},
    ]
    _write_v2_snapshot(
        directory=v2_dir,
        metadata_db=samples / "thresholds_v2_metadata.sqlite",
        snapshot_id="v2-sharded-gen2",
        generation=2,
        effective_ms=0,
        entries=v2_entries,
        tombstones=["svc-gamma:latency_p95_ms:warn"],
    )

    next_entries = [dict(entry) for entry in v2_entries]
    for entry in next_entries:
        if (
            entry["scope"] == "svc-alpha"
            and entry["metric"] == "cpu_utilization"
            and entry["level"] == "warn"
        ):
            entry["value"] = "0.60"
    _write_v2_snapshot(
        directory=v2_next_dir,
        metadata_db=samples / "thresholds_v2_next_metadata.sqlite",
        snapshot_id="v2-sharded-gen3-alpha-cpu",
        generation=3,
        effective_ms=35_000,
        entries=next_entries,
        tombstones=["svc-gamma:latency_p95_ms:warn"],
    )

    invalid_path = samples / "invalid_traces.jsonl"
    invalid_rows = [
        trace_rows[0],
        {**trace_rows[1], "request_seq": 1},
        {
            "request_seq": 999,
            "service_name": "svc-alpha",
            "instance_id": "alpha-1",
            "logical_clock_ms": -5,
            "duration_ms": 0,
            "success": "yes",
            "dropped": False,
        },
        {
            "request_seq": 1000,
            "service_name": "svc-unknown",
            "instance_id": "unknown-1",
            "logical_clock_ms": 10,
            "duration_ms": 3,
            "success": True,
            "dropped": False,
        },
    ]
    _write_trace(invalid_path, invalid_rows)

    return {
        "samples_dir": str(samples),
        "scenario": str(samples / "scenario.json"),
        "traces": str(samples / "traces.jsonl"),
        "metrics": str(samples / "metrics.json"),
        "thresholds_v1": str(samples / "thresholds_v1.json"),
        "thresholds_v2_metadata": str(samples / "thresholds_v2_metadata.sqlite"),
        "thresholds_v2_next_metadata": str(samples / "thresholds_v2_next_metadata.sqlite"),
    }


def _write_v2_snapshot(
    *,
    directory: Path,
    metadata_db: Path,
    snapshot_id: str,
    generation: int,
    effective_ms: int,
    entries: list[dict[str, Any]],
    tombstones: list[str],
) -> None:
    if metadata_db.exists():
        metadata_db.unlink()
    half = (len(entries) + 1) // 2
    shards = [
        {
            "schema": "capacity-thresholds-v2-shard/v1",
            "snapshot_id": snapshot_id,
            "shard_id": 0,
            "generation": generation,
            "effective_absolute_ms": effective_ms,
            "entries": entries[:half],
            "tombstones": [],
        },
        {
            "schema": "capacity-thresholds-v2-shard/v1",
            "snapshot_id": snapshot_id,
            "shard_id": 1,
            "generation": generation,
            "effective_absolute_ms": effective_ms,
            "entries": entries[half:],
            "tombstones": tombstones,
        },
    ]

    metadata_rows: list[tuple[int, str, str, int]] = []
    for shard in shards:
        shard_id = int(shard["shard_id"])
        filename = f"shard_{shard_id:03d}.json"
        path = directory / filename
        dump_json(path, shard)
        relative_path = f"{directory.name}/{filename}"
        metadata_rows.append(
            (shard_id, relative_path, sha256_file(path), generation)
        )

    conn = sqlite3.connect(metadata_db)
    try:
        conn.execute(
            "CREATE TABLE snapshot ("
            "snapshot_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, "
            "effective_absolute_ms INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE shards ("
            "shard_id INTEGER PRIMARY KEY, path TEXT NOT NULL, "
            "sha256 TEXT NOT NULL, generation INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO snapshot VALUES (?, ?, ?)",
            (snapshot_id, generation, effective_ms),
        )
        conn.executemany(
            "INSERT INTO shards(shard_id, path, sha256, generation) VALUES (?, ?, ?, ?)",
            metadata_rows,
        )
        conn.commit()
    finally:
        conn.close()
