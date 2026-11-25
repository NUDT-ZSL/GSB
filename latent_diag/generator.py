from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from .config import DEFAULT_VOCABULARY
from .hashing import canonical_json_bytes, hash_array, hash_json
from .pipeline import initial_noise
from .task import TaskSpec


FIXED_GENERATOR_SEED = 20261126


def _sample_specs() -> list[dict[str, Any]]:
    return [
        {
            "task_id": "t001_forest_f64",
            "prompt": "Quiet forest with red fox in morning light",
            "prompt_seed": 1001,
            "latent_shape": [4, 24, 32],
            "block_grid": [2, 2],
            "sampler_steps": 5,
            "noise_scale": 0.55,
            "quant_scale": 1024,
            "rounding_mode": "nearest_even",
            "dtype_profile": "float64",
            "revision": 1,
            "depends_on": [],
        },
        {
            "task_id": "t002_city_f32",
            "prompt": "Blue city street at night with silver light",
            "prompt_seed": 1002,
            "latent_shape": [4, 32, 32],
            "block_grid": [2, 4],
            "sampler_steps": 4,
            "noise_scale": 0.65,
            "quant_scale": 1024,
            "rounding_mode": "nearest_even",
            "dtype_profile": "float32",
            "revision": 1,
            "depends_on": ["t001_forest_f64"],
        },
        {
            "task_id": "t003_ocean_fixed",
            "prompt": "Painted ocean and white boat in soft evening light",
            "prompt_seed": 1003,
            "latent_shape": [4, 24, 40],
            "block_grid": [2, 4],
            "sampler_steps": 5,
            "noise_scale": 0.5,
            "quant_scale": 2048,
            "rounding_mode": "nearest_even",
            "dtype_profile": "fixed",
            "revision": 1,
            "depends_on": [],
        },
        {
            "task_id": "t004_robot_f64",
            "prompt": "Detailed silver robot in a quiet garden",
            "prompt_seed": 1004,
            "latent_shape": [4, 16, 24],
            "block_grid": [2, 2],
            "sampler_steps": 4,
            "noise_scale": 0.6,
            "quant_scale": 1024,
            "rounding_mode": "nearest_even",
            "dtype_profile": "float64",
            "revision": 1,
            "depends_on": ["t002_city_f32"],
        },
        {
            "task_id": "t005_desert_f32",
            "prompt": "Orange desert tower under a bright sun",
            "prompt_seed": 1005,
            "latent_shape": [4, 24, 32],
            "block_grid": [4, 2],
            "sampler_steps": 4,
            "noise_scale": 0.45,
            "quant_scale": 1024,
            "rounding_mode": "nearest_even",
            "dtype_profile": "float32",
            "revision": 1,
            "depends_on": [],
        },
        {
            "task_id": "t006_snow_fixed",
            "prompt": "Minimal snow mountain with white cloud and black stone",
            "prompt_seed": 1006,
            "latent_shape": [4, 32, 24],
            "block_grid": [4, 2],
            "sampler_steps": 4,
            "noise_scale": 0.58,
            "quant_scale": 2048,
            "rounding_mode": "nearest_even",
            "dtype_profile": "fixed",
            "revision": 1,
            "depends_on": [],
        },
    ]


def _base_config() -> dict[str, Any]:
    vocabulary = list(dict.fromkeys(DEFAULT_VOCABULARY))
    return {
        "schema_version": 1,
        "condition_dim": 64,
        "vocabulary": vocabulary,
        "phase": {"amplitude": 0.0025},
        "denoise": {
            "base_decay": 0.95,
            "boundary_mix": 0.08,
            "bias_scale": 0.01,
        },
        "decode": {"contrast": 0.55},
        "budget": {
            "max_total_denoise_iterations": 500,
            "max_resident_latent_bytes": 32 * 1024 * 1024,
            "worker_count": 2,
            "tasks_per_batch": 2,
            "priority": ["iterations", "memory"],
        },
        "tolerance": {
            "max_abs": 0.02,
            "max_rel": 0.02,
            "max_amplification": 1000.0,
        },
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            canonical_json_bytes(row).decode("utf-8") + "\n" for row in rows
        ),
        encoding="utf-8",
    )


def generate_samples(
    output_dir: str | Path,
    *,
    seed: int = FIXED_GENERATOR_SEED,
) -> dict[str, Any]:
    out = Path(output_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    latents_dir = out / "latents"
    latents_dir.mkdir()
    config = _base_config()
    _write_json(out / "config.json", config)

    specs = _sample_specs()
    rows: list[dict[str, Any]] = []
    latent_hashes: dict[str, str] = {}
    for spec in specs:
        latent_name = f"{spec['task_id']}.npy"
        relative_latent = f"latents/{latent_name}"
        row = {**spec, "latent_file": relative_latent}
        task = TaskSpec.from_dict(
            {key: value for key, value in row.items() if key != "latent_file"}
        )
        latent = initial_noise(task)
        np.save(latents_dir / latent_name, latent, allow_pickle=False)
        latent_hashes[spec["task_id"]] = hash_array(latent)
        rows.append(row)
    _write_jsonl(out / "tasks.jsonl", rows)

    changed_rows = [json.loads(json.dumps(row)) for row in rows]
    for row in changed_rows:
        if row["task_id"] == "t002_city_f32":
            row["dtype_profile"] = "float64"
            row["revision"] = 2
    _write_jsonl(out / "tasks_v2_dtype_change.jsonl", changed_rows)

    iteration_config = json.loads(json.dumps(config))
    iteration_config["budget"]["max_total_denoise_iterations"] = 50
    _write_json(out / "config_budget_iter.json", iteration_config)

    memory_config = json.loads(json.dumps(config))
    memory_config["budget"]["max_resident_latent_bytes"] = 700_000
    memory_config["budget"]["tasks_per_batch"] = 2
    _write_json(out / "config_budget_memory.json", memory_config)

    tolerance_config = json.loads(json.dumps(config))
    tolerance_config["tolerance"] = {
        "max_abs": 1e-10,
        "max_rel": 1e-10,
        "max_amplification": 1e12,
    }
    _write_json(out / "config_tolerance_tight.json", tolerance_config)

    metadata = {
        "generator_seed": seed,
        "task_count": len(rows),
        "config_hash": hash_json(config),
        "tasks_hash": hash_json(rows),
        "latent_hashes": latent_hashes,
        "note": "Latent files are canonical float64 arrays generated by numpy.PCG64.",
    }
    _write_json(out / "sample_metadata.json", metadata)
    return metadata

