from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import numpy as np

from .arrayio import BlobStore
from .config import EffectiveConfig
from .errors import EngineError
from .hashing import hash_array, hash_json
from .mathops import Float64Ops, Ops, make_ops
from .task import TaskSpec


TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)
ENGINE_VERSION = "ldiag-pipeline-v1"


def normalize_prompt(prompt: str) -> str:
    return " ".join(prompt.lower().split())


def tokenize_prompt(prompt: str) -> list[str]:
    return TOKEN_PATTERN.findall(normalize_prompt(prompt))


def _basis_vector(token: str, dimension: int) -> np.ndarray:
    values: list[float] = []
    counter = 0
    prefix = f"{ENGINE_VERSION}|embedding|{dimension}|{token}|".encode("utf-8")
    while len(values) < dimension:
        digest = hashlib.sha256(prefix + str(counter).encode("ascii")).digest()
        values.extend((byte - 127.5) / 127.5 for byte in digest)
        counter += 1
    vector = np.asarray(values[:dimension], dtype=np.float64)
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        vector[0] = 1.0
        return vector
    return vector / norm


def condition_embedding(prompt: str, config: EffectiveConfig) -> np.ndarray:
    vocab = [str(v).lower() for v in config.vocabulary]
    vocab_index = {token: index for index, token in enumerate(vocab)}
    tokens = tokenize_prompt(prompt)
    if not tokens:
        tokens = ["<unk>"]
    vector = np.zeros(config.condition_dim, dtype=np.float64)
    weighted: list[tuple[str, float]] = [("<bos>", 0.35)]
    for position, token in enumerate(tokens):
        canonical = token if token in vocab_index else "<unk>"
        weighted.append((canonical, 1.0 / np.sqrt(position + 1.0)))
    weighted.append(("<eos>", 0.35))
    for token, weight in weighted:
        vector += _basis_vector(token, config.condition_dim) * float(weight)
    norm = float(np.linalg.norm(vector))
    if norm > 0.0:
        vector /= norm
    return np.ascontiguousarray(vector, dtype=np.float64)


def condition_cache_key(task: TaskSpec, config: EffectiveConfig) -> str:
    return hash_json(
        {
            "engine": ENGINE_VERSION,
            "kind": "condition",
            "prompt": normalize_prompt(task.prompt),
            "condition_dim": config.condition_dim,
            "vocabulary": [str(v).lower() for v in config.vocabulary],
        }
    )


def _deterministic_noise(seed: int, shape: tuple[int, int, int], scale: float) -> np.ndarray:
    generator = np.random.Generator(np.random.PCG64(int(seed)))
    values = generator.standard_normal(shape, dtype=np.float64)
    return np.ascontiguousarray(values * float(scale), dtype=np.float64)


def initial_noise(task: TaskSpec) -> np.ndarray:
    expected = _deterministic_noise(task.prompt_seed, task.latent_shape, task.noise_scale)
    if task.latent_file is not None:
        source = Path(task.latent_file)
        if not source.exists():
            raise EngineError(
                "E_LATENT_INPUT_NOT_FOUND",
                f"latent file not found: {source}",
                task_id=task.task_id,
                details={"latent_file": str(source)},
            )
        try:
            supplied = np.load(source, allow_pickle=False)
        except (ValueError, OSError) as exc:
            raise EngineError(
                "E_LATENT_INPUT_INVALID",
                f"cannot read latent file: {source}",
                task_id=task.task_id,
            ) from exc
        if tuple(supplied.shape) != task.latent_shape:
            raise EngineError(
                "E_LATENT_SHAPE_MISMATCH",
                "latent file shape does not match latent_shape",
                task_id=task.task_id,
                details={
                    "latent_shape": list(task.latent_shape),
                    "noise_shape": list(supplied.shape),
                },
            )
        if hash_array(supplied) != hash_array(expected):
            raise EngineError(
                "E_LATENT_INPUT_MISMATCH",
                "latent file content disagrees with deterministic seed construction",
                task_id=task.task_id,
                details={
                    "latent_file": str(source),
                    "expected_hash": hash_array(expected),
                    "actual_hash": hash_array(supplied),
                },
            )
    return expected


