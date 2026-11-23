"""Deterministic sample workspace generator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np


def _series(
    rng: np.random.Generator,
    starts: list[tuple[int, DecimalLike]],
    *,
    jitter: DecimalLike,
    end_ms: int = 10_000,
    step_ms: int = 250,
) -> list[float]:
    values: list[float] = []
    sorted_starts = sorted(starts)
    for timestamp in range(0, end_ms, step_ms):
        base = sorted_starts[0][1]
        for start, candidate in sorted_starts:
            if timestamp >= start:
                base = candidate
        noise = float(rng.normal(0.0, float(jitter)))
        values.append(round(float(base) + noise, 6))
    return values


DecimalLike = float | int


def build_sample_payloads() -> dict[str, Any]:
    rng = np.random.default_rng(20261124)
    timestamps = list(range(0, 10_000, 250))
    api_cpu = _series(
        rng,
        [
            (0, 0.50),
            (1_000, 0.91),
            (2_000, 0.78),
            (3_000, 0.83),
            (4_000, 0.92),
            (5_000, 0.90),
            (6_000, 0.55),
            (7_000, 0.52),
            (8_000, 0.91),
        ],
        jitter=0.012,
    )
    api_latency = _series(
        rng,
        [
            (0, 130),
            (1_000, 278),
            (2_000, 190),
            (3_000, 205),
            (4_000, 292),
            (5_000, 270),
            (6_000, 120),
            (7_000, 110),
            (8_000, 285),
        ],
        jitter=6.0,
    )
    worker_queue = _series(
        rng,
        [
            (0, 39),
            (1_000, 37),
            (2_000, 8),
            (3_000, 9),
            (4_000, 40),
            (5_000, 38),
            (6_000, 7),
            (7_000, 8),
            (8_000, 42),
        ],
        jitter=1.2,
    )
    batch_mem = _series(
        rng,
        [
            (0, 410),
            (1_000, 398),
            (2_000, 430),
            (3_000, 445),
            (4_000, 648),
            (5_000, 660),
            (6_000, 540),
            (7_000, 400),
            (8_000, 655),
            (9_000, 650),
        ],
        jitter=4.0,
    )
    archive_cpu = _series(
        rng,
        [
            (0, 830),
            (1_000, 905),
            (2_000, 890),
            (3_000, 910),
            (4_000, 920),
            (5_000, 900),
            (6_000, 880),
            (7_000, 910),
            (8_000, 930),
            (9_000, 925),
        ],
        jitter=8.0,
    )
    series = {
        "api_cpu": api_cpu,
        "api_latency": api_latency,
        "worker_queue": worker_queue,
        "batch_mem": batch_mem,
        "archive_cpu": archive_cpu,
    }
    samples: list[dict[str, Any]] = []
    for metric in sorted(series):
        for index, (timestamp, value) in enumerate(zip(timestamps, series[metric])):
            samples.append(
                {
                    "sample_id": f"{metric}-{index:04d}",
                    "metric": metric,
                    "timestamp_ms": timestamp,
                    "value": value,
                }
            )
    samples.sort(key=lambda item: (item["timestamp_ms"], item["metric"], item["sample_id"]))

    workloads = {
        "workloads": [
            {
                "name": "api-web",
                "current_replicas": 3,
                "min_replicas": 1,
                "max_replicas": 10,
                "cpu_millicores": 250,
                "memory_mib": 128,
                "topology_domain": "zone-a",
            },
            {
                "name": "worker",
                "current_replicas": 2,
                "min_replicas": 1,
                "max_replicas": 8,
                "cpu_millicores": 500,
                "memory_mib": 256,
                "topology_domain": "zone-b",
            },
            {
                "name": "batch",
                "current_replicas": 6,
                "min_replicas": 2,
                "max_replicas": 12,
                "cpu_millicores": 1000,
                "memory_mib": 512,
                "topology_domain": "zone-c",
            },
            {
                "name": "archive",
                "current_replicas": 2,
                "min_replicas": 1,
                "max_replicas": 8,
                "cpu_millicores": 4000,
                "memory_mib": 1024,
                "topology_domain": "zone-c",
            },
        ]
    }
    metrics = {
        "metrics": [
            {
                "name": "api_cpu",
                "aggregation": "average",
                "window_ms": 1000,
                "target_value": 0.60,
                "alert_threshold": 0.80,
                "lower_threshold": 0.40,
                "scale": 1000,
                "error_budget": 0.01,
            },
            {
                "name": "api_latency",
                "aggregation": "percentile",
                "percentile": 95,
                "window_ms": 1000,
                "target_value": 200,
                "alert_threshold": 240,
                "lower_threshold": 150,
                "scale": 100,
                "error_budget": 0.05,
            },
            {
                "name": "worker_queue",
                "aggregation": "average",
                "window_ms": 1000,
                "target_value": 20,
                "alert_threshold": 30,
                "lower_threshold": 10,
                "scale": 100,
                "error_budget": 0.05,
            },
            {
                "name": "batch_mem",
                "aggregation": "average",
                "window_ms": 1000,
                "target_value": 512,
                "alert_threshold": 620,
                "lower_threshold": 420,
                "scale": 100,
                "error_budget": 0.05,
            },
            {
                "name": "archive_cpu",
                "aggregation": "average",
                "window_ms": 1000,
                "target_value": 50,
                "alert_threshold": 70,
                "lower_threshold": 30,
                "scale": 100,
                "error_budget": 0.01,
            },
        ]
    }
    rules = {
        "rules": [
            {
                "name": "api-cpu-scale-up",
                "workload": "api-web",
                "metric": "api_cpu",
                "direction": "up",
                "step": 1,
                "percent": 20,
                "cooldown_ms": 3000,
                "freeze_ms": 2000,
                "priority": 100,
                "duration_ms": 1000,
            },
            {
                "name": "api-latency-scale-up",
                "workload": "api-web",
                "metric": "api_latency",
                "direction": "up",
                "step": 2,
                "percent": 0,
                "cooldown_ms": 4000,
                "freeze_ms": 2000,
                "priority": 100,
                "duration_ms": 1000,
            },
            {
                "name": "api-cpu-scale-down",
                "workload": "api-web",
                "metric": "api_cpu",
                "direction": "down",
                "step": 1,
                "percent": 10,
                "cooldown_ms": 2000,
                "freeze_ms": 3000,
                "priority": 50,
                "duration_ms": 1000,
            },
            {
                "name": "worker-scale-up",
                "workload": "worker",
                "metric": "worker_queue",
                "direction": "up",
                "step": 1,
                "percent": 50,
                "cooldown_ms": 2500,
                "freeze_ms": 2000,
                "priority": 80,
                "duration_ms": 1000,
            },
            {
                "name": "batch-scale-down",
                "workload": "batch",
                "metric": "batch_mem",
                "direction": "down",
                "step": 1,
                "percent": 10,
                "cooldown_ms": 2000,
                "freeze_ms": 3000,
                "priority": 60,
                "duration_ms": 3000,
            },
            {
                "name": "batch-scale-up",
                "workload": "batch",
                "metric": "batch_mem",
                "direction": "up",
                "step": 1,
                "percent": 10,
                "cooldown_ms": 2000,
                "freeze_ms": 3000,
                "priority": 55,
                "duration_ms": 2000,
            },
            {
                "name": "archive-scale-up",
                "workload": "archive",
                "metric": "archive_cpu",
                "direction": "up",
                "step": 1,
                "percent": 0,
                "cooldown_ms": 1000,
                "freeze_ms": 1000,
                "priority": 10,
                "duration_ms": 1000,
            },
        ]
    }
    for rule in rules["rules"]:
        rule["version"] = 1
    budget = {
        "budget": {
            "pool_cpu_core_ms": 15000,
            "pool_memory_mib": 8192,
            "round_time_budget_ms": 5000,
            "per_workload_concurrency": 2,
            "float_tolerance": 0.01,
            "error_upper_bound": 1,
            "clock_rollback_tolerance_ms": 50,
            "backoff_base_ms": 1000,
            "action_duration_ms": 1000,
        }
    }
    stream = {
        "description": "Deterministic logical-clock sample stream",
        "samples": samples,
        "events": [
            {
                "type": "action_outcome",
                "round_id": "r1",
                "workload": "worker",
                "success": False,
                "reason": "injected_worker_failure",
            }
        ],
        "rounds": [
            {"round_id": "r1", "at_ms": 2000, "workloads": ["api-web", "worker", "batch", "archive"]},
            {"round_id": "r2", "at_ms": 5000, "workloads": ["api-web", "worker", "batch", "archive"]},
            {"round_id": "r3", "at_ms": 8000, "workloads": ["api-web", "worker", "batch", "archive"]},
            {"round_id": "r4", "at_ms": 9000, "workloads": ["api-web", "worker", "batch", "archive"]},
        ],
    }
    return {
        "workloads.json": workloads,
        "metrics.json": metrics,
        "rules.json": rules,
        "budget.json": budget,
        "samples.json": stream,
    }


def generate_samples(directory: str | Path) -> list[Path]:
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, payload in build_sample_payloads().items():
        path = destination / filename
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        written.append(path)
    return written
