from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import DiagnosticError, ErrorCode
from .model import OperatorConfig, UnifiedTask
from .validation import validate_config, validate_task


@dataclass
class ExecutionResult:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.payload


def _quantize(values: np.ndarray, mode: str, stage: str, trace: list[dict[str, Any]]) -> np.ndarray:
    if mode == "none":
        return values
    if values.dtype == np.dtype("float32"):
        quantum = 1e-6
    elif values.dtype == np.dtype("float64"):
        quantum = 1e-9
    else:
        quantum = 0.0
    scaled = values / quantum
    if mode == "nearest_even":
        rounded = np.rint(scaled)
    elif mode == "toward_zero":
        rounded = np.trunc(scaled)
    elif mode == "floor":
        rounded = np.floor(scaled)
    elif mode == "ceil":
        rounded = np.ceil(scaled)
    else:
        raise DiagnosticError(ErrorCode.E_CONFIG_INVALID, "unknown rounding mode", field="rounding_mode")
    result = (rounded * quantum).astype(values.dtype, copy=False)
    trace.append(
        {
            "stage": stage,
            "mode": mode,
            "quantum": quantum,
            "changed_values": int(np.count_nonzero(result != values)),
            "dtype": str(values.dtype),
        }
    )
    return result


def _guard_subnormals(
    values: np.ndarray,
    guard: str,
    stage: str,
    events: list[dict[str, Any]],
) -> np.ndarray:
    if guard == "none":
        return values
    dtype = values.dtype
    if np.issubdtype(dtype, np.floating):
        tiny = float(np.finfo(dtype).tiny)
    else:
        return values
    mask = (values != 0) & (np.abs(values) < tiny)
    indices = np.flatnonzero(mask)
    for index in indices:
        original = float(values[int(index)])
        if guard == "clamp_tiny":
            replacement = math.copysign(tiny, original)
        elif guard == "flush_zero":
            replacement = 0.0
        else:
            raise DiagnosticError(ErrorCode.E_CONFIG_INVALID, "unknown subnormal guard", field="subnormal_guard")
        values[int(index)] = replacement
        events.append(
            {
                "stage": stage,
                "index": int(index),
                "original": original,
                "replacement": float(replacement),
                "guard": guard,
                "dtype": str(dtype),
            }
        )
    return values


def _softmax(scores: np.ndarray, dtype: np.dtype, *, stage: str, events: list[dict[str, Any]], guard: str) -> np.ndarray:
    if scores.size == 0:
        raise DiagnosticError(ErrorCode.E_SEMANTIC_INCOMPLETE, "candidate set is empty", field="candidate_mask")
    finite = np.isfinite(scores)
    if not np.any(finite):
        raise DiagnosticError(ErrorCode.E_CONFIG_INVALID, "all candidate scores are invalid", field="logits")
    shifted = scores - np.max(scores[finite])
    with np.errstate(over="ignore", under="ignore", invalid="ignore"):
        exponentials = np.exp(shifted).astype(dtype, copy=False)
    exponentials = _guard_subnormals(exponentials, guard, f"{stage}.exp", events)
    total = exponentials.sum(dtype=dtype)
    if not np.isfinite(total) or total <= 0:
        raise DiagnosticError(
            ErrorCode.E_CONFIG_INVALID,
            "softmax normalization failed",
            field="logits",
            details={"stage": stage, "dtype": str(dtype)},
        )
    probabilities = exponentials / total
    probabilities = _guard_subnormals(probabilities, guard, f"{stage}.normalize", events)
    return probabilities.astype(dtype, copy=False)


def _sort_indices(scores: np.ndarray, indices: list[int]) -> list[int]:
    return sorted(indices, key=lambda index: (-float(scores[index]), index))


def _validate_priority(task: UnifiedTask, config: OperatorConfig) -> str:
    if task.candidate_mask.has_exclusions():
        if config.mask_priority not in {"mask_first", "k_first"}:
            raise DiagnosticError(
                ErrorCode.E_MASK_PRIORITY_INVALID,
                "mask_priority is required when a candidate mask and k_value are both present",
                field="mask_priority",
                case_id=task.case_id,
            )
        return config.mask_priority
    return config.mask_priority or "mask_first"