def noise_cache_key(task: TaskSpec) -> str:
    return hash_json(
        {
            "engine": ENGINE_VERSION,
            "kind": "initial_noise",
            "prompt_seed": task.prompt_seed,
            "latent_shape": list(task.latent_shape),
            "noise_scale": task.noise_scale,
            "generator": "numpy.PCG64.standard_normal.float64",
        }
    )


def boundary_mask(shape: tuple[int, int, int], grid: tuple[int, int]) -> np.ndarray:
    _, height, width = shape
    grid_h, grid_w = grid
    block_h = height // grid_h
    block_w = width // grid_w
    mask = np.zeros((1, height, width), dtype=np.bool_)
    for block_i in range(grid_h):
        y0 = block_i * block_h
        y1 = y0 + block_h
        for block_j in range(grid_w):
            x0 = block_j * block_w
            x1 = x0 + block_w
            mask[:, y0:y1, x0] = True
            mask[:, y0:y1, x1 - 1] = True
            mask[:, y0, x0:x1] = True
            mask[:, y1 - 1, x0:x1] = True
    return mask


def block_schedule(task: TaskSpec) -> list[dict[str, int]]:
    grid_h, grid_w = task.block_grid
    block_h = task.height // grid_h
    block_w = task.width // grid_w
    schedule: list[dict[str, int]] = []
    for block_i in range(grid_h):
        for block_j in range(grid_w):
            schedule.append(
                {
                    "block_index": block_i * grid_w + block_j,
                    "block_row": block_i,
                    "block_col": block_j,
                    "y0": block_i * block_h,
                    "y1": (block_i + 1) * block_h,
                    "x0": block_j * block_w,
                    "x1": (block_j + 1) * block_w,
                }
            )
    return schedule


def _blocks(array: np.ndarray, grid: tuple[int, int]) -> list[np.ndarray]:
    _, height, width = array.shape
    grid_h, grid_w = grid
    block_h = height // grid_h
    block_w = width // grid_w
    result: list[np.ndarray] = []
    for block_i in range(grid_h):
        y0 = block_i * block_h
        y1 = y0 + block_h
        for block_j in range(grid_w):
            x0 = block_j * block_w
            x1 = x0 + block_w
            result.append(np.ascontiguousarray(array[:, y0:y1, x0:x1]))
    return result


def _assemble_blocks(blocks: list[np.ndarray], grid: tuple[int, int]) -> np.ndarray:
    grid_h, grid_w = grid
    _, block_h, block_w = blocks[0].shape
    channels = blocks[0].shape[0]
    assembled = np.empty(
        (channels, grid_h * block_h, grid_w * block_w),
        dtype=blocks[0].dtype,
    )
    cursor = 0
    for block_i in range(grid_h):
        y0 = block_i * block_h
        y1 = y0 + block_h
        for block_j in range(grid_w):
            x0 = block_j * block_w
            x1 = x0 + block_w
            assembled[:, y0:y1, x0:x1] = blocks[cursor]
            cursor += 1
    return np.ascontiguousarray(assembled)


