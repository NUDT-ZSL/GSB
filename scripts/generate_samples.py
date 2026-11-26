#!/usr/bin/env python3
"""Generate the fixed-seed local acceptance samples."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine_diag.models import CACHE_LAYERS
from engine_diag.util import digest_obj, digest_text, stable_json_dumps


SAMPLE_SEED = 20261127


def _task(
    job_id: str,
    op_kind: str,
    shapes: list[list[int]],
    dtype: str,
    layout: str,
    seed: int,
    revision: int,
    clock: int,
    precision_sensitive: bool,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "op_kind": op_kind,
        "input_shapes": shapes,
        "dtype": dtype,
        "layout": layout,
        "seed": seed,
        "revision": revision,
        "logical_clock_ms": clock,
        "precision_sensitive": precision_sensitive,
    }


def _tasks_v1() -> list[dict[str, Any]]:
    return [
        _task(
            "job-alpha",
            "matmul",
            [[64, 96], [96, 48]],
            "float16",
            "ROW_MAJOR",
            1001,
            1,
            100,
            True,
        ),
        _task(
            "job-beta",
            "conv2d",
            [[2, 32, 28, 28], [64, 32, 3, 3]],
            "float32",
            "NHWC",
            1002,
            1,
            160,
            False,
        ),
        _task(
            "job-gamma",
            "matmul",
            [[32, 64], [64, 128]],
            "float32",
            "COL_MAJOR",
            1003,
            1,
            220,
            False,
        ),
        _task(
            "job-delta",
            "conv2d",
            [[1, 16, 32, 32], [32, 16, 5, 5]],
            "bfloat16",
            "NCHW",
            1004,
            1,
            280,
            True,
        ),
    ]


def _tasks_revision() -> list[dict[str, Any]]:
    return [
        _task(
            "job-gamma",
            "matmul",
            [[32, 80], [80, 128]],
            "float32",
            "COL_MAJOR",
            2003,
            2,
            600,
            False,
        )
    ]


def _cost(cpu: float, memory: float) -> dict[str, float]:
    return {"cpu_instructions": cpu, "memory_bytes": memory}


def _job(
    job_id: str,
    priority: int,
    deadline: float,
    cpu_weight: float,
    memory_weight: float,
    *,
    depends_on: list[str] | None = None,
    critical_path_id: str,
    precision_sensitive: bool,
    threads_required: int,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "priority": priority,
        "deadline_clock_ms": deadline,
        "cost_estimates": {
            "weight_sum": _cost(cpu_weight, memory_weight),
            "activation": _cost(6000.0, 4500.0),
        },
        "dependencies": depends_on or [],
        "critical_path_id": critical_path_id,
        "precision_sensitive": precision_sensitive,
        "threads_required": threads_required,
    }


def _base_plan_dicts() -> tuple[dict[str, Any], dict[str, Any]]:
    jobs_v1 = {
        "job-alpha": _job(
            "job-alpha", 1, 160.0, 24000.0, 16000.0,
            critical_path_id="path-alpha-beta",
            precision_sensitive=True,
            threads_required=4,
        ),
        "job-beta": _job(
            "job-beta", 2, 260.0, 32000.0, 24000.0,
            depends_on=["job-alpha"],
            critical_path_id="path-alpha-beta",
            precision_sensitive=False,
            threads_required=8,
        ),
        "job-gamma": _job(
            "job-gamma", 1, 340.0, 18000.0, 12000.0,
            critical_path_id="path-gamma-delta",
            precision_sensitive=False,
            threads_required=4,
        ),
        "job-delta": _job(
            "job-delta", 2, 440.0, 26000.0, 20000.0,
            depends_on=["job-gamma"],
            critical_path_id="path-gamma-delta",
            precision_sensitive=True,
            threads_required=6,
        ),
    }
    v1 = {
        "plan_version": "p1",
        "global_seed": SAMPLE_SEED,
        "base_logical_clock_ms": 0,
        "logical_clock_step_ms": 10,
        "max_threads": 16,
        "cost_model": {
            "instruction_weight": 1.0,
            "memory_weight": 0.75,
            "cache_cpu_penalty": 0.35,
            "cache_memory_penalty": 2.0,
            "occupancy_penalty": 0.15,
            "logical_ms_per_cost_unit": 0.001,
        },
        "jobs": jobs_v1,
        "candidates": [
            {
                "candidate_id": "alpha-beta-gamma-delta",
                "job_order": ["job-alpha", "job-beta", "job-gamma", "job-delta"],
                "interleave_jobs": False,
            },
            {
                "candidate_id": "delta-gamma-beta-alpha",
                "job_order": ["job-delta", "job-gamma", "job-beta", "job-alpha"],
                "interleave_jobs": False,
            },
            {
                "candidate_id": "interleaved-deadline",
                "job_order": ["job-gamma", "job-alpha", "job-delta", "job-beta"],
                "interleave_jobs": True,
            },
        ],
        "critical_paths": {
            "path-alpha-beta": ["job-alpha", "job-beta"],
            "path-gamma-delta": ["job-gamma", "job-delta"],
        },
    }

    jobs_v2 = {
        key: json.loads(json.dumps(value))
        for key, value in jobs_v1.items()
        if key != "job-delta"
    }
    jobs_v2["job-beta"]["deadline_clock_ms"] = 280.0
    jobs_v2["job-beta"]["cost_estimates"]["activation"] = _cost(6200.0, 4600.0)
    v2 = {
        "plan_version": "p2",
        "global_seed": SAMPLE_SEED,
        "base_logical_clock_ms": 0,
        "logical_clock_step_ms": 10,
        "max_threads": 16,
        "cost_model": json.loads(json.dumps(v1["cost_model"])),
        "jobs": jobs_v2,
        "candidates": [
            {
                "candidate_id": "alpha-beta-gamma",
                "job_order": ["job-alpha", "job-beta", "job-gamma"],
                "interleave_jobs": False,
            },
            {
                "candidate_id": "reverse-with-interleave",
                "job_order": ["job-gamma", "job-alpha", "job-beta"],
                "interleave_jobs": True,
            },
        ],
        "critical_paths": {
            "path-alpha-beta": ["job-alpha", "job-beta"],
            "path-gamma": ["job-gamma"],
        },
    }
    return v1, v2


def _stats(seed: int) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    clocks = [0, 20, 40, 60, 80, 100, 120, 140, 160, 180, 200, 220, 240]
    for clock_index, clock in enumerate(clocks):
        for layer_index, layer in enumerate(CACHE_LAYERS):
            material = f"{seed}:{clock}:{layer}"
            raw = int(digest_obj(material)[:12], 16)
            hit_rate = round(0.55 + ((raw % 4000) / 10000.0), 4)
            occupancy = 2 + ((clock_index * 3 + layer_index * 2) % 13)
            throughput = round(0.90 + ((raw % 350) / 1000.0), 3)
            entries.append(
                {
                    "logical_clock_ms": clock,
                    "cache_layer": layer,
                    "hit_rate": min(0.99, hit_rate),
                    "thread_occupancy": occupancy,
                    "throughput_cost": throughput,
                }
            )
    return {"max_threads": 16, "entries": entries}


def _stats_changed(seed: int) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for clock in [620, 640, 660]:
        for layer_index, layer in enumerate(CACHE_LAYERS):
            raw = int(digest_obj(f"{seed}:change:{clock}:{layer}")[:12], 16)
            entries.append(
                {
                    "logical_clock_ms": clock,
                    "cache_layer": layer,
                    "hit_rate": round(0.50 + ((raw % 3000) / 10000.0), 4),
                    "thread_occupancy": 4 + ((clock + layer_index) % 10),
                    "throughput_cost": round(
                        0.95 + ((raw % 300) / 1000.0), 3
                    ),
                }
            )
    return {"max_threads": 16, "entries": entries}


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    )


def generate(out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    v1, v2 = _base_plan_dicts()
    files = {
        "tasks_v1.jsonl": _jsonl(_tasks_v1()),
        "tasks_revision.jsonl": _jsonl(_tasks_revision()),
        "plans_v1.json": stable_json_dumps(v1) + "\n",
        "plans_v2.json": stable_json_dumps(v2) + "\n",
        "platform_stats.json": stable_json_dumps(_stats(SAMPLE_SEED)) + "\n",
        "platform_stats_changed.json": stable_json_dumps(
            _stats_changed(SAMPLE_SEED)
        )
        + "\n",
    }
    manifest: dict[str, Any] = {
        "seed": SAMPLE_SEED,
        "generator": "scripts/generate_samples.py",
        "files": {},
    }
    for name, content in sorted(files.items()):
        path = out_dir / name
        path.write_text(content, encoding="utf-8")
        manifest["files"][name] = {
            "sha256": digest_text(content),
            "bytes": len(content.encode("utf-8")),
        }
    (out_dir / "manifest.json").write_text(
        stable_json_dumps(manifest) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    args = parser.parse_args()
    manifest = generate(Path(args.out))
    print(stable_json_dumps(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

