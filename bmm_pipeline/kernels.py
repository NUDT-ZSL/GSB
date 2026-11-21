from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .config import PipelineConfig
from .errors import error
from .jobs import JobSpec
from .matrices import float64_reference, generate_operands
from .strategy import StrategyPlan


@dataclass
class KernelStats:
    block_count: int = 0
    compensation_interventions: int = 0
    compensation_element_updates: int = 0
    flushed_subnormals: int = 0
    rounded_values: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "block_count": self.block_count,
            "compensation_interventions": self.compensation_interventions,
            "compensation_element_updates": self.compensation_element_updates,
            "flushed_subnormals": self.flushed_subnormals,
            "rounded_values": self.rounded_values,
        }


@dataclass
class PartialTile:
    batch_start: int
    batch_stop: int
    m_start: int
    m_stop: int
    n_start: int
    n_stop: int
    contribution: np.ndarray


@dataclass
class KernelRun:
    result: np.ndarray
    stats: KernelStats
    diagnostics: dict[str, Any]
    reference: np.ndarray | None = None
    partials: list[PartialTile] | None = None


BlockHook = Callable[[str, int], None]


def _starts(size: int, block: int) -> list[int]:
    return list(range(0, size, block))


def _float(value: float) -> float:
    value = float(value)
    if math.isnan(value):
        return float("nan")
    return value


def _sanitize(values: np.ndarray, config: PipelineConfig, stats: KernelStats) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if config.subnormal_mode == "flush":
        tiny = np.finfo(np.float64).tiny
        mask = np.isfinite(array) & (array != 0.0) & (np.abs(array) < tiny)
        count = int(np.count_nonzero(mask))
        if count:
            array = array.copy()
            array[mask] = 0.0
            stats.flushed_subnormals += count
    return array


def _directed_round(
    values: np.ndarray, mode: str, stats: KernelStats
) -> np.ndarray:
    if mode == "nearest":
        return np.asarray(values, dtype=np.float64)
    array = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(array) & (array != 0.0)
    if not np.any(finite):
        return array
    rounded = array.copy()
    if mode == "up":
        rounded[finite] = np.nextafter(array[finite], np.inf)
    elif mode == "down":
        rounded[finite] = np.nextafter(array[finite], -np.inf)
    elif mode == "toward_zero":
        rounded[finite] = np.nextafter(array[finite], 0.0)
    else:
        raise ValueError(f"unsupported rounding mode: {mode}")
    stats.rounded_values += int(np.count_nonzero(finite))
    return rounded


def _accumulate(
    target: np.ndarray,
    contribution: np.ndarray,
    compensation: np.ndarray,
    mode: str,
    stats: KernelStats,
) -> tuple[np.ndarray, np.ndarray]:
    target64 = np.asarray(target, dtype=np.float64)
    contribution64 = np.asarray(contribution, dtype=np.float64)
    compensation64 = np.asarray(compensation, dtype=np.float64)
    if mode == "plain":
        return target64 + contribution64, compensation64
    if mode == "kahan":
        corrected = contribution64 - compensation64
        updated = target64 + corrected
        new_compensation = (updated - target64) - corrected
    elif mode == "neumaier":
        updated = target64 + contribution64
        target_larger = np.abs(target64) >= np.abs(contribution64)
        new_compensation = np.where(
            target_larger,
            compensation64 + ((target64 - updated) + contribution64),
            compensation64 + ((contribution64 - updated) + target64),
        )
    else:
        raise ValueError(f"unsupported accum mode: {mode}")
    active = new_compensation != 0.0
    active_count = int(np.count_nonzero(active))
    if active_count:
        stats.compensation_interventions += 1
        stats.compensation_element_updates += active_count
    return updated, new_compensation


def _finalize(
    result: np.ndarray,
    compensation: np.ndarray,
    mode: str,
    config: PipelineConfig,
    stats: KernelStats,
) -> np.ndarray:
    final = np.asarray(result, dtype=np.float64).copy()
    if mode != "plain":
        final = final + np.asarray(compensation, dtype=np.float64)
    final = _sanitize(final, config, stats)
    final = _directed_round(final, config.rounding_mode, stats)
    final = _sanitize(final, config, stats)
    return np.asarray(final, dtype=np.float64, order="C")