def _phase_field(
    task: TaskSpec,
    condition: np.ndarray,
    step: int,
    config: EffectiveConfig,
) -> np.ndarray:
    height = task.height
    width = task.width
    rows = np.arange(height, dtype=np.float64)[:, None] + 1.0
    cols = np.arange(width, dtype=np.float64)[None, :] + 1.0
    c0 = float(np.mean(condition[: max(1, condition.size // 4)]))
    c1 = float(np.mean(condition[condition.size // 2 :]))
    phase = np.sin(rows * 0.11 + step * 0.17 + c0) * np.cos(
        cols * 0.07 - step * 0.13 + c1
    )
    return np.ascontiguousarray(
        phase[np.newaxis, :, :] * float(config.phase["amplitude"]),
        dtype=np.float64,
    )


def _raw_block(
    block: np.ndarray,
    ops: Ops,
    decay: float,
    bias: float,
    phase: np.ndarray,
) -> np.ndarray:
    scaled = ops.mul(block, decay)
    with_bias = ops.add(scaled, ops.const(np.asarray(bias, dtype=np.float64)))
    return ops.add(with_bias, ops.const(phase))


def _stats_or_identity(
    ops: Ops,
    raw: np.ndarray,
) -> tuple[object, object, float, float]:
    mean, std, mean_float, std_float = ops.stats(raw)
    if abs(std_float) < 1e-15:
        std = ops.const(np.asarray(1.0, dtype=np.float64))
        std_float = 1.0
    return mean, std, mean_float, std_float


def _normalize_block(
    raw_block: np.ndarray,
    mask_block: np.ndarray,
    mean: object,
    std: object,
    mix: float,
    ops: Ops,
) -> np.ndarray:
    shifted = ops.sub(raw_block, np.asarray(mean))
    normalized = ops.div(shifted, np.asarray(std))
    keep = ops.const(np.where(mask_block, 1.0 - mix, 1.0))
    take = ops.const(np.where(mask_block, mix, 0.0))
    return ops.add(ops.mul(raw_block, keep), ops.mul(normalized, take))


def _denoise_blockwise(
    initial: np.ndarray,
    task: TaskSpec,
    condition: np.ndarray,
    config: EffectiveConfig,
    ops: Ops,
    mask: np.ndarray,
) -> tuple[np.ndarray, list[dict[str, Any]], list[np.ndarray]]:
    latent = ops.from_float64(initial)
    records: list[dict[str, Any]] = []
    step_arrays: list[np.ndarray] = []
    decay = float(config.denoise["base_decay"])
    bias = float(config.denoise["bias_scale"]) * float(np.mean(condition))
    mix = float(config.denoise["boundary_mix"])
    _, height, width = initial.shape
    grid_h, grid_w = task.block_grid
    block_h = height // grid_h
    block_w = width // grid_w

    for step in range(task.sampler_steps):
        phase = _phase_field(task, condition, step, config)
        latent_blocks = _blocks(latent, task.block_grid)
        raw_blocks: list[np.ndarray] = []
        cursor = 0
        for block_i in range(grid_h):
            y0 = block_i * block_h
            y1 = y0 + block_h
            for block_j in range(grid_w):
                x0 = block_j * block_w
                x1 = x0 + block_w
                raw_blocks.append(
                    _raw_block(
                        latent_blocks[cursor],
                        ops,
                        decay,
                        bias,
                        phase[:, y0:y1, x0:x1],
                    )
                )
                cursor += 1
        raw = _assemble_blocks(raw_blocks, task.block_grid)
        raw_hash = hash_array(raw)
        mean, std, mean_float, std_float = _stats_or_identity(ops, raw)
        normalized_blocks: list[np.ndarray] = []
        cursor = 0
        for block_i in range(grid_h):
            y0 = block_i * block_h
            y1 = y0 + block_h
            for block_j in range(grid_w):
                x0 = block_j * block_w
                x1 = x0 + block_w
                normalized_blocks.append(
                    _normalize_block(
                        raw_blocks[cursor],
                        mask[:, y0:y1, x0:x1],
                        mean,
                        std,
                        mix,
                        ops,
                    )
                )
                cursor += 1
        latent = _assemble_blocks(normalized_blocks, task.block_grid)
        step_arrays.append(latent.copy())
        records.append(
            {
                "step": step,
                "raw_hash": raw_hash,
                "candidate_hash": hash_array(latent),
                "mean": mean_float,
                "std": std_float,
                "stats_hash": hash_json(
                    {
                        "step": step,
                        "raw_hash": raw_hash,
                        "mean": mean_float,
                        "std": std_float,
                    }
                ),
            }
        )
    return latent, records, step_arrays


def _denoise_reference(
    initial: np.ndarray,
    task: TaskSpec,
    condition: np.ndarray,
    config: EffectiveConfig,
    mask: np.ndarray,
    ops: Ops | None = None,
) -> tuple[np.ndarray, list[dict[str, Any]], list[np.ndarray]]:
    ops = ops or Float64Ops()
    latent = ops.from_float64(initial)
    records: list[dict[str, Any]] = []
    step_arrays: list[np.ndarray] = []
    decay = float(config.denoise["base_decay"])
    bias = float(config.denoise["bias_scale"]) * float(np.mean(condition))
    mix = float(config.denoise["boundary_mix"])
    for step in range(task.sampler_steps):
        phase = _phase_field(task, condition, step, config)
        raw = _raw_block(latent, ops, decay, bias, phase)
        raw_hash = hash_array(raw)
        mean, std, mean_float, std_float = _stats_or_identity(ops, raw)
        latent = _normalize_block(raw, mask, mean, std, mix, ops)
        step_arrays.append(latent.copy())
        records.append(
            {
                "step": step,
                "raw_hash": raw_hash,
                "reference_hash": hash_array(latent),
                "mean": mean_float,
                "std": std_float,
                "stats_hash": hash_json(
                    {
                        "step": step,
                        "raw_hash": raw_hash,
                        "mean": mean_float,
                        "std": std_float,
                    }
                ),
            }
        )
    return latent, records, step_arrays


def decode_weights(channels: int) -> np.ndarray:
    weights = np.empty((3, channels), dtype=np.float64)
    for row in range(3):
        for channel in range(channels):
            weights[row, channel] = 0.5 + ((row * 7 + channel * 11) % 13) / 25.0
        weights[row] /= np.sum(weights[row])
    return weights


def decode_rgb(latent: np.ndarray, config: EffectiveConfig) -> tuple[np.ndarray, np.ndarray]:
    channels = latent.shape[0]
    weights = decode_weights(channels)
    channels_last = np.ascontiguousarray(latent.transpose(1, 2, 0), dtype=np.float64)
    projected = np.tensordot(channels_last, weights.T, axes=([2], [0]))
    base = 0.5 * (projected + 1.0)
    contrast = float(config.decode["contrast"])
    rgb_float = np.ascontiguousarray(
        np.clip(0.5 + contrast * (base - 0.5), 0.0, 1.0),
        dtype=np.float64,
    )
    rgb_u8 = np.ascontiguousarray(
        np.rint(rgb_float * 255.0).astype(np.uint8),
        dtype=np.uint8,
    )
    return rgb_float, rgb_u8


def _first_position(diff: np.ndarray, threshold: float) -> list[int] | None:
    locations = np.argwhere(diff > threshold)
    if locations.size:
        return [int(v) for v in locations[0]]
    maximum = float(np.max(diff)) if diff.size else 0.0
    if maximum > 0.0:
        location = np.unravel_index(int(np.argmax(diff)), diff.shape)
        return [int(v) for v in location]
    return None


def _block_boundary_metrics(
    diff: np.ndarray,
    mask: np.ndarray,
    task: TaskSpec,
    absolute_tolerance: float,
) -> list[dict[str, Any]]:
    grid_h, grid_w = task.block_grid
    block_h = task.height // grid_h
    block_w = task.width // grid_w
    records: list[dict[str, Any]] = []
    for block_i in range(grid_h):
        y0 = block_i * block_h
        y1 = y0 + block_h
        for block_j in range(grid_w):
            x0 = block_j * block_w
            x1 = x0 + block_w
            local_diff = diff[:, y0:y1, x0:x1]
            local_mask = np.broadcast_to(mask[:, y0:y1, x0:x1], local_diff.shape)
            values = local_diff[local_mask]
            max_abs = float(np.max(values)) if values.size else 0.0
            local_position = _first_position(local_diff, absolute_tolerance)
            global_position = None
            if local_position is not None:
                global_position = [
                    local_position[0],
                    local_position[1] + y0,
                    local_position[2] + x0,
                ]
            records.append(
                {
                    "block_index": block_i * grid_w + block_j,
                    "block_row": block_i,
                    "block_col": block_j,
                    "origin": [y0, x0],
                    "shape": [y1 - y0, x1 - x0],
                    "boundary_max_abs": max_abs,
                    "first_deviation": global_position,
                }
            )
    return records


def _artifact_descriptor(
    kind: str,
    cache_key: str,
    content_hash: str,
    path: str | None,
    *,
    storage: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "cache_key": cache_key,
        "content_hash": content_hash,
        "artifact_path": path,
        "storage": storage,
    }


def _put_array(
    blobs: BlobStore | None,
    kind: str,
    cache_key: str,
    array: np.ndarray,
) -> dict[str, Any]:
    content_hash = hash_array(array)
    path: str | None = None
    if blobs is not None:
        content_hash, path = blobs.put_array(array)
    return _artifact_descriptor(kind, cache_key, content_hash, path, storage="array")


def _put_json(
    blobs: BlobStore | None,
    kind: str,
    cache_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    content_hash = hash_json(payload)
    path: str | None = None
    if blobs is not None:
        content_hash, path = blobs.put_json(payload)
    return _artifact_descriptor(kind, cache_key, content_hash, path, storage="json")


def _load_reused_array(
    blobs: BlobStore | None,
    descriptor: dict[str, Any] | None,
    *,
    task_id: str,
) -> np.ndarray | None:
    if blobs is None or not descriptor:
        return None
    path = descriptor.get("artifact_path")
    expected = descriptor.get("content_hash")
    if not path or not expected:
        return None
    try:
        return blobs.load_array(path, expected)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise EngineError(
            "E_CACHE_CORRUPT",
            "reusable content-addressed artifact is missing or corrupt",
            task_id=task_id,
            details={"artifact_path": path, "content_hash": expected},
        ) from exc


def _ledger_metrics(
    candidate: np.ndarray,
    reference: np.ndarray,
    profile_full_candidate: np.ndarray,
    rgb_candidate: np.ndarray,
    rgb_reference: np.ndarray,
    mask: np.ndarray,
    task: TaskSpec,
    ops: Ops,
    candidate_final_stats: tuple[float, float],
    reference_final_stats: tuple[float, float],
    config: EffectiveConfig,
) -> dict[str, Any]:
    diff = np.abs(ops.to_float64(candidate) - reference)
    mask_full = np.broadcast_to(mask, diff.shape)
    ref_abs_max = float(np.max(np.abs(reference)))
    max_abs = float(np.max(diff))
    max_rel = max_abs / max(ref_abs_max, 1e-12)
    boundary_values = diff[mask_full]
    boundary_max = float(np.max(boundary_values)) if boundary_values.size else 0.0
    interior_values = diff[~mask_full]
    interior_max = float(np.max(interior_values)) if interior_values.size else 0.0

    quantized_reference = ops.to_float64(ops.from_float64(reference))
    quant_floor = float(np.max(np.abs(quantized_reference - reference)))

    candidate_mean, candidate_std = candidate_final_stats
    reference_mean, reference_std = reference_final_stats
    candidate_std = candidate_std if abs(candidate_std) > 1e-15 else 1.0
    reference_std = reference_std if abs(reference_std) > 1e-15 else 1.0
    normalized_by_candidate = (reference - candidate_mean) / candidate_std
    normalized_by_reference = (reference - reference_mean) / reference_std
    norm_delta_values = (
        np.abs(normalized_by_candidate - normalized_by_reference)[mask_full]
        * float(config.denoise["boundary_mix"])
    )
    normalization_delta = (
        float(np.max(norm_delta_values)) if norm_delta_values.size else 0.0
    )
    boundary_excess = max(0.0, boundary_max - interior_max)
    scheduling_diff = np.abs(
        ops.to_float64(candidate) - ops.to_float64(profile_full_candidate)
    )
    scheduling_boundary_values = scheduling_diff[mask_full]
    scheduling_boundary_max = (
        float(np.max(scheduling_boundary_values))
        if scheduling_boundary_values.size
        else 0.0
    )
    scheduling_interior_values = scheduling_diff[~mask_full]
    scheduling_interior_max = (
        float(np.max(scheduling_interior_values))
        if scheduling_interior_values.size
        else 0.0
    )
    block_scheduling_excess = max(
        0.0, scheduling_boundary_max - scheduling_interior_max
    )
    components = {
        "quantization": quant_floor,
        "normalization": normalization_delta,
        "block_boundary": block_scheduling_excess,
    }
    if max_abs == 0.0:
        dominant_source = "none"
    else:
        dominant_source = max(components, key=lambda key: components[key])
        if components[dominant_source] <= 1e-15:
            dominant_source = "block_boundary" if boundary_max > 0.0 else "quantization"

    amplification = max_abs / max(quant_floor, 1e-12)
    rgb_diff = np.abs(rgb_candidate - rgb_reference)
    rgb_max_abs = float(np.max(rgb_diff))
    rgb_max_rel = rgb_max_abs / max(float(np.max(np.abs(rgb_reference))), 1e-12)
    tolerance = config.tolerance
    first_deviation = _first_position(diff, float(tolerance["max_abs"]))
    reasons: list[str] = []
    if max_abs > float(tolerance["max_abs"]):
        reasons.append("max_abs")
    if max_rel > float(tolerance["max_rel"]):
        reasons.append("max_rel")
    if amplification > float(tolerance["max_amplification"]):
        reasons.append("max_amplification")
    tolerance_ok = not reasons
    return {
        "max_abs": max_abs,
        "max_rel": max_rel,
        "error_amplification": amplification,
        "boundary_max_abs": boundary_max,
        "interior_max_abs": interior_max,
        "quantization_floor_error": quant_floor,
        "normalization_delta": normalization_delta,
        "block_boundary_excess": boundary_excess,
        "profile_consistent_scheduling_max_abs": float(np.max(scheduling_diff)),
        "profile_consistent_boundary_scheduling_max_abs": scheduling_boundary_max,
        "block_scheduling_excess": block_scheduling_excess,
        "dominant_source": dominant_source,
        "components": components,
        "rgb_max_abs": rgb_max_abs,
        "rgb_max_rel": rgb_max_rel,
        "first_deviation": first_deviation,
        "tolerance_ok": tolerance_ok,
        "tolerance_reasons": reasons,
        "tolerance": {
            "max_abs": float(tolerance["max_abs"]),
            "max_rel": float(tolerance["max_rel"]),
            "max_amplification": float(tolerance["max_amplification"]),
        },
        "candidate_final_stats": {
            "mean": candidate_mean,
            "std": candidate_std,
        },
        "reference_final_stats": {
            "mean": reference_mean,
            "std": reference_std,
        },
        "block_boundaries": _block_boundary_metrics(
            diff,
            mask,
            task,
            float(tolerance["max_abs"]),
        ),
    }


def compute_task(
    task: TaskSpec,
    config: EffectiveConfig,
    blobs: BlobStore | None = None,
    reuse: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run all four stages and return a deterministic, content-addressed result."""

    reuse = reuse or {}
    input_hash = task.input_hash(config)
    condition_key = condition_cache_key(task, config)
    noise_key = noise_cache_key(task)

    condition = _load_reused_array(blobs, reuse.get("condition"), task_id=task.task_id)
    if condition is None:
        condition = condition_embedding(task.prompt, config)
    condition = np.ascontiguousarray(condition, dtype=np.float64)
    if condition.shape != (config.condition_dim,):
        raise EngineError(
            "E_CACHE_CORRUPT",
            "reused condition vector has the wrong shape",
            task_id=task.task_id,
            details={"shape": list(condition.shape)},
        )

    noise = None
    if task.latent_file is None:
        noise = _load_reused_array(blobs, reuse.get("noise"), task_id=task.task_id)
    if noise is None:
        noise = initial_noise(task)
    noise = np.ascontiguousarray(noise, dtype=np.float64)
    if noise.shape != task.latent_shape:
        raise EngineError(
            "E_LATENT_SHAPE_MISMATCH",
            "noise shape differs from latent_shape",
            task_id=task.task_id,
            details={
                "latent_shape": list(task.latent_shape),
                "noise_shape": list(noise.shape),
            },
        )

    ops = make_ops(task.dtype_profile, task.quant_scale, task.rounding_mode)
    mask = boundary_mask(task.latent_shape, task.block_grid)
    candidate, candidate_records, candidate_steps = _denoise_blockwise(
        noise, task, condition, config, ops, mask
    )
    candidate_full, _, _ = _denoise_reference(
        noise, task, condition, config, mask, ops=ops
    )
    reference, reference_records, reference_steps = _denoise_reference(
        noise, task, condition, config, mask
    )
    candidate_f64 = ops.to_float64(candidate)
    rgb_float, rgb_u8 = decode_rgb(candidate_f64, config)
    reference_rgb_float, reference_rgb_u8 = decode_rgb(reference, config)

    artifacts: list[dict[str, Any]] = []
    conditions = _put_array(blobs, "condition", condition_key, condition)
    artifacts.append(conditions)
    noises = _put_array(blobs, "initial_noise", noise_key, noise)
    artifacts.append(noises)
    candidate_initial = ops.from_float64(noise)
    artifacts.append(
        _put_array(
            blobs,
            "candidate_initial",
            hash_json(
                {
                    "input_hash": input_hash,
                    "profile": task.dtype_profile,
                    "quant_scale": task.quant_scale,
                    "rounding_mode": task.rounding_mode,
                }
            ),
            candidate_initial,
        )
    )

    normalization_artifacts: list[dict[str, Any]] = []
    for candidate_record, reference_record, candidate_step, reference_step in zip(
        candidate_records, reference_records, candidate_steps, reference_steps
    ):
        step = candidate_record["step"]
        step_prefix = hash_json(
            {
                "input_hash": input_hash,
                "profile": task.dtype_profile,
                "quant_scale": task.quant_scale,
                "rounding_mode": task.rounding_mode,
                "block_grid": list(task.block_grid),
                "step": step,
            }
        )
        artifacts.append(
            _put_array(blobs, "candidate_step", f"{step_prefix}:candidate", candidate_step)
        )
        artifacts.append(
            _put_array(blobs, "reference_step", f"{step_prefix}:reference", reference_step)
        )
        stats_payload = {
            "step": step,
            "candidate": {
                "mean": candidate_record["mean"],
                "std": candidate_record["std"],
                "raw_hash": candidate_record["raw_hash"],
            },
            "reference": {
                "mean": reference_record["mean"],
                "std": reference_record["std"],
                "raw_hash": reference_record["raw_hash"],
            },
        }
        stats_artifact = _put_json(
            blobs, "normalization_stats", f"{step_prefix}:stats", stats_payload
        )
        artifacts.append(stats_artifact)
        normalization_artifacts.append(stats_artifact)

    final_candidate = _put_array(
        blobs, "final_candidate", f"{input_hash}:candidate", candidate
    )
    artifacts.append(final_candidate)
    final_reference = _put_array(
        blobs, "reference_final", f"{input_hash}:reference", reference
    )
    artifacts.append(final_reference)
    profile_full_artifact = _put_array(
        blobs,
        "profile_full_candidate",
        f"{input_hash}:profile_full",
        candidate_full,
    )
    artifacts.append(profile_full_artifact)
    rgb_float_artifact = _put_array(
        blobs, "rgb_float", f"{input_hash}:rgb_float", rgb_float
    )
    artifacts.append(rgb_float_artifact)
    rgb_u8_artifact = _put_array(
        blobs, "rgb_u8", f"{input_hash}:rgb_u8", rgb_u8
    )
    artifacts.append(rgb_u8_artifact)

    metrics = _ledger_metrics(
        candidate,
        reference,
        candidate_full,
        rgb_float,
        reference_rgb_float,
        mask,
        task,
        ops,
        (
            candidate_records[-1]["mean"] if candidate_records else 0.0,
            candidate_records[-1]["std"] if candidate_records else 1.0,
        ),
        (
            reference_records[-1]["mean"] if reference_records else 0.0,
            reference_records[-1]["std"] if reference_records else 1.0,
        ),
        config,
    )
    ledger_payload = {
        "schema": "latent-diag-ledger-v1",
        "task_id": task.task_id,
        "revision": task.revision,
        "input_hash": input_hash,
        "dtype_profile": task.dtype_profile,
        "profile": {
            "quant_scale": task.quant_scale,
            "rounding_mode": task.rounding_mode,
        },
        "reference_path": "float64_full_frame",
        "block_grid": list(task.block_grid),
        "block_schedule": block_schedule(task),
        "block_schedule_hash": hash_json(block_schedule(task)),
        "sampler_steps": task.sampler_steps,
        "condition_hash": conditions["content_hash"],
        "initial_noise_hash": noises["content_hash"],
        "candidate_final_hash": final_candidate["content_hash"],
        "reference_final_hash": final_reference["content_hash"],
        "profile_full_candidate_hash": profile_full_artifact["content_hash"],
        "rgb_float_hash": rgb_float_artifact["content_hash"],
        "rgb_u8_hash": rgb_u8_artifact["content_hash"],
        "reference_rgb_u8_hash": hash_array(reference_rgb_u8),
        "boundary_mask_hash": hash_array(mask),
        "step_hashes": [
            {
                "step": candidate_record["step"],
                "candidate_hash": candidate_record["candidate_hash"],
                "reference_hash": reference_record["reference_hash"],
                "candidate_stats_hash": candidate_record["stats_hash"],
                "reference_stats_hash": reference_record["stats_hash"],
            }
            for candidate_record, reference_record in zip(
                candidate_records, reference_records
            )
        ],
        "normalization_stats_hashes": [
            {
                "step": candidate_record["step"],
                "content_hash": stats_artifact["content_hash"],
            }
            for candidate_record, stats_artifact in zip(
                candidate_records, normalization_artifacts
            )
        ],
        "metrics": metrics,
    }
    ledger_hash = hash_json(ledger_payload)
    ledger_artifact = _put_json(
        blobs,
        "ledger_entry",
        f"{input_hash}:{ledger_hash}",
        ledger_payload,
    )
    artifacts.append(ledger_artifact)

    tolerance_ok = bool(metrics["tolerance_ok"])
    status = "ok" if tolerance_ok else "tolerance_failed"
    error = None
    if not tolerance_ok:
        error = {
            "code": "E_TOLERANCE_EXCEEDED",
            "message": "blockwise output exceeds configured tolerance",
            "task_id": task.task_id,
            "first_deviation": metrics["first_deviation"],
            "reasons": metrics["tolerance_reasons"],
        }

    stages = {
        "condition": conditions,
        "noise": noises,
        "denoise": final_candidate,
        "rgb": rgb_u8_artifact,
    }
    return {
        "ok": tolerance_ok,
        "status": status,
        "task_id": task.task_id,
        "revision": task.revision,
        "input_hash": input_hash,
        "ledger": ledger_payload,
        "ledger_hash": ledger_hash,
        "ledger_path": ledger_artifact["artifact_path"],
        "metrics": metrics,
        "artifacts": artifacts,
        "stages": stages,
        "output_hashes": {
            "condition": conditions["content_hash"],
            "noise": noises["content_hash"],
            "candidate": final_candidate["content_hash"],
            "reference": final_reference["content_hash"],
            "rgb_float": rgb_float_artifact["content_hash"],
            "rgb_u8": rgb_u8_artifact["content_hash"],
        },
        "error": error,
    }

