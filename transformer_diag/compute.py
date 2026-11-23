from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import BusinessError
from .models import JobSpec, PolicyConfig, ToleranceConfig
from .util import clamp_positions, decode_array, encode_array, npy_bytes, sha256_bytes


FLOAT64_TINY = float(np.finfo(np.float64).tiny)


@dataclass
class ComputationIssue:
    code: str
    message: str
    stage: str
    job_id: str
    field: str | None = None
    details: dict[str, Any] | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "type": "error",
            "source": "diagnostic",
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "job_id": self.job_id,
            "field": self.field,
            "details": self.details or {},
        }


def matrix_view(array: np.ndarray, axis: int) -> tuple[np.ndarray, tuple[int, ...]]:
    moved = np.moveaxis(array, axis, -1)
    moved_shape = moved.shape
    matrix = np.ascontiguousarray(moved).reshape(-1, moved_shape[-1])
    return matrix, moved_shape


def restore_axis(matrix: np.ndarray, moved_shape: tuple[int, ...], axis: int) -> np.ndarray:
    moved = matrix.reshape(moved_shape)
    return np.moveaxis(moved, -1, axis)


def _group_coordinate(flat_group: int, moved_shape: tuple[int, ...], axis: int) -> list[int | None]:
    dimensions = moved_shape[:-1]
    coordinate: list[int | None] = []
    unravelled = np.unravel_index(int(flat_group), dimensions) if dimensions else ()
    iterator = iter(int(item) for item in unravelled)
    for index in range(len(moved_shape)):
        if index == axis:
            coordinate.append(None)
        else:
            coordinate.append(next(iterator))
    return coordinate


def apply_subnormal_policy(array: np.ndarray, policy: PolicyConfig) -> tuple[np.ndarray, dict[str, Any]]:
    source_dtype = np.asarray(array).dtype
    values = np.asarray(array, dtype=np.float64).copy()
    source_tiny = (
        float(np.finfo(source_dtype).tiny)
        if np.issubdtype(source_dtype, np.floating)
        else FLOAT64_TINY
    )
    mask = (values != 0.0) & (np.abs(values) < source_tiny)
    flat_indices = np.flatnonzero(mask.reshape(-1))
    if policy.subnormal_policy == "flush":
        values[mask] = 0.0
    return values, {
        "policy": policy.subnormal_policy,
        "count": int(flat_indices.size),
        "positions": clamp_positions(flat_indices.tolist(), values.shape),
    }


def _guard_eps(job: JobSpec, policy: PolicyConfig) -> dict[str, Any]:
    eps = float(job.eps)
    band_abs = policy.band * max(abs(policy.eps_min), FLOAT64_TINY)
    if policy.strategy == "strict":
        if eps < policy.eps_min:
            raise BusinessError(
                "E_POLICY_GUARD",
                f"eps {eps!r} is below strict lower bound {policy.eps_min!r}",
                job_id=job.job_id,
                field="eps",
                stage="validate_plan",
                details={"strategy": policy.strategy, "threshold": policy.eps_min},
            )
        return {"effective_eps": eps, "intervened": False, "strategy": policy.strategy}
    if eps < policy.eps_min - band_abs:
        raise BusinessError(
            "E_POLICY_GUARD",
            f"eps {eps!r} is outside the lower-bound tolerance band",
            job_id=job.job_id,
            field="eps",
            stage="validate_plan",
            details={
                "strategy": policy.strategy,
                "threshold": policy.eps_min,
                "band": policy.band,
                "band_abs": band_abs,
            },
        )
    intervened = eps < policy.eps_min
    return {
        "effective_eps": policy.eps_min if intervened else eps,
        "intervened": intervened,
        "strategy": policy.strategy,
        "distance": abs(eps - policy.eps_min),
        "band_abs": band_abs,
    }


