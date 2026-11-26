"""误差账本与主导来源归因。

以 float64 整段路径为参考真值，对初始校验、分块变换、跨块归约与定点
量化各级计算最大绝对偏差、相对偏差与误差放大比，并定位主导误差来源。
"""

import numpy as np

from .errors import EToleranceExceeded
from .precision import make_backend
from .transforms import (
    array_checksum,
    cross_block_reduce,
    run_block_serial,
    run_monolithic,
)

LEDGER_STAGES = (
    "initial_validation",
    "block_transform",
    "cross_reduce",
    "fixed_quantization",
)


def _to_float64(array, backend):
    if getattr(backend, "is_fixed", False):
        return backend.to_float(array)
    return np.asarray(array, dtype=np.float64)


def max_abs_diff(actual, reference):
    actual = np.asarray(actual, dtype=np.float64)
    reference = np.asarray(reference, dtype=np.float64)
    if actual.shape != reference.shape:
        raise ValueError("对比数组形状不一致")
    if actual.size == 0:
        return 0.0
    return float(np.max(np.abs(actual - reference)))


def max_rel_diff(actual, reference):
    actual = np.asarray(actual, dtype=np.float64)
    reference = np.asarray(reference, dtype=np.float64)
    denom = np.maximum(np.abs(reference), 1e-12)
    if actual.size == 0:
        return 0.0
    return float(np.max(np.abs(actual - reference) / denom))


def amplification(stage_abs, input_abs):
    """误差放大比 = 本级绝对偏差 / 输入端绝对偏差（输入端为 0 时记 0）。"""
    if input_abs <= 0.0:
        return 0.0
    return float(stage_abs / input_abs)


def _first_deviation_step(reference_initial, backend, num_steps, reference_states):
    """逐步进给，定位首个出现偏差的步骤（位级或数值偏差）。"""
    cur = backend.initial(reference_initial)
    for step in range(num_steps):
        cur = backend.update_block(cur, 0, 0, step)
        ref_step = reference_states[step + 1]
        cur_f = _to_float64(cur, backend)
        if not np.array_equal(np.asarray(cur_f), np.asarray(ref_step, dtype=np.float64)):
            return step + 1
    return None


def _reference_trajectory(initial, num_steps, budget=None):
    """float64 整段参考真值的逐步轨迹（含第 0 步初始状态）。"""
    backend = make_backend("float64")
    cur = backend.initial(initial)
    trajectory = [cur.copy()]
    for step in range(num_steps):
        cur = backend.update_block(cur, 0, 0, step)
        trajectory.append(cur.copy())
    return backend, trajectory


def build_ledger(task, initial_state, config, budget=None):
    """为单个任务构建完整误差账本。

    返回 (ledger_dict, payloads)，payloads 携带最终状态等供提交复用；
    超容差时账本仍完整返回，由调用方决定是否抛出 E_TOLERANCE_EXCEEDED。
    """
    ref_backend, ref_traj = _reference_trajectory(
        initial_state, task.num_steps, budget=budget
    )
    traj_bytes = sum(arr.nbytes for arr in ref_traj)
    traj_scope = (
        budget.scope(traj_bytes, "reference_trajectory") if budget else None
    )
    if traj_scope is None:
        return _build_ledger_body(
            task, config, initial_state, ref_backend, ref_traj, budget
        )
    with traj_scope:
        return _build_ledger_body(
            task, config, initial_state, ref_backend, ref_traj, budget
        )


