"""Deterministic sample generation, normalization, and fixed-point conversion."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import array_hash, sha256_json, stable_int
from .errors import BusinessError
from .models import Batch, Task


@dataclass(frozen=True)
class NormalizationResult:
    raw: np.ndarray
    normalized: np.ndarray
    mean: np.ndarray
    std: np.ndarray
    denominator: np.ndarray
    raw_hash: str
    normalized_hash: str
    stats_hash: str

    def stats_dict(self, task: Task, batch: Batch) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "batch_id": batch.batch_id,
            "sample_count": int(self.raw.shape[0]),
            "feature_dim": int(self.raw.shape[1]),
            "method": "zscore",
            "ddof": task.normalization_ddof,
            "epsilon": task.normalization_epsilon,
            "mean": [float(value) for value in self.mean],
            "std": [float(value) for value in self.std],
            "denominator": [float(value) for value in self.denominator],
            "raw_hash": self.raw_hash,
            "normalized_hash": self.normalized_hash,
            "stats_hash": self.stats_hash,
        }


@dataclass(frozen=True)
class QuantizationResult:
    quantized: np.ndarray
    dequantized: np.ndarray
    quantized_hash: str
    dequantized_hash: str


def generate_samples(task: Task, batch: Batch) -> np.ndarray:
    """Generate a deterministic float64 matrix from task id and batch seed.

    The batch-provided feature_seed is bound to task_id, so equal seeds on two
    tasks do not accidentally share a matrix. NumPy's PCG64 stream and the
    float64 layout are stable under the pinned NumPy 2.0.0 runtime.
    """

    namespace = stable_int(task.task_id)
    seed_sequence = np.random.SeedSequence([namespace, int(batch.feature_seed)])
    rng = np.random.Generator(np.random.PCG64(seed_sequence))
    feature_loc = rng.normal(0.0, 1.5, size=task.feature_dim)
    feature_scale = rng.uniform(0.35, 2.75, size=task.feature_dim)
    noise = rng.standard_normal(size=(batch.sample_count, task.feature_dim), dtype=np.float64)
    matrix = feature_loc + feature_scale * noise
    matrix = np.ascontiguousarray(matrix, dtype=np.float64)
    if not np.all(np.isfinite(matrix)):
        raise BusinessError(
            "E_BAD_BATCH",
            "generated sample matrix contains non-finite values",
            task_id=task.task_id,
            batch_id=batch.batch_id,
        )
    return matrix


def _float64_list(values: list[float]) -> str:
    return sha256_json([float(item) for item in values])


def normalize_zscore(
    matrix: np.ndarray,
    *,
    ddof: int,
    epsilon: float,
    coordinate_dimension: int,
) -> NormalizationResult:
    raw = np.ascontiguousarray(matrix, dtype=np.float64)
    if raw.ndim != 2 or raw.shape[1] != coordinate_dimension:
        raise BusinessError(
            "E_QUANT_LAYER",
            "sample matrix dimension does not match task feature_dim",
            context={"shape": raw.shape, "feature_dim": coordinate_dimension},
        )
    if raw.shape[0] <= ddof:
        raise BusinessError(
            "E_PARAM_INVALID",
            "sample_count is too small for requested ddof",
            context={"sample_count": int(raw.shape[0]), "ddof": ddof},
        )
    if not np.all(np.isfinite(raw)):
        raise BusinessError(
            "E_QUANT_INPUT",
            "normalization input contains a non-finite sample",
            context={"nonfinite_count": int(np.size(raw) - np.isfinite(raw).sum())},
        )

    sample_count, feature_dim = raw.shape
    mean_values: list[float] = []
    std_values: list[float] = []
    denominator_values: list[float] = []
    for feature_index in range(feature_dim):
        column = np.asarray(raw[:, feature_index], dtype=np.float64)
        mean_value = math.fsum(float(value) for value in column) / sample_count
        deviations = column - mean_value
        squared = np.asarray(deviations * deviations, dtype=np.float64)
        variance_denominator = sample_count - ddof
        variance = math.fsum(float(value) for value in squared) / variance_denominator
        std_value = math.sqrt(max(variance, 0.0))
        denominator = max(std_value, float(epsilon))
        mean_values.append(float(mean_value))
        std_values.append(float(std_value))
        denominator_values.append(float(denominator))

    mean_array = np.asarray(mean_values, dtype=np.float64)
    denominator_array = np.asarray(denominator_values, dtype=np.float64)
    normalized = (raw - mean_array) / denominator_array
    normalized = np.ascontiguousarray(normalized, dtype=np.float64)
    std_array = np.asarray(std_values, dtype=np.float64)
    stats_hash = sha256_json(
        {
            "mean": mean_values,
            "std": std_values,
            "denominator": denominator_values,
            "ddof": ddof,
            "epsilon": float(epsilon),
            "sample_count": sample_count,
            "feature_dim": feature_dim,
        }
    )
    return NormalizationResult(
        raw=raw,
        normalized=normalized,
        mean=mean_array,
        std=std_array,
        denominator=denominator_array,
        raw_hash=array_hash(raw),
        normalized_hash=array_hash(normalized),
        stats_hash=stats_hash,
    )


def round_half_away_from_zero(values: np.ndarray) -> np.ndarray:
    positive = np.floor(values + 0.5)
    negative = np.ceil(values - 0.5)
    return np.where(values >= 0.0, positive, negative)


def quantize(
    normalized: np.ndarray,
    *,
    scale: float,
    zero_point: int,
    rounding_mode: str,
    expected_shape: tuple[int, int] | None = None,
    task_id: str | None = None,
    batch_id: str | None = None,
) -> QuantizationResult:
    values = np.asarray(normalized, dtype=np.float64)
    if expected_shape is not None and values.shape != expected_shape:
        raise BusinessError(
            "E_QUANT_LAYER",
            "quantization input dimension does not match task declaration",
            stage="quantize",
            task_id=task_id,
            batch_id=batch_id,
            context={"shape": list(values.shape), "expected_shape": list(expected_shape)},
        )
    nonfinite = ~np.isfinite(values)
    if np.any(nonfinite):
        first_flat = int(np.flatnonzero(nonfinite)[0])
        first_position = [int(item) for item in np.unravel_index(first_flat, values.shape)]
        raise BusinessError(
            "E_QUANT_INPUT",
            "quantization input contains non-finite values",
            stage="quantize",
            task_id=task_id,
            batch_id=batch_id,
            context={
                "nonfinite_count": int(nonfinite.sum()),
                "first_position": first_position,
            },
        )
    if not math.isfinite(scale) or scale <= 0:
        raise BusinessError(
            "E_PARAM_INVALID",
            "quant_scale must be positive",
            stage="quantize",
            task_id=task_id,
            batch_id=batch_id,
        )
    scaled = values / float(scale)
    if rounding_mode == "half_away_from_zero":
        rounded = round_half_away_from_zero(scaled)
    elif rounding_mode == "half_to_even":
        rounded = np.rint(scaled)
    else:
        raise BusinessError(
            "E_PARAM_INVALID",
            "unsupported rounding mode",
            stage="quantize",
            task_id=task_id,
            batch_id=batch_id,
            context={"rounding_mode": rounding_mode},
        )
    if not np.all(np.isfinite(rounded)):
        raise BusinessError(
            "E_QUANT_LAYER",
            "rounding produced non-finite quantized coordinates",
            stage="quantize",
            task_id=task_id,
            batch_id=batch_id,
        )
    limits = np.iinfo(np.int64)
    if np.any(rounded < float(limits.min)) or np.any(rounded > float(limits.max - 1)):
        raise BusinessError(
            "E_QUANT_LAYER",
            "quantized coordinate overflows int64",
            stage="quantize",
            task_id=task_id,
            batch_id=batch_id,
        )
    quantized = rounded.astype(np.int64) + np.int64(zero_point)
    if quantized.shape != values.shape:
        raise BusinessError(
            "E_QUANT_LAYER",
            "quantized output dimension changed",
            stage="quantize",
            task_id=task_id,
            batch_id=batch_id,
            context={"input_shape": list(values.shape), "output_shape": list(quantized.shape)},
        )
    dequantized = (quantized.astype(np.float64) - float(zero_point)) * float(scale)
    dequantized = np.ascontiguousarray(dequantized, dtype=np.float64)
    if dequantized.shape != values.shape:
        raise BusinessError(
            "E_QUANT_LAYER",
            "dequantized output dimension changed",
            stage="quantize",
            task_id=task_id,
            batch_id=batch_id,
            context={"input_shape": list(values.shape), "output_shape": list(dequantized.shape)},
        )
    return QuantizationResult(
        quantized=quantized,
        dequantized=dequantized,
        quantized_hash=array_hash(quantized),
        dequantized_hash=array_hash(dequantized),
    )


def dequantize(
    quantized: np.ndarray,
    *,
    scale: float,
    zero_point: int,
    expected_shape: tuple[int, int] | None = None,
    task_id: str | None = None,
    batch_id: str | None = None,
) -> np.ndarray:
    values = np.asarray(quantized)
    if expected_shape is not None and values.shape != expected_shape:
        raise BusinessError(
            "E_QUANT_LAYER",
            "dequantization input dimension does not match task declaration",
            stage="quantize",
            task_id=task_id,
            batch_id=batch_id,
            context={"shape": list(values.shape), "expected_shape": list(expected_shape)},
        )
    result = (values.astype(np.float64) - float(zero_point)) * float(scale)
    result = np.ascontiguousarray(result, dtype=np.float64)
    if result.shape != values.shape:
        raise BusinessError(
            "E_QUANT_LAYER",
            "dequantized output dimension changed",
            stage="quantize",
            task_id=task_id,
            batch_id=batch_id,
            context={"input_shape": list(values.shape), "output_shape": list(result.shape)},
        )
    return result
