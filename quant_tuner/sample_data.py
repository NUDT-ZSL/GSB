from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .config_io import write_v1, write_v2


ORIGIN_ABSOLUTE_MS = 1_700_000_000_000
SAMPLE_SEED = 20261127


def platform_stats() -> dict[str, Any]:
    return {
        "seed": SAMPLE_SEED,
        "cores": 4,
        "frequency_mhz": 2400.0,
        "memory_bandwidth_gbps": 42.0,
        "tile_dispatch_overhead_us": 1.25,
        "cache": [
            {"level": "L1", "size_kb": 32, "line_bytes": 64, "spill_cost_us_per_mb": 0.08},
            {"level": "L2", "size_kb": 512, "line_bytes": 64, "spill_cost_us_per_mb": 0.35},
            {"level": "LLC", "size_kb": 16384, "line_bytes": 64, "spill_cost_us_per_mb": 1.4},
        ],
        "throughput": {
            "matmul": {"mac_per_cycle_per_core": 16.0, "efficiency": 0.78},
            "conv2d": {"mac_per_cycle_per_core": 12.0, "efficiency": 0.70},
        },
        "dequant_ops_per_cycle_per_core": 8.0,
        "thread_occupancy": {
            "cores": 4,
            "base_process_mb": 12.0,
            "per_worker_mb": 6.0,
            "per_thread_mb": 1.5,
            "interference_penalty": 0.045,
        },
    }


def engine_config() -> dict[str, Any]:
    return {
        "seed": SAMPLE_SEED,
        "arbitration": {
            "worker_count": 3,
            "max_concurrency": 3,
            "logical_clock_budget_ms": 60_000.0,
            "memory_budget_mb": 256.0,
            "max_interference_factor": 1.4,
            "max_dequant_ops": 200_000_000,
            "max_relative_rmse": 0.09,
            "precision_upgrade_allowed": True,
            "high_precision_bits": 16,
            "timing_repeats": 3,
            "warmup_repeats": 1,
            "evaluation_overhead_ms": 0.02,
            "enforce_target_latency": False,
            "priority_order": ["progress", "memory", "precision"],
        },
    }


def tuning_tasks() -> list[dict[str, Any]]:
    return [
        {
            "tuning_id": "mm-0001",
            "config_generation": 1,
            "op_kind": "matmul",
            "workload_shape": {"M": 48, "N": 64, "K": 96},
            "quant_config": {"bits": 8, "scale": 0.035, "zero_point": 0},
            "target_latency_us": 500.0,
            "priority": 1,
            "logical_clock_ms": 1_000,
            "candidate_schedules": [
                {
                    "schedule_id": "mm-t1-small",
                    "schedule_revision": 1,
                    "threads": 1,
                    "tile_m": 16,
                    "tile_n": 16,
                    "tile_k": 32,
                    "vector_width": 8,
                    "pipeline_stages": 2,
                },
                {
                    "schedule_id": "mm-t2-balanced",
                    "schedule_revision": 1,
                    "threads": 2,
                    "tile_m": 32,
                    "tile_n": 32,
                    "tile_k": 32,
                    "vector_width": 8,
                    "pipeline_stages": 3,
                },
                {
                    "schedule_id": "mm-t4-wide",
                    "schedule_revision": 1,
                    "threads": 4,
                    "tile_m": 32,
                    "tile_n": 64,
                    "tile_k": 48,
                    "vector_width": 16,
                    "pipeline_stages": 4,
                },
            ],
        },
        {
            "tuning_id": "mm-0002",
            "config_generation": 1,
            "op_kind": "matmul",
            "workload_shape": {"M": 64, "N": 96, "K": 128},
            "quant_config": {"bits": 8, "scale": 0.05, "zero_point": 8},
            "target_latency_us": 900.0,
            "priority": 1,
            "logical_clock_ms": 2_000,
            "candidate_schedules": [
                {
                    "schedule_id": "mm-t1-small",
                    "schedule_revision": 1,
                    "threads": 1,
                    "tile_m": 16,
                    "tile_n": 16,
                    "tile_k": 32,
                    "vector_width": 8,
                    "pipeline_stages": 2,
                },
                {
                    "schedule_id": "mm-t2-balanced",
                    "schedule_revision": 1,
                    "threads": 2,
                    "tile_m": 32,
                    "tile_n": 32,
                    "tile_k": 64,
                    "vector_width": 8,
                    "pipeline_stages": 3,
                },
                {
                    "schedule_id": "mm-t4-wide",
                    "schedule_revision": 1,
                    "threads": 4,
                    "tile_m": 64,
                    "tile_n": 64,
                    "tile_k": 64,
                    "vector_width": 16,
                    "pipeline_stages": 4,
                },
            ],
        },
        {
            "tuning_id": "conv-0001",
            "config_generation": 1,
            "op_kind": "conv2d",
            "workload_shape": {
                "batch": 1,
                "height": 8,
                "width": 8,
                "in_channels": 4,
                "out_channels": 8,
                "kernel_h": 3,
                "kernel_w": 3,
                "stride": 1,
                "padding": 1,
            },
            "quant_config": {"bits": 8, "scale": 0.04, "zero_point": 128},
            "target_latency_us": 700.0,
            "priority": 2,
            "logical_clock_ms": 3_000,
            "candidate_schedules": [
                {
                    "schedule_id": "conv-t1",
                    "schedule_revision": 1,
                    "threads": 1,
                    "tile_m": 16,
                    "tile_n": 8,
                    "tile_k": 16,
                    "vector_width": 8,
                    "pipeline_stages": 2,
                },
                {
                    "schedule_id": "conv-t2",
                    "schedule_revision": 1,
                    "threads": 2,
                    "tile_m": 32,
                    "tile_n": 8,
                    "tile_k": 36,
                    "vector_width": 8,
                    "pipeline_stages": 3,
                },
                {
                    "schedule_id": "conv-t4",
                    "schedule_revision": 1,
                    "threads": 4,
                    "tile_m": 64,
                    "tile_n": 16,
                    "tile_k": 36,
                    "vector_width": 16,
                    "pipeline_stages": 3,
                },
            ],
        },
        {
            "tuning_id": "conv-0002",
            "config_generation": 1,
            "op_kind": "conv2d",
            "workload_shape": {
                "batch": 1,
                "height": 12,
                "width": 12,
                "in_channels": 8,
                "out_channels": 16,
                "kernel_h": 3,
                "kernel_w": 3,
                "stride": 1,
                "padding": 1,
            },
            "quant_config": {"bits": 8, "scale": 0.045, "zero_point": 120},
            "target_latency_us": 1200.0,
            "priority": 2,
            "logical_clock_ms": 4_000,
            "candidate_schedules": [
                {
                    "schedule_id": "conv-t1",
                    "schedule_revision": 1,
                    "threads": 1,
                    "tile_m": 24,
                    "tile_n": 16,
                    "tile_k": 32,
                    "vector_width": 8,
                    "pipeline_stages": 2,
                },
                {
                    "schedule_id": "conv-t2",
                    "schedule_revision": 1,
                    "threads": 2,
                    "tile_m": 48,
                    "tile_n": 16,
                    "tile_k": 72,
                    "vector_width": 8,
                    "pipeline_stages": 3,
                },
                {
                    "schedule_id": "conv-t4",
                    "schedule_revision": 1,
                    "threads": 4,
                    "tile_m": 96,
                    "tile_n": 16,
                    "tile_k": 72,
                    "vector_width": 16,
                    "pipeline_stages": 4,
                },
            ],
        },
    ]