def _guard_block_size(job: JobSpec, policy: PolicyConfig) -> dict[str, Any]:
    block_size = int(job.block_size)
    band_abs = policy.band * max(1, abs(policy.block_size_max))
    if policy.strategy == "strict":
        if block_size > policy.block_size_max:
            raise BusinessError(
                "E_POLICY_GUARD",
                f"block_size {block_size} exceeds strict maximum {policy.block_size_max}",
                job_id=job.job_id,
                field="block_size",
                stage="validate_plan",
                details={"strategy": policy.strategy, "threshold": policy.block_size_max},
            )
        return {
            "effective_block_size": block_size,
            "intervened": False,
            "strategy": policy.strategy,
        }
    if block_size > policy.block_size_max + band_abs:
        raise BusinessError(
            "E_POLICY_GUARD",
            f"block_size {block_size} is outside the maximum tolerance band",
            job_id=job.job_id,
            field="block_size",
            stage="validate_plan",
            details={
                "strategy": policy.strategy,
                "threshold": policy.block_size_max,
                "band": policy.band,
                "band_abs": band_abs,
            },
        )
    intervened = block_size > policy.block_size_max
    return {
        "effective_block_size": policy.block_size_max if intervened else block_size,
        "intervened": intervened,
        "strategy": policy.strategy,
        "distance": abs(block_size - policy.block_size_max),
        "band_abs": band_abs,
    }