def _removed_entry(index: int, reason: str, stage: str) -> dict[str, Any]:
    return {"index": index, "reason": reason, "stage": stage}


def execute_sampling(
    task: UnifiedTask,
    config: OperatorConfig,
    *,
    dtype_override: str | None = None,
    rounding_override: str | None = None,
    subnormal_guard_override: str | None = None,
) -> ExecutionResult:
    validate_config(config)
    validate_task(task)
    dtype_name = dtype_override or config.dtype
    rounding_mode = rounding_override or config.rounding_mode
    subnormal_guard = subnormal_guard_override or config.subnormal_guard
    if dtype_name not in {"float32", "float64"}:
        raise DiagnosticError(ErrorCode.E_CONFIG_INVALID, "dtype must be float32 or float64", field="dtype")
    if rounding_mode not in {"none", "nearest_even", "toward_zero", "floor", "ceil"}:
        raise DiagnosticError(ErrorCode.E_CONFIG_INVALID, "unknown rounding mode", field="rounding_mode")
    if subnormal_guard not in {"none", "clamp_tiny", "flush_zero"}:
        raise DiagnosticError(ErrorCode.E_CONFIG_INVALID, "unknown subnormal guard", field="subnormal_guard")
    priority = _validate_priority(task, config)
    dtype = np.dtype(dtype_name)
    subnormal_events: list[dict[str, Any]] = []
    rounding_trace: list[dict[str, Any]] = []
    logits = np.asarray(task.logits, dtype=dtype)
    temperature = np.asarray(task.temperature, dtype=dtype)
    with np.errstate(over="ignore", under="ignore", invalid="ignore", divide="ignore"):
        scores = logits / temperature
    scores = _quantize(scores, rounding_mode, "temperature_scaling", rounding_trace)
    scores = _guard_subnormals(scores, subnormal_guard, "temperature_scaling", subnormal_events)
    if not np.all(np.isfinite(scores)):
        raise DiagnosticError(
            ErrorCode.E_CONFIG_INVALID,
            "temperature scaling produced non-finite scores",
            field="temperature",
            case_id=task.case_id,
        )
    all_indices = list(range(len(task.logits)))
    mask_indices = set(task.candidate_mask.excluded_indices)
    removed: list[dict[str, Any]] = []
    working: list[int]
    if priority == "mask_first" or task.k_mode == "top_p":
        working = [index for index in all_indices if index not in mask_indices]
        for index in sorted(mask_indices):
            removed.append(_removed_entry(index, "mask_excluded", "mask_filter"))
    else:
        working = list(all_indices)
    if not working:
        raise DiagnosticError(
            ErrorCode.E_SEMANTIC_INCOMPLETE,
            "candidate mask removed every candidate",
            field="candidate_mask",
            case_id=task.case_id,
        )
    ordered_all = _sort_indices(scores, working)
    selected = list(ordered_all)
    if task.k_mode == "top_k":
        k = int(task.k_value)
        selected = ordered_all[:k]
        for index in ordered_all[k:]:
            removed.append(_removed_entry(index, "top_k_cutoff", "top_k"))
        if priority == "k_first":
            for index in sorted(mask_indices & set(selected)):
                removed.append(_removed_entry(index, "mask_excluded", "mask_filter"))
            selected = [index for index in selected if index not in mask_indices]
    selected = _sort_indices(scores, selected)
    if not selected:
        raise DiagnosticError(
            ErrorCode.E_SEMANTIC_INCOMPLETE,
            "candidate filtering removed every candidate",
            field="candidate_mask",
            case_id=task.case_id,
        )
    selected_scores = scores[np.asarray(selected)]
    probabilities = _softmax(
        selected_scores,
        dtype,
        stage="candidate_softmax",
        events=subnormal_events,
        guard=subnormal_guard,
    )
    probabilities = _quantize(probabilities, rounding_mode, "candidate_probabilities", rounding_trace)
    probabilities = _guard_subnormals(probabilities, subnormal_guard, "candidate_probabilities", subnormal_events)
    top_p = task.top_p if task.k_mode == "top_k" else task.k_value
    if top_p is not None:
        if not 0.0 < float(top_p) <= 1.0:
            raise DiagnosticError(
                ErrorCode.E_CONFIG_INVALID,
                "top_p must be in (0, 1]",
                field="top_p",
                case_id=task.case_id,
            )
        cumulative = np.cumsum(probabilities, dtype=dtype)
        cumulative = _quantize(cumulative, rounding_mode, "top_p_cumulative", rounding_trace)
        cumulative = _guard_subnormals(cumulative, subnormal_guard, "top_p_cumulative", subnormal_events)
        cutoff = len(selected)
        for position, value in enumerate(cumulative):
            if float(value) >= float(top_p):
                cutoff = position + 1
                break
        if cutoff < len(selected):
            for index in selected[cutoff:]:
                removed.append(_removed_entry(index, "top_p_cutoff", "top_p"))
            selected = selected[:cutoff]
            probabilities = probabilities[:cutoff]
            total = probabilities.sum(dtype=dtype)
            if total <= 0:
                raise DiagnosticError(
                    ErrorCode.E_CONFIG_INVALID,
                    "top_p retained probabilities cannot be normalized",
                    field="top_p",
                    case_id=task.case_id,
                )
            probabilities = probabilities / total
            probabilities = _guard_subnormals(
                probabilities,
                subnormal_guard,
                "top_p_renormalize",
                subnormal_events,
            )
    probabilities = probabilities.astype(dtype, copy=False)
    total = probabilities.sum(dtype=dtype)
    if not np.isfinite(total) or total <= 0:
        raise DiagnosticError(
            ErrorCode.E_CONFIG_INVALID,
            "final probability distribution is invalid",
            field="probabilities",
            case_id=task.case_id,
        )
    probabilities = probabilities / total
    probabilities = _guard_subnormals(
        probabilities,
        subnormal_guard,
        "final_normalize",
        subnormal_events,
    )
    for event in subnormal_events:
        if (
            event["stage"].startswith("candidate_")
            or event["stage"].startswith("top_p_")
            or event["stage"].startswith("final_")
        ) and 0 <= event["index"] < len(selected):
            event["candidate_index"] = int(selected[event["index"]])
    final_cumulative = np.cumsum(probabilities, dtype=dtype).astype(np.float64)
    rng = np.random.Generator(np.random.PCG64(task.seed))
    sample_sequence: list[int] = []
    sample_rank_sequence: list[int] = []
    for _ in range(task.num_samples):
        draw = float(rng.random())
        position = int(np.searchsorted(final_cumulative, draw, side="right"))
        if position >= len(selected):
            position = len(selected) - 1
        sample_sequence.append(int(selected[position]))
        sample_rank_sequence.append(position)
    candidates = [
        {
            "rank": rank,
            "index": int(index),
            "score": float(scores[index]),
            "probability": float(probabilities[rank]),
            "cumulative_probability": float(np.cumsum(probabilities, dtype=dtype)[rank]),
        }
        for rank, index in enumerate(selected)
    ]
    removed_sorted = sorted(removed, key=lambda item: (item["index"], item["stage"], item["reason"]))
    payload = {
        "case_id": task.case_id,
        "parameter_version": task.revision,
        "dtype": dtype_name,
        "rounding_mode": rounding_mode,
        "subnormal_guard": subnormal_guard,
        "mask_priority": priority,
        "rng": {"algorithm": "pcg64", "seed": task.seed},
        "candidate_order": [candidate["index"] for candidate in candidates],
        "candidates": candidates,
        "removed_candidates": removed_sorted,
        "sample_sequence": sample_sequence,
        "sample_rank_sequence": sample_rank_sequence,
        "subnormal_events": subnormal_events,
        "rounding_trace": rounding_trace,
    }
    return ExecutionResult(payload=payload)

