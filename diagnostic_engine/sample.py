"""Fixed-seed local sample manifest and noise parameter generation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .hashing import array_content_hash
from .models import ClipTask
from .noise import frame_noise


SAMPLE_RANDOM_SEED = 20261126


def sample_records() -> list[dict[str, Any]]:
    return [
        {
            "clip_id": "clip_alpha",
            "total_frames": 12,
            "frame_shape": [8, 8, 3],
            "noise_seed": 1001,
            "motion_mode": "translate",
            "block_size": 4,
            "propagation_steps": 2,
            "revision": 1,
        },
        {
            "clip_id": "clip_beta",
            "total_frames": 10,
            "frame_shape": [6, 10, 1],
            "noise_seed": 2002,
            "motion_mode": "ripple",
            "block_size": 3,
            "propagation_steps": 1,
            "revision": 1,
        },
        {
            "clip_id": "clip_gamma",
            "total_frames": 7,
            "frame_shape": [5, 7],
            "noise_seed": 3003,
            "motion_mode": "diffuse",
            "block_size": 2,
            "propagation_steps": 3,
            "revision": 1,
        },
    ]


def sample_tasks() -> list[ClipTask]:
    return [ClipTask.from_dict(record) for record in sample_records()]


def generate_sample(output_dir: str | Path) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    records = sample_records()
    manifest_path = output / "tasks.jsonl"
    with manifest_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")

    revised_alpha = dict(records[0])
    revised_alpha["revision"] = 2
    revised_alpha["change_frame"] = 5
    revision_path = output / "revision_change.jsonl"
    with revision_path.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(revised_alpha, sort_keys=True, ensure_ascii=False) + "\n"
        )

    noise_params: dict[str, Any] = {
        "generator": "numpy.random.default_rng(np.random.SeedSequence([...]))",
        "sample_script_seed": SAMPLE_RANDOM_SEED,
        "frames": {},
    }
    initial_frames: dict[str, np.ndarray] = {}
    for record in records:
        task = ClipTask.from_dict(record)
        initial = frame_noise(task.clip_id, task.noise_seed, 0, task.frame_shape)
        initial_frames[task.clip_id] = initial
        noise_params["frames"][task.clip_id] = {
            "noise_seed": task.noise_seed,
            "frame_index": 0,
            "frame_shape": list(task.frame_shape),
            "dtype": str(initial.dtype),
            "content_sha256": array_content_hash(initial),
            "seed_rule": "SeedSequence([noise_seed, sha256(clip_id)[:8] mod 2^32, frame_index])",
        }

    np.savez_compressed(output / "initial_noise_frames.npz", **initial_frames)
    params_path = output / "initial_noise_params.json"
    with params_path.open("w", encoding="utf-8") as handle:
        json.dump(noise_params, handle, sort_keys=True, indent=2, ensure_ascii=False)
        handle.write("\n")

    return {
        "manifest": str(manifest_path),
        "revision_change_manifest": str(revision_path),
        "noise_params": str(params_path),
        "initial_noise_frames": str(output / "initial_noise_frames.npz"),
        "clip_count": len(records),
    }