def plan_stage(
    job: JobSpec, input_array: np.ndarray, policy: PolicyConfig
) -> dict[str, Any]:
    if tuple(input_array.shape) != job.input_shape:
        raise BusinessError(
            "E_INPUT_INVALID",
            f"input shape {input_array.shape} does not match task shape {job.input_shape}",
            job_id=job.job_id,
            field="input_shape",
            stage="validate_plan",
        )
    if input_array.dtype != job.dtype_np():
        raise BusinessError(
            "E_INPUT_INVALID",
            f"input dtype {input_array.dtype} does not match task dtype {job.dtype}",
            job_id=job.job_id,
            field="dtype",
            stage="validate_plan",
        )
    eps_guard = _guard_eps(job, policy)
    block_guard = _guard_block_size(job, policy)
    working, subnormal = apply_subnormal_policy(input_array, policy)
    if not np.all(np.isfinite(working)):
        raise BusinessError(
            "E_NUMERIC_UNSTABLE",
            "input tensor became non-finite during validation/planning",
            job_id=job.job_id,
            field="input",
            stage="validate_plan",
        )
    axis_length = int(job.input_shape[job.axis_normalized])
    effective_block = min(int(block_guard["effective_block_size"]), axis_length)
    blocks = [
        {"index": index, "start": start, "end": min(start + effective_block, axis_length)}
        for index, start in enumerate(range(0, axis_length, effective_block))
    ]
    group_count = int(working.size // axis_length)
    interventions = int(subnormal["count"]) + int(eps_guard["intervened"]) + int(
        block_guard["intervened"]
    )
    return {
        "schema": 1,
        "op_kind": job.op_kind,
        "axis": job.axis_normalized,
        "input_shape": list(job.input_shape),
        "axis_length": axis_length,
        "n_groups": group_count,
        "requested_block_size": job.block_size,
        "effective_block_size": effective_block,
        "blocks": blocks,
        "compute_dtype": "float64",
        "guards": {
            "eps": eps_guard,
            "block_size": block_guard,
            "subnormal": subnormal,
            "intervention_count": interventions,
        },
        "protected_elements": {
            "subnormal_positions": subnormal["positions"],
            "eps_intervened": bool(eps_guard["intervened"]),
            "block_size_intervened": bool(block_guard["intervened"]),
        },
    }


def _matrix_from_plan(input_array: np.ndarray, plan: dict[str, Any], policy: PolicyConfig) -> np.ndarray:
    working, _ = apply_subnormal_policy(input_array, policy)
    matrix, moved_shape = matrix_view(working, int(plan["axis"]))
    if moved_shape[-1] != int(plan["axis_length"]):
        raise BusinessError(
            "E_INPUT_INVALID",
            "input tensor shape changed after plan creation",
            job_id=None,
            field="input_shape",
            stage="block_stats",
        )
    return matrix


def block_stats_stage(
    job: JobSpec,
    input_array: np.ndarray,
    plan: dict[str, Any],
    policy: PolicyConfig,
) -> dict[str, Any]:
    matrix = _matrix_from_plan(input_array, plan, policy)
    if not np.all(np.isfinite(matrix)):
        raise BusinessError(
            "E_NUMERIC_UNSTABLE",
            "normalization matrix contains non-finite values",
            job_id=job.job_id,
            field="input",
            stage="block_stats",
        )
    blocks = plan["blocks"]
    if job.op_kind == "softmax":
        local_max = np.empty((matrix.shape[0], len(blocks)), dtype=np.float64)
        local_sum = np.empty_like(local_max)
        for index, block in enumerate(blocks):
            chunk = matrix[:, int(block["start"]) : int(block["end"])]
            maximum = np.max(chunk, axis=1)
            local_max[:, index] = maximum
            local_sum[:, index] = np.sum(np.exp(chunk - maximum[:, None]), axis=1)
        if not (np.all(np.isfinite(local_max)) and np.all(np.isfinite(local_sum))):
            raise BusinessError(
                "E_NUMERIC_UNSTABLE",
                "softmax block statistics are non-finite",
                job_id=job.job_id,
                field="block_stats",
                stage="block_stats",
            )
        return {
            "schema": 1,
            "op_kind": job.op_kind,
            "block_count": len(blocks),
            "group_count": int(matrix.shape[0]),
            "stats": {
                "local_max": encode_array(local_max),
                "local_sumexp": encode_array(local_sum),
            },
        }

    counts = np.asarray(
        [int(block["end"]) - int(block["start"]) for block in blocks], dtype=np.float64
    )
    local_mean = np.empty((matrix.shape[0], len(blocks)), dtype=np.float64)
    local_m2 = np.empty_like(local_mean)
    for index, block in enumerate(blocks):
        chunk = matrix[:, int(block["start"]) : int(block["end"])]
        mean = np.mean(chunk, axis=1)
        local_mean[:, index] = mean
        local_m2[:, index] = np.sum((chunk - mean[:, None]) ** 2, axis=1)
    if not (np.all(np.isfinite(local_mean)) and np.all(np.isfinite(local_m2))):
        raise BusinessError(
            "E_NUMERIC_UNSTABLE",
            "layernorm block statistics are non-finite",
            job_id=job.job_id,
            field="block_stats",
            stage="block_stats",
        )
    return {
        "schema": 1,
        "op_kind": job.op_kind,
        "block_count": len(blocks),
        "group_count": int(matrix.shape[0]),
        "stats": {
            "counts": encode_array(counts),
            "local_mean": encode_array(local_mean),
            "local_m2": encode_array(local_m2),
        },
    }


def merge_softmax_stats(
    local_max: np.ndarray, local_sumexp: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    groups, blocks = local_max.shape
    running_max = np.full(groups, -np.inf, dtype=np.float64)
    running_sum = np.zeros(groups, dtype=np.float64)
    for index in range(blocks):
        block_max = local_max[:, index]
        block_sum = local_sumexp[:, index]
        new_max = np.maximum(running_max, block_max)
        old_scale = np.zeros_like(running_sum)
        valid_old = np.isfinite(running_max)
        old_scale[valid_old] = np.exp(running_max[valid_old] - new_max[valid_old])
        block_scale = np.exp(block_max - new_max)
        running_sum = running_sum * old_scale + block_sum * block_scale
        running_max = new_max
    return running_max, running_sum


def merge_layernorm_stats(
    counts: np.ndarray, local_mean: np.ndarray, local_m2: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    groups, blocks = local_mean.shape
    total_count = np.zeros(groups, dtype=np.float64)
    mean = np.zeros(groups, dtype=np.float64)
    m2 = np.zeros(groups, dtype=np.float64)
    for index in range(blocks):
        block_count = np.full(groups, float(counts[index]), dtype=np.float64)
        block_mean = local_mean[:, index]
        block_m2 = local_m2[:, index]
        new_count = total_count + block_count
        delta = block_mean - mean
        ratio = np.divide(block_count, new_count, out=np.zeros_like(new_count), where=new_count > 0)
        mean = mean + delta * ratio
        correction = np.divide(
            delta * delta * total_count * block_count,
            new_count,
            out=np.zeros_like(new_count),
            where=new_count > 0,
        )
        m2 = m2 + block_m2 + correction
        total_count = new_count
    variance = np.divide(m2, total_count, out=np.zeros_like(m2), where=total_count > 0)
    return mean, variance


def merge_block_stats(stats_payload: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    stats = stats_payload["stats"]
    if stats_payload["op_kind"] == "softmax":
        return merge_softmax_stats(
            decode_array(stats["local_max"]),
            decode_array(stats["local_sumexp"]),
        )
    counts = decode_array(stats["counts"])
    return merge_layernorm_stats(
        counts,
        decode_array(stats["local_mean"]),
        decode_array(stats["local_m2"]),
    )


def _guard_denominators(
    denominators: np.ndarray,
    policy: PolicyConfig,
    *,
    moved_shape: tuple[int, ...],
    axis: int,
    job_id: str,
) -> tuple[np.ndarray, dict[str, Any]]:
    values = np.asarray(denominators, dtype=np.float64)
    floor = max(float(policy.denom_min), FLOAT64_TINY)
    band_abs = policy.band * floor
    if policy.strategy == "strict":
        protect = (values < floor) | ((values != 0.0) & (np.abs(values) < FLOAT64_TINY))
    else:
        protect = (values < floor + band_abs) | (
            (values != 0.0) & (np.abs(values) < FLOAT64_TINY)
        )
    protected = values.copy()
    protected[protect] = np.maximum(values[protect], floor)
    affected = np.flatnonzero(protect.reshape(-1))
    records = []
    for flat_group in affected[:64].tolist():
        records.append(
            {
                "group_index": int(flat_group),
                "group_coordinate": _group_coordinate(int(flat_group), moved_shape, axis),
                "original": float(values.reshape(-1)[flat_group]),
                "protected": float(protected.reshape(-1)[flat_group]),
            }
        )
    return protected, {
        "count": int(affected.size),
        "elements": records,
        "floor": floor,
        "band": policy.band,
        "band_abs": band_abs,
        "strategy": policy.strategy,
    }


def normalize_stage(
    job: JobSpec,
    input_array: np.ndarray,
    plan: dict[str, Any],
    block_payload: dict[str, Any],
    policy: PolicyConfig,
    eps_effective: float,
) -> dict[str, Any]:
    matrix = _matrix_from_plan(input_array, plan, policy)
    moved_shape = np.moveaxis(input_array, int(plan["axis"]), -1).shape
    merged_a, merged_b = merge_block_stats(block_payload)
    guards: list[dict[str, Any]] = []

    if job.op_kind == "softmax":
        whole_max = np.max(matrix, axis=1)
        whole_sum = np.sum(np.exp(matrix - whole_max[:, None]), axis=1)
        merged_denom, merged_guard = _guard_denominators(
            merged_b,
            policy,
            moved_shape=moved_shape,
            axis=int(plan["axis"]),
            job_id=job.job_id,
        )
        whole_denom, whole_guard = _guard_denominators(
            whole_sum,
            policy,
            moved_shape=moved_shape,
            axis=int(plan["axis"]),
            job_id=job.job_id,
        )
        if merged_guard["count"]:
            guards.append({"stage": "normalize", "kind": "denominator", "source": "block", **merged_guard})
        if whole_guard["count"]:
            guards.append({"stage": "normalize", "kind": "denominator", "source": "whole", **whole_guard})
        merged_stats = {"max": merged_a, "denominator": merged_b}
        whole_stats = {"max": whole_max, "denominator": whole_sum}
        normalizer = {"max": merged_a, "denominator": merged_denom}
        whole_normalizer = {"max": whole_max, "denominator": whole_denom}
        if not all(
            np.all(np.isfinite(value))
            for value in (
                merged_a,
                merged_b,
                whole_max,
                whole_sum,
                merged_denom,
                whole_denom,
            )
        ):
            raise BusinessError(
                "E_NUMERIC_UNSTABLE",
                "softmax normalization statistics are non-finite",
                job_id=job.job_id,
                field="normalizer",
                stage="normalize",
            )
    else:
        whole_mean = np.mean(matrix, axis=1)
        whole_variance = np.mean((matrix - whole_mean[:, None]) ** 2, axis=1)
        merged_denom_raw = np.sqrt(np.maximum(merged_b, 0.0) + eps_effective)
        whole_denom_raw = np.sqrt(np.maximum(whole_variance, 0.0) + eps_effective)
        merged_denom, merged_guard = _guard_denominators(
            merged_denom_raw,
            policy,
            moved_shape=moved_shape,
            axis=int(plan["axis"]),
            job_id=job.job_id,
        )
        whole_denom, whole_guard = _guard_denominators(
            whole_denom_raw,
            policy,
            moved_shape=moved_shape,
            axis=int(plan["axis"]),
            job_id=job.job_id,
        )
        if merged_guard["count"]:
            guards.append({"stage": "normalize", "kind": "denominator", "source": "block", **merged_guard})
        if whole_guard["count"]:
            guards.append({"stage": "normalize", "kind": "denominator", "source": "whole", **whole_guard})
        merged_stats = {"mean": merged_a, "variance": merged_b}
        whole_stats = {"mean": whole_mean, "variance": whole_variance}
        normalizer = {"mean": merged_a, "invstd": 1.0 / merged_denom, "denominator": merged_denom}
        whole_normalizer = {
            "mean": whole_mean,
            "invstd": 1.0 / whole_denom,
            "denominator": whole_denom,
        }
        if not all(
            np.all(np.isfinite(value))
            for value in (
                merged_a,
                merged_b,
                whole_mean,
                whole_variance,
                merged_denom,
                whole_denom,
            )
        ):
            raise BusinessError(
                "E_NUMERIC_UNSTABLE",
                "layernorm normalization statistics are non-finite",
                job_id=job.job_id,
                field="normalizer",
                stage="normalize",
            )

    return {
        "schema": 1,
        "op_kind": job.op_kind,
        "merged_stats": {name: encode_array(value) for name, value in merged_stats.items()},
        "whole_stats": {name: encode_array(value) for name, value in whole_stats.items()},
        "normalizer": {name: encode_array(value) for name, value in normalizer.items()},
        "whole_normalizer": {
            name: encode_array(value) for name, value in whole_normalizer.items()
        },
        "guards": guards,
        "effective_eps": float(eps_effective),
        "merge_rule": "online-softmax" if job.op_kind == "softmax" else "welford-merge",
    }


def _output_from_normalizer(
    op_kind: str, matrix: np.ndarray, normalizer: dict[str, np.ndarray]
) -> np.ndarray:
    if op_kind == "softmax":
        maximum = normalizer["max"]
        denominator = normalizer["denominator"]
        return np.exp(matrix - maximum[:, None]) / denominator[:, None]
    mean = normalizer["mean"]
    invstd = normalizer["invstd"]
    return (matrix - mean[:, None]) * invstd[:, None]


def output_stage(
    job: JobSpec,
    input_array: np.ndarray,
    plan: dict[str, Any],
    normalize_payload: dict[str, Any],
    policy: PolicyConfig,
) -> dict[str, Any]:
    matrix = _matrix_from_plan(input_array, plan, policy)
    moved_shape = np.moveaxis(input_array, int(plan["axis"]), -1).shape
    merged_normalizer = {
        name: decode_array(value) for name, value in normalize_payload["normalizer"].items()
    }
    whole_normalizer = {
        name: decode_array(value)
        for name, value in normalize_payload["whole_normalizer"].items()
    }
    block_flat = _output_from_normalizer(job.op_kind, matrix, merged_normalizer)
    whole_flat = _output_from_normalizer(job.op_kind, matrix, whole_normalizer)
    block_f64 = restore_axis(block_flat, moved_shape, int(plan["axis"]))
    whole_f64 = restore_axis(whole_flat, moved_shape, int(plan["axis"]))
    if not (np.all(np.isfinite(block_f64)) and np.all(np.isfinite(whole_f64))):
        raise BusinessError(
            "E_NUMERIC_UNSTABLE",
            "output contains non-finite values",
            job_id=job.job_id,
            field="output",
            stage="output",
        )
    dtype = job.dtype_np()
    block_output = np.ascontiguousarray(block_f64.astype(dtype, copy=False))
    whole_output = np.ascontiguousarray(whole_f64.astype(dtype, copy=False))
    block_npy = npy_bytes(block_output)
    whole_npy = npy_bytes(whole_output)
    return {
        "schema": 1,
        "op_kind": job.op_kind,
        "output_dtype": job.dtype,
        "block_output_f64": encode_array(block_f64),
        "whole_output_f64": encode_array(whole_f64),
        "output_npy_sha256": sha256_bytes(block_npy),
        "whole_output_npy_sha256": sha256_bytes(whole_npy),
        "byte_identical_to_whole_path": block_npy == whole_npy,
    }


def compare_arrays(
    actual: np.ndarray,
    reference: np.ndarray,
    tolerance: ToleranceConfig,
    *,
    stat: bool = False,
    max_positions: int = 256,
) -> dict[str, Any]:
    a = np.asarray(actual, dtype=np.float64).reshape(-1)
    r = np.asarray(reference, dtype=np.float64).reshape(-1)
    if a.shape != r.shape:
        raise ValueError(f"array shape mismatch: {a.shape} != {r.shape}")
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(r))):
        raise BusinessError(
            "E_NUMERIC_UNSTABLE",
            "comparison input contains non-finite values",
            field="comparison",
            stage="diagnostic",
        )
    abs_dev = np.abs(a - r)
    rel_denominator = np.maximum(np.abs(r), FLOAT64_TINY)
    rel_dev = abs_dev / rel_denominator
    atol = tolerance.stat_atol if stat else tolerance.atol
    rtol = tolerance.stat_rtol if stat else tolerance.rtol
    threshold = atol + rtol * np.abs(r)
    exceed_mask = abs_dev > threshold
    if a.size:
        max_abs_flat = int(np.argmax(abs_dev))
        max_rel_flat = int(np.argmax(rel_dev))
    else:
        max_abs_flat = 0
        max_rel_flat = 0
    exceed_flat = np.flatnonzero(exceed_mask)
    return {
        "max_abs": float(abs_dev[max_abs_flat]) if a.size else 0.0,
        "max_abs_flat_index": max_abs_flat,
        "max_rel": float(rel_dev[max_rel_flat]) if a.size else 0.0,
        "max_rel_flat_index": max_rel_flat,
        "exceed_count": int(exceed_flat.size),
        "exceed_flat_indices": exceed_flat[:max_positions].astype(int).tolist(),
        "atol": float(atol),
        "rtol": float(rtol),
        "element_count": int(a.size),
    }


def evaluate_stat_consistency(
    job: JobSpec,
    normalize_payload: dict[str, Any],
    tolerance: ToleranceConfig,
) -> tuple[bool, dict[str, Any]]:
    merged = {name: decode_array(value) for name, value in normalize_payload["merged_stats"].items()}
    whole = {name: decode_array(value) for name, value in normalize_payload["whole_stats"].items()}
    comparisons: dict[str, Any] = {}
    passed = True
    for name in merged:
        comparison = compare_arrays(merged[name], whole[name], tolerance, stat=True)
        comparisons[name] = comparison
        if comparison["exceed_count"]:
            passed = False
    return passed, {"passed": passed, "fields": comparisons}


def evaluate_output_consistency(
    job: JobSpec,
    output_payload: dict[str, Any],
    tolerance: ToleranceConfig,
) -> tuple[bool, dict[str, Any]]:
    block = decode_array(output_payload["block_output_f64"])
    whole = decode_array(output_payload["whole_output_f64"])
    comparison = compare_arrays(block, whole, tolerance)
    comparison["byte_identical_to_whole_path"] = bool(
        output_payload["byte_identical_to_whole_path"]
    )
    comparison["passed"] = comparison["exceed_count"] == 0
    return comparison["passed"], comparison


def execute_pipeline(
    job: JobSpec, input_array: np.ndarray, policy: PolicyConfig
) -> dict[str, dict[str, Any]]:
    plan = plan_stage(job, input_array, policy)
    block_stats = block_stats_stage(job, input_array, plan, policy)
    eps_effective = float(plan["guards"]["eps"]["effective_eps"])
    normalize = normalize_stage(
        job, input_array, plan, block_stats, policy, eps_effective
    )
    output = output_stage(job, input_array, plan, normalize, policy)
    return {
        "validate_plan": plan,
        "block_stats": block_stats,
        "normalize": normalize,
        "output": output,
    }


def rebuild_statistics_from_blocks(
    op_kind: str, block_payload: dict[str, Any]
) -> dict[str, np.ndarray]:
    first, second = merge_block_stats(block_payload)
    if op_kind == "softmax":
        return {"max": first, "denominator": second}
    return {"mean": first, "variance": second}


def output_array_from_payload(output_payload: dict[str, Any]) -> np.ndarray:
    block = decode_array(output_payload["block_output_f64"])
    dtype = np.dtype(output_payload["output_dtype"])
    return np.ascontiguousarray(block.astype(dtype, copy=False))


def finite_or_raise(value: float, *, field: str, job_id: str, stage: str) -> float:
    if not math.isfinite(value):
        raise BusinessError(
            "E_NUMERIC_UNSTABLE",
            f"non-finite {field} encountered",
            job_id=job_id,
            field=field,
            stage=stage,
        )
    return value