def _apply_tile(
    result: np.ndarray,
    compensation: np.ndarray,
    tile: PartialTile,
    mode: str,
    stats: KernelStats,
) -> None:
    target = result[
        tile.batch_start : tile.batch_stop,
        tile.m_start : tile.m_stop,
        tile.n_start : tile.n_stop,
    ]
    comp = compensation[
        tile.batch_start : tile.batch_stop,
        tile.m_start : tile.m_stop,
        tile.n_start : tile.n_stop,
    ]
    updated, new_comp = _accumulate(target, tile.contribution, comp, mode, stats)
    result[
        tile.batch_start : tile.batch_stop,
        tile.m_start : tile.m_stop,
        tile.n_start : tile.n_stop,
    ] = updated
    compensation[
        tile.batch_start : tile.batch_stop,
        tile.m_start : tile.m_stop,
        tile.n_start : tile.n_stop,
    ] = new_comp


def _compute_tiles(
    left: np.ndarray,
    right: np.ndarray,
    plan: StrategyPlan,
    config: PipelineConfig,
    accum_mode: str,
    stats: KernelStats,
    *,
    collect_partials: bool,
    hook: BlockHook | None,
) -> tuple[np.ndarray, np.ndarray, list[PartialTile] | None]:
    batch, m, k = left.shape
    _, _, n = right.shape
    block_m = plan.block_size["m"]
    block_n = plan.block_size["n"]
    block_k = plan.block_size["k"]
    result = np.zeros((batch, m, n), dtype=np.float64, order="C")
    compensation = np.zeros_like(result)
    partials: list[PartialTile] | None = [] if collect_partials else None
    tile_in_job = 0

    if plan.batch_path == "batched" and batch > 1:
        for m_start in _starts(m, block_m):
            m_stop = min(m, m_start + block_m)
            for n_start in _starts(n, block_n):
                n_stop = min(n, n_start + block_n)
                for k_start in _starts(k, block_k):
                    k_stop = min(k, k_start + block_k)
                    contribution = np.matmul(
                        left[:, m_start:m_stop, k_start:k_stop],
                        right[:, k_start:k_stop, n_start:n_stop],
                    )
                    contribution = _sanitize(contribution, config, stats)
                    tile = PartialTile(
                        0,
                        batch,
                        m_start,
                        m_stop,
                        n_start,
                        n_stop,
                        np.asarray(contribution, dtype=np.float64),
                    )
                    _apply_tile(result, compensation, tile, accum_mode, stats)
                    if partials is not None:
                        partials.append(tile)
                    tile_in_job += 1
                    stats.block_count += 1
                    if hook is not None:
                        hook(plan.job_id, tile_in_job)
    else:
        m_values = _starts(m, block_m)
        n_values = _starts(n, block_n)
        if plan.loop_order == ["m", "n", "batch", "k"]:
            outer = (
                (m_start, n_start, batch_index)
                for m_start in m_values
                for n_start in n_values
                for batch_index in range(batch)
            )
        else:
            outer = (
                (m_start, n_start, batch_index)
                for batch_index in range(batch)
                for m_start in m_values
                for n_start in n_values
            )
        for m_start, n_start, batch_index in outer:
            m_stop = min(m, m_start + block_m)
            n_stop = min(n, n_start + block_n)
            for k_start in _starts(k, block_k):
                k_stop = min(k, k_start + block_k)
                contribution = np.matmul(
                    left[
                        batch_index : batch_index + 1,
                        m_start:m_stop,
                        k_start:k_stop,
                    ],
                    right[
                        batch_index : batch_index + 1,
                        k_start:k_stop,
                        n_start:n_stop,
                    ],
                )
                contribution = _sanitize(contribution, config, stats)
                tile = PartialTile(
                    batch_index,
                    batch_index + 1,
                    m_start,
                    m_stop,
                    n_start,
                    n_stop,
                    np.asarray(contribution, dtype=np.float64),
                )
                _apply_tile(result, compensation, tile, accum_mode, stats)
                if partials is not None:
                    partials.append(tile)
                tile_in_job += 1
                stats.block_count += 1
                if hook is not None:
                    hook(plan.job_id, tile_in_job)
    return result, compensation, partials


def _comparison(actual: np.ndarray, reference: np.ndarray) -> tuple[dict[str, Any], np.ndarray]:
    actual64 = np.asarray(actual, dtype=np.float64, order="C")
    reference64 = np.asarray(reference, dtype=np.float64, order="C")
    difference = np.abs(actual64 - reference64)
    equal_nan = np.isnan(actual64) & np.isnan(reference64)
    difference = np.where(equal_nan, 0.0, difference)
    return {
        "max_abs_error": _float(np.max(difference)) if difference.size else 0.0,
        "mean_abs_error": _float(np.mean(difference)) if difference.size else 0.0,
        "p95_abs_error": _float(np.quantile(difference, 0.95)) if difference.size else 0.0,
        "p99_abs_error": _float(np.quantile(difference, 0.99)) if difference.size else 0.0,
    }, difference


