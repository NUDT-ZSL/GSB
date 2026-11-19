"""Fixed-seed local demo dataset and tensor artifact generation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import atomic_write_bytes, atomic_write_json, canonical_json_bytes
from .canonical import content_checksum
from .compute import generate_features, generate_model, generate_targets
from .models import PipelineConfig, Task, manifest_checksum


DEMO_CONFIG: dict[str, Any] = {
    "max_slots": 3,
    "compute_budget": 20_000_000,
    "memory_budget_mb": 8.0,
    "bytes_per_scalar": 8,
    "activation_copies": 3,
    "gradient_buffer_copies": 1,
    "learning_rate": 0.03,
    "degradation_enabled": True,
    "degradation_priority_floor": 0,
    "min_batch_size": 4,
    "min_iterations": 2,
    "max_degrade_candidates": 24,
    "equivalence_atol": 0.0,
    "equivalence_rtol": 0.0,
}


DEMO_JOBS: list[dict[str, Any]] = [
    {
        "job_id": "job-001",
        "job_kind": "train",
        "model_tag": "alpha",
        "batch_size": 4,
        "seq_len": 8,
        "steps": 3,
        "feature_dim": 8,
        "seed": 11,
        "priority": 90,
    },
    {
        "job_id": "job-002",
        "job_kind": "infer",
        "model_tag": "alpha",
        "batch_size": 4,
        "seq_len": 8,
        "warmup": 3,
        "feature_dim": 8,
        "seed": 12,
        "priority": 90,
    },
    {
        "job_id": "job-003",
        "job_kind": "infer",
        "model_tag": "alpha",
        "batch_size": 6,
        "seq_len": 8,
        "warmup": 2,
        "feature_dim": 8,
        "seed": 13,
        "priority": 80,
    },
    {
        "job_id": "job-004",
        "job_kind": "train",
        "model_tag": "beta",
        "batch_size": 4,
        "seq_len": 8,
        "steps": 2,
        "feature_dim": 8,
        "seed": 14,
        "priority": 70,
    },
    {
        "job_id": "job-005",
        "job_kind": "infer",
        "model_tag": "beta",
        "batch_size": 4,
        "seq_len": 8,
        "warmup": 2,
        "feature_dim": 8,
        "seed": 15,
        "priority": 70,
    },
    {
        "job_id": "job-006",
        "job_kind": "infer",
        "model_tag": "delta",
        "batch_size": 32,
        "seq_len": 32,
        "warmup": 8,
        "feature_dim": 32,
        "seed": 17,
        "priority": 50,
        "fault": "score",
    },
    {
        "job_id": "job-007",
        "job_kind": "train",
        "model_tag": "gamma",
        "batch_size": 8,
        "seq_len": 16,
        "steps": 4,
        "feature_dim": 16,
        "seed": 18,
        "priority": 40,
    },
    {
        "job_id": "job-008",
        "job_kind": "train",
        "model_tag": "omega",
        "batch_size": 64,
        "seq_len": 64,
        "steps": 12,
        "feature_dim": 64,
        "seed": 19,
        "priority": 10,
    },
]


def demo_tasks() -> list[Task]:
    return [Task.from_dict(job) for job in DEMO_JOBS]


def demo_config() -> PipelineConfig:
    return PipelineConfig.from_dict(DEMO_CONFIG)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def generate_dataset(root: str | Path, *, force: bool = True) -> dict[str, Any]:
    root_path = Path(root)
    data_dir = root_path / "data"
    config_dir = root_path / "configs"
    tensor_dir = data_dir / "tensors"
    feature_dir = tensor_dir / "features"
    target_dir = tensor_dir / "targets"
    weight_dir = tensor_dir / "weights"
    bias_dir = tensor_dir / "bias"
    for directory in (
        data_dir,
        config_dir,
        feature_dir,
        target_dir,
        weight_dir,
        bias_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    manifest_path = data_dir / "jobs.jsonl"
    config_path = config_dir / "demo.json"
    tensor_manifest_path = tensor_dir / "manifest.json"
    if (
        not force
        and manifest_path.exists()
        and config_path.exists()
        and tensor_manifest_path.exists()
    ):
        return {
            "manifest": str(manifest_path),
            "config": str(config_path),
            "tensor_dir": str(tensor_dir),
            "reused": True,
        }

    tasks = demo_tasks()
    manifest_lines = [
        canonical_json_bytes(task.to_dict()) for task in sorted(tasks, key=lambda t: t.job_id)
    ]
    atomic_write_bytes(manifest_path, b"\n".join(manifest_lines) + b"\n")
    atomic_write_json(config_path, DEMO_CONFIG)

    artifact_records: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for task in tasks:
        features = generate_features(task)
        targets = generate_targets(task)
        feature_name = _safe_name(task.job_id) + ".npy"
        target_name = _safe_name(task.job_id) + ".npy"
        np.save(feature_dir / feature_name, features, allow_pickle=False)
        np.save(target_dir / target_name, targets, allow_pickle=False)
        artifact_records.append(
            {
                "kind": "features",
                "job_id": task.job_id,
                "path": f"features/{feature_name}",
                "shape": list(features.shape),
                "dtype": str(features.dtype),
                "array_checksum": content_checksum(features.tolist()),
            }
        )
        artifact_records.append(
            {
                "kind": "targets",
                "job_id": task.job_id,
                "path": f"targets/{target_name}",
                "shape": list(targets.shape),
                "dtype": str(targets.dtype),
                "array_checksum": content_checksum(targets.tolist()),
            }
        )
        if task.model_tag not in seen_models:
            seen_models.add(task.model_tag)
            weights, bias = generate_model(task.model_tag, task.feature_dim)
            weight_name = _safe_name(task.model_tag) + ".npy"
            bias_name = _safe_name(task.model_tag) + ".npy"
            np.save(weight_dir / weight_name, weights, allow_pickle=False)
            np.save(bias_dir / bias_name, bias, allow_pickle=False)
            artifact_records.append(
                {
                    "kind": "weights",
                    "model_tag": task.model_tag,
                    "path": f"weights/{weight_name}",
                    "shape": list(weights.shape),
                    "dtype": str(weights.dtype),
                    "array_checksum": content_checksum(weights.tolist()),
                }
            )
            artifact_records.append(
                {
                    "kind": "bias",
                    "model_tag": task.model_tag,
                    "path": f"bias/{bias_name}",
                    "shape": list(bias.shape),
                    "dtype": str(bias.dtype),
                    "array_checksum": content_checksum(bias.tolist()),
                }
            )

    tensor_manifest = {
        "schema_version": 1,
        "generator": "mixedpipe.samples.generate_dataset",
        "manifest_checksum": manifest_checksum(tasks),
        "artifacts": sorted(
            artifact_records,
            key=lambda record: (
                record["kind"],
                record.get("job_id", ""),
                record.get("model_tag", ""),
            ),
        ),
    }
    atomic_write_json(tensor_manifest_path, tensor_manifest)
    return {
        "manifest": str(manifest_path),
        "config": str(config_path),
        "tensor_dir": str(tensor_dir),
        "tensor_manifest": str(tensor_manifest_path),
        "manifest_checksum": manifest_checksum(tasks),
        "artifact_count": len(artifact_records),
        "reused": False,
    }


def ensure_demo(root: str | Path) -> dict[str, Any]:
    return generate_dataset(root, force=False)
