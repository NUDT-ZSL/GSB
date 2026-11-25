"""Deterministic per-case sample generation and explicit fixed-point quantization."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from .errors import ETaskInvalid
from .models import Task
from .util import sha256_obj


QUANTIZATION_CONTRACT = {
    "scale": "task.quant_scale",
    "zero_point": "task.quant_zero_point",
    "rounding": "half_away_from_zero",
    "clip": "dtype_min..dtype_max",
    "dequantize": "(q-zero_point)*scale",
    "accumulator_dtype": "float64",
}


def generate_sample(task: Task, global_seed: int) -> dict[str, Any]:
    """Generate x/y with a task-specific deterministic PCG64 stream."""

    generator = np.random.Generator(np.random.PCG64(task.sample_seed(global_seed)))
    features = generator.normal(loc=0.0, scale=1.0, size=(task.data_size, task.feature_dim)).astype(
        np.float64,
        copy=False,
    )
    label_ids = generator.integers(
        low=0,
        high=task.label_dim,
        size=task.data_size,
        dtype=np.int64,
    )
    labels = np.zeros((task.data_size, task.label_dim), dtype=np.float64)
    labels[np.arange(task.data_size), label_ids] = 1.0
    if features.shape != (task.data_size, task.feature_dim):
        raise ETaskInvalid("feature shape does not match task declaration", {"case_id": task.case_id})
    if labels.shape != (task.data_size, task.label_dim):
        raise ETaskInvalid("label shape does not match task declaration", {"case_id": task.case_id})

    digest = hashlib.sha256()
    digest.update(features.tobytes(order="C"))
    digest.update(labels.tobytes(order="C"))
    digest.update(f"{features.shape}|{features.dtype}|{labels.dtype}".encode("ascii"))
    data_digest = digest.hexdigest()
    summary = {
        "case_id": task.case_id,
        "task_content_hash": task.content_hash,
        "data_digest": data_digest,
        "shape": [task.data_size, task.feature_dim],
        "label_shape": [task.data_size, task.label_dim],
        "dtype": "float64",
        "feature_stats": {
            "mean": round(float(np.mean(features)), 12),
            "std": round(float(np.std(features)), 12),
            "min": round(float(np.min(features)), 12),
            "max": round(float(np.max(features)), 12),
        },
        "label_distribution_counts": {
            str(index): int(count)
            for index, count in enumerate(np.bincount(label_ids, minlength=task.label_dim))
        },
    }
    return {
        "features": features,
        "labels": labels,
        "label_ids": label_ids,
        "summary": summary,
        "data_digest": data_digest,
        "sample_seed": task.sample_seed(global_seed),
    }


def half_away_from_zero(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    return np.where(values >= 0.0, np.floor(values + 0.5), np.ceil(values - 0.5))


def quantize_dequantize(features: np.ndarray, task: Task) -> dict[str, Any]:
    info = np.iinfo(np.dtype(task.dtype))
    scaled = features / task.quant_scale
    rounded = half_away_from_zero(scaled)
    with_zero_point = rounded + float(task.quant_zero_point)
    clipped = np.clip(with_zero_point, info.min, info.max)
    quantized = clipped.astype(np.dtype(task.dtype), copy=False)
    dequantized = (quantized.astype(np.float64) - float(task.quant_zero_point)) * task.quant_scale
    error = dequantized.astype(np.float64) - features
    digest = hashlib.sha256()
    digest.update(quantized.tobytes(order="C"))
    digest.update(dequantized.tobytes(order="C"))
    digest.update(f"{task.dtype}|{task.quant_scale}|{task.quant_zero_point}".encode("utf-8"))
    summary = {
        "case_id": task.case_id,
        "quant_contract": dict(QUANTIZATION_CONTRACT),
        "quantized_dtype": task.dtype,
        "quantized_shape": list(quantized.shape),
        "quant_digest": digest.hexdigest(),
        "clip_fraction": round(float(np.mean(clipped != with_zero_point)), 12),
        "max_abs_error": round(float(np.max(np.abs(error))), 12),
        "mean_abs_error": round(float(np.mean(np.abs(error))), 12),
    }
    return {
        "quantized": quantized,
        "dequantized": dequantized,
        "summary": summary,
        "quant_digest": digest.hexdigest(),
    }


def independent_sample_summary(task: Task, global_seed: int) -> str:
    """Small helper used by diagnostics to prove content addressability."""

    return sha256_obj(
        {
            "case_id": task.case_id,
            "task_content_hash": task.content_hash,
            "seed": task.sample_seed(global_seed),
            "shape": [task.data_size, task.feature_dim],
        }
    )