def check_tolerance(
    actual: np.ndarray,
    reference: np.ndarray,
    *,
    job_id: str,
    atol: float,
    rtol: float,
    enforce: bool = True,
) -> dict[str, Any]:
    actual64 = np.asarray(actual, dtype=np.float64, order="C")
    reference64 = np.asarray(reference, dtype=np.float64, order="C")
    if actual64.shape != reference64.shape:
        raise error(
            "E_TOLERANCE_EXCEEDED",
            "分块结果与参考结果形状不一致",
            job_id=job_id,
            field="result",
            details={"actual_shape": list(actual64.shape), "reference_shape": list(reference64.shape)},
        )
    difference = np.abs(actual64 - reference64)
    allowed = atol + rtol * np.abs(reference64)
    equal = (actual64 == reference64) | np.isnan(actual64) & np.isnan(reference64)
    violated = ~equal & ((difference > allowed) | ~np.isfinite(difference))
    spectrum, absolute_difference = _comparison(actual64, reference64)
    denominator = np.abs(reference64)
    relative = np.divide(
        absolute_difference,
        denominator,
        out=np.zeros_like(absolute_difference),
        where=denominator != 0,
    )
    spectrum["max_rel_error"] = _float(np.max(relative)) if relative.size else 0.0
    spectrum["mismatch_count"] = int(np.count_nonzero(violated))
    spectrum["element_count"] = int(actual64.size)
    locations = np.argwhere(violated)
    elements: list[dict[str, Any]] = []
    for location in locations[:10]:
        index = tuple(int(value) for value in location)
        elements.append(
            {
                "index": list(index),
                "actual": _float(actual64[index]),
                "reference": _float(reference64[index]),
                "abs_error": _float(absolute_difference[index]),
                "allowed_error": _float(allowed[index]),
            }
        )
    spectrum["violations"] = elements
    if enforce and elements:
        raise error(
            "E_TOLERANCE_EXCEEDED",
            "分块累加结果超出 float64 参考容差",
            job_id=job_id,
            field="result",
            details=spectrum,
        )
    return spectrum


def compute_job(
    job: JobSpec,
    plan: StrategyPlan,
    config: PipelineConfig,
    *,
    collect_partials: bool = False,
    hook: BlockHook | None = None,
    enforce_tolerance: bool = True,
) -> KernelRun:
    left, right = generate_operands(job)
    left3 = left if left.ndim == 3 else left[np.newaxis, :, :]
    right3 = right if right.ndim == 3 else right[np.newaxis, :, :]
    mode = job.accum_mode
    stats = KernelStats()
    result3, compensation, partials = _compute_tiles(
        left3,
        right3,
        plan,
        config,
        mode,
        stats,
        collect_partials=collect_partials,
        hook=hook,
    )
    final3 = _finalize(result3, compensation, mode, config, stats)
    reference = float64_reference(left, right)
    diagnostics = check_tolerance(
        final3 if job.op_kind == "bmm" else final3[0],
        reference,
        job_id=job.job_id,
        atol=config.atol,
        rtol=config.rtol,
        enforce=enforce_tolerance,
    )
    diagnostics.update(stats.to_dict())
    diagnostics["batch_path"] = plan.batch_path
    diagnostics["degradation_categories"] = list(plan.degradation_categories)
    output = final3 if job.op_kind == "bmm" else final3[0]
    return KernelRun(
        result=np.asarray(output, dtype=np.float64, order="C"),
        stats=stats,
        diagnostics=diagnostics,
        reference=np.asarray(reference, dtype=np.float64, order="C"),
        partials=partials,
    )


def merge_partials(
    job: JobSpec,
    plan: StrategyPlan,
    config: PipelineConfig,
    partials: list[PartialTile],
) -> np.ndarray:
    batch = job.batch_size
    result = np.zeros((batch, job.m, job.n), dtype=np.float64, order="C")
    compensation = np.zeros_like(result)
    stats = KernelStats()
    for tile in partials:
        _apply_tile(result, compensation, tile, job.accum_mode, stats)
    final = _finalize(result, compensation, job.accum_mode, config, stats)
    return final if job.op_kind == "bmm" else final[0]


def compare_exact(actual: np.ndarray, expected: np.ndarray) -> bool:
    left = np.asarray(actual, dtype=np.float64, order="C")
    right = np.asarray(expected, dtype=np.float64, order="C")
    return bool(
        left.shape == right.shape
        and np.array_equal(left, right, equal_nan=True)
    )