def _build_ledger_body(task, config, initial_state, ref_backend, ref_traj,
                       budget):
    reference_final = ref_traj[-1]
    precision = config.precision

    backend = make_backend(precision, task.quant_scale, config.rounding_mode)
    block_final, traces = run_block_serial(
        initial_state, backend, task.num_steps, task.block_size, budget=budget
    )
    mono_final = run_monolithic(initial_state, backend, task.num_steps,
                                budget=budget)
    block_f = _to_float64(block_final, backend)
    mono_f = _to_float64(mono_final, backend)

    block_reduction = cross_block_reduce(block_final, traces, backend)
    ref_traces = run_block_serial(
        initial_state, ref_backend, task.num_steps, task.block_size,
        budget=budget
    )[1]
    ref_reduction = cross_block_reduce(reference_final, ref_traces, ref_backend)

    initial_actual = _to_float64(backend.initial(initial_state), backend)
    initial_abs = max_abs_diff(initial_actual, initial_state)
    initial_rel = max_rel_diff(initial_actual, initial_state)
    block_abs = max_abs_diff(block_f, reference_final)
    reduce_abs = abs(block_reduction["global_sum"] - ref_reduction["global_sum"])

    entries = []
    entries.append(
        {
            "stage": "initial_validation",
            "max_abs_diff": initial_abs,
            "max_rel_diff": initial_rel,
            "amplification": 0.0,
            "checksum": array_checksum(np.asarray(backend.initial(initial_state))),
        }
    )
    entries.append(
        {
            "stage": "block_transform",
            "max_abs_diff": block_abs,
            "max_rel_diff": max_rel_diff(block_f, reference_final),
            "amplification": amplification(block_abs, initial_abs),
            "checksum": array_checksum(np.asarray(block_final)),
        }
    )
    entries.append(
        {
            "stage": "cross_reduce",
            "max_abs_diff": reduce_abs,
            "max_rel_diff": max_rel_diff(
                np.array([block_reduction["global_sum"]]),
                np.array([ref_reduction["global_sum"]]),
            ),
            "amplification": amplification(reduce_abs, block_abs),
            "checksum": array_checksum(
                np.array(
                    [block_reduction["global_sum"], block_reduction["global_l2"]],
                    dtype=np.float64,
                )
            ),
        }
    )

    first_dev = _first_deviation_step(
        initial_state, backend, task.num_steps, ref_traj
    )
    quant_boundary = None
    if getattr(backend, "is_fixed", False):
        quant_boundary = {
            "scale": backend.scale,
            "rounding_mode": backend.rounding_mode,
            "quantized_min": int(np.min(block_final)),
            "quantized_max": int(np.max(block_final)),
            "float_min": float(np.min(block_f)),
            "float_max": float(np.max(block_f)),
        }
    entries.append(
        {
            "stage": "fixed_quantization",
            "max_abs_diff": block_abs if precision == "fixed" else 0.0,
            "max_rel_diff": (
                max_rel_diff(block_f, reference_final) if precision == "fixed" else 0.0
            ),
            "amplification": (
                amplification(block_abs, initial_abs) if precision == "fixed" else 0.0
            ),
            "checksum": array_checksum(np.asarray(block_final)),
            # 量化级同时记录“初始量化边界误差”与“经变换传播后的量化误差”，
            # 二者之差即纯传播贡献，便于主导来源归因。
            "initial_quant_abs": initial_abs if precision == "fixed" else 0.0,
            "initial_quant_rel": initial_rel if precision == "fixed" else 0.0,
        }
    )

    dominant = max(
        (e for e in entries if e["stage"] != "initial_validation"),
        key=lambda e: e["max_abs_diff"],
    )["stage"]
    over = [
        e["stage"]
        for e in entries
        if e["max_abs_diff"] > config.tolerance_abs
        or e["max_rel_diff"] > config.tolerance_rel
    ]
    ledger = {
        "task_id": task.task_id,
        "precision": precision,
        "algebra": config.algebra,
        "revision": task.revision,
        "block_size": task.block_size,
        "quant_scale": task.quant_scale,
        "entries": entries,
        "dominant_source": dominant,
        "first_deviation_step": first_dev,
        "quantization_boundary": quant_boundary,
        "tolerance_abs": config.tolerance_abs,
        "tolerance_rel": config.tolerance_rel,
        "exceeded_stages": over,
        "block_serial_vs_monolithic_abs": max_abs_diff(block_f, mono_f),
        "reduction": block_reduction,
        "final_checksum": array_checksum(np.asarray(block_final)),
    }
    payloads = {
        "final_state": np.asarray(block_final),
        "monolithic_state": np.asarray(mono_final),
        "traces": traces,
        "reduction": block_reduction,
    }
    return ledger, payloads


def assert_within_tolerance(ledger):
    if ledger["exceeded_stages"]:
        raise EToleranceExceeded(
            "误差超过容差",
            details={
                "task_id": ledger["task_id"],
                "exceeded_stages": ledger["exceeded_stages"],
                "first_deviation_step": ledger["first_deviation_step"],
            },
        )
