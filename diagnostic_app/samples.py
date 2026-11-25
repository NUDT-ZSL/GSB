from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import numpy as np

from .config import DEFAULT_CONFIG
from .hashing import sha256_json
from .io_utils import write_json, write_jsonl, write_pnm
from .metadata import flatten_v1_record, write_v1, write_v2
from .pipeline import build_image_pixels


SAMPLE_SEED = 20261126


def sample_tasks() -> list[dict[str, Any]]:
    return [
        {
            "task_id": "tdc-001",
            "image_seed": 410011,
            "image_shape": [36, 48, 3],
            "text": "crimson blocks and grid lattice over a sharp city skyline",
            "prompt": "绯红块面与网格晶格，锐利的城市天际线",
            "sampler_steps": 4,
            "noise_scale": 0.16,
            "precision_profile": "float32_deterministic",
            "effective_second": 3,
        },
        {
            "task_id": "tdc-002",
            "image_seed": 410029,
            "image_shape": [48, 64, 3],
            "text": "azure waves and concentric lattice near the ocean forest",
            "prompt": "湛蓝波浪与同心晶格，海洋森林，柔和发光",
            "sampler_steps": 5,
            "noise_scale": 0.14,
            "precision_profile": "float32_deterministic",
            "effective_second": 8,
        },
        {
            "task_id": "tdc-003",
            "image_seed": 410047,
            "image_shape": [32, 32, 3],
            "text": "emerald diagonal blocks in a muted desert studio",
            "prompt": "翠绿对角块面，低饱和沙漠摄影棚",
            "sampler_steps": 6,
            "noise_scale": 0.18,
            "precision_profile": "float64_diagnostic",
            "effective_second": 14,
        },
    ]


def sample_config() -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["sample_seed"] = SAMPLE_SEED
    return config


def generate_all(root: str | Path = "samples") -> dict[str, Any]:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    tasks = sample_tasks()
    task_rows = [{key: value for key, value in task.items() if key != "effective_second"} for task in tasks]
    tasks_path = write_jsonl(root / "tasks.jsonl", task_rows)

    config = sample_config()
    config_path = write_json(root / "generation_config.json", config)

    strict_latency = copy.deepcopy(config)
    strict_latency["latency_budget_ms"] = 0.35
    strict_latency_path = write_json(root / "config.strict_latency.json", strict_latency)

    tiny_resources = copy.deepcopy(config)
    tiny_resources["resource_limits"]["max_total_pixels"] = 128
    tiny_resources["resource_limits"]["max_parallel_tasks"] = 1
    tiny_resource_path = write_json(root / "config.tiny_resources.json", tiny_resources)

    reference_dir = root / "references"
    reference_dir.mkdir(exist_ok=True)
    references: dict[str, str] = {}
    for task in tasks:
        reference_task = copy.deepcopy(task)
        reference_task["image_seed"] = int(task["image_seed"]) + 700_000
        reference_pixels = build_image_pixels(reference_task, np.dtype(np.float32))
        npy_path = reference_dir / f"{task['task_id']}.npy"
        np.save(npy_path, reference_pixels.astype(np.float32))
        u8 = np.rint(reference_pixels * 255.0).astype(np.uint8)
        image_path = write_pnm(reference_dir / f"{task['task_id']}.ppm", u8)
        references[task["task_id"]] = str(npy_path)

    v1_records = [flatten_v1_record(task, int(task["effective_second"])) for task in tasks]
    v1_manifest = write_v1(v1_records, root / "descriptions" / "v1" / "task_descriptions_v1.json")
    v2_manifest = write_v2(
        v1_records,
        root / "descriptions" / "v2" / "shards",
        root / "descriptions" / "v2" / "metadata.sqlite",
        shard_size=2,
    )

    manifest = {
        "sample_seed": SAMPLE_SEED,
        "tasks_jsonl": str(tasks_path),
        "config_path": str(config_path),
        "strict_latency_config_path": str(strict_latency_path),
        "tiny_resource_config_path": str(tiny_resource_path),
        "references": references,
        "v1": v1_manifest,
        "v2": v2_manifest,
        "task_count": len(tasks),
        "manifest_checksum": sha256_json({"tasks": tasks, "config": config}),
    }
    write_json(root / "manifest.json", manifest)
    return manifest