def revised_tasks(original: list[dict[str, Any]]) -> list[dict[str, Any]]:
    revised = copy.deepcopy(original)
    for task in revised:
        if task["tuning_id"] == "mm-0001":
            task["config_generation"] = 2
            task["quant_config"]["scale"] = 0.028
        if task["tuning_id"] == "conv-0001":
            for schedule in task["candidate_schedules"]:
                if schedule["schedule_id"] == "conv-t2":
                    schedule["schedule_revision"] = 2
                    schedule["tile_m"] = 24
                    schedule["pipeline_stages"] = 4
    return revised


def config_bundle_from_tasks(
    tasks: list[dict[str, Any]], generation_overrides: dict[str, int] | None = None
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for task_index, task in enumerate(tasks):
        for candidate_index, schedule in enumerate(task["candidate_schedules"]):
            generation = (generation_overrides or {}).get(
                task["tuning_id"], int(task.get("config_generation", 1))
            )
            records.append(
                {
                    "record_id": f"cfg-{task['tuning_id']}-{schedule['schedule_id']}",
                    "tuning_id": task["tuning_id"],
                    "absolute_ms": ORIGIN_ABSOLUTE_MS
                    + int(task["logical_clock_ms"])
                    + candidate_index * 250
                    + task_index,
                    "generation": generation,
                    "quant_config": task["quant_config"],
                    "schedule": schedule,
                }
            )
    return {
        "format": "unified/v1",
        "source_version": "sample-generator",
        "origin_absolute_ms": ORIGIN_ABSOLUTE_MS,
        "records": records,
    }


def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in items),
        encoding="utf-8",
    )


def generate_all(output_dir: str | Path) -> dict[str, str]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    tasks = tuning_tasks()
    new_tasks = revised_tasks(tasks)

    task_path = root / "tuning_tasks.jsonl"
    revised_path = root / "tuning_tasks_revised.jsonl"
    platform_path = root / "platform_stats.json"
    config_path = root / "engine_config.json"
    v1_path = root / "v1_config.json"
    v2_dir = root / "v2_config"
    v2_revised_dir = root / "v2_config_revised"

    _write_jsonl(task_path, tasks)
    _write_jsonl(revised_path, new_tasks)
    platform_path.write_text(
        json.dumps(platform_stats(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    config_path.write_text(
        json.dumps(engine_config(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    bundle = config_bundle_from_tasks(tasks)
    write_v1(v1_path, bundle, ORIGIN_ABSOLUTE_MS)
    write_v2(v2_dir, bundle, shard_size=4)

    revised_bundle = config_bundle_from_tasks(new_tasks)
    for record in revised_bundle["records"]:
        if record["tuning_id"] == "mm-0001":
            record["generation"] = 2
        if (
            record["tuning_id"] == "conv-0001"
            and record["schedule"].get("schedule_id") == "conv-t2"
        ):
            record["generation"] = 2
    write_v2(v2_revised_dir, revised_bundle, shard_size=4)

    return {
        "samples_dir": str(root),
        "tasks": str(task_path),
        "revised_tasks": str(revised_path),
        "platform": str(platform_path),
        "config": str(config_path),
        "v1": str(v1_path),
        "v2": str(v2_dir),
        "v2_revised": str(v2_revised_dir),
    }
