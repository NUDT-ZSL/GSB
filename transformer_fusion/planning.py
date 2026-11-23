"""Explicit fusion-vs-separate and resource fallback decisions."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from .errors import (
    Diagnostic,
    E_AXIS_INVALID,
    E_FUSION_INFEASIBLE,
    E_NO_ADJACENT_PAIR,
    E_POLICY_INVALID,
    E_POLICY_LOCALITY,
    E_POLICY_MEMORY,
    E_RESOURCE_LIMIT,
)
from .kernel import block_target_indices
from .model import CasePlan, DTYPE_ITEMSIZE, FusionPair, ResourceLimits, TaskCase


def locality_score(task: TaskCase, sample_limit: int = 8192) -> float:
    """Estimate scatter locality for one input-order traversal.

    The score combines the fraction of adjacent output addresses and the mean
    address jump.  Identity order approaches 1.0; a strided permutation is low.
    """

    numel = task.numel
    sample = min(numel, sample_limit)
    if sample <= 1:
        return 1.0
    targets = block_target_indices(task.input_shape, task.axis_order, 0, sample)
    if len(targets) <= 1:
        return 1.0
    jumps = np.abs(np.diff(targets.astype(np.int64)))
    contiguous = float(np.mean(jumps == 1))
    mean_jump = float(np.mean(jumps)) if len(jumps) else 1.0
    jump_score = 1.0 / (1.0 + math.log2(1.0 + mean_jump))
    return round(0.65 * contiguous + 0.35 * jump_score, 6)


def estimate_metrics(
    task: TaskCase,
    *,
    block_size: int,
    dtype: str,
) -> dict[str, Any]:
    itemsize = DTYPE_ITEMSIZE[dtype]
    numel = task.numel
    ndim = len(task.input_shape)
    block_count = math.ceil(numel / max(1, block_size))
    # Fused block holds input values, activated values, target indices and
    # coordinate temporaries.  Separate block only holds input/activated values.
    fused_block_bytes = int(block_size * (2 * itemsize + 8 * (ndim + 2)))
    separate_block_bytes = int(block_size * (2 * itemsize))
    input_bytes = int(numel * itemsize)
    output_bytes = int(numel * itemsize)
    fused_total = input_bytes + output_bytes + fused_block_bytes
    separate_total = input_bytes + output_bytes + output_bytes + separate_block_bytes
    return {
        "numel": numel,
        "itemsize": itemsize,
        "block_size": int(block_size),
        "block_count": int(block_count),
        "traversals_fused": 1,
        "traversals_separate": 2,
        "intermediate_writes_fused": 0,
        "intermediate_writes_separate": 1,
        "fused_block_resident_bytes": fused_block_bytes,
        "separate_block_resident_bytes": separate_block_bytes,
        "fused_single_block_memory_higher": fused_block_bytes > separate_block_bytes,
        "fused_estimated_resident_bytes": int(fused_total),
        "separate_estimated_resident_bytes": int(separate_total),
        "locality_score": locality_score(task),
        "irregular_reorder_penalty": round(1.0 - locality_score(task), 6),
    }


def _check_separate_resource(
    metrics: dict[str, Any],
    limits: ResourceLimits,
) -> Diagnostic | None:
    if metrics["separate_estimated_resident_bytes"] > limits.max_resident_bytes:
        return Diagnostic(
            code=E_RESOURCE_LIMIT,
            case_id="*",
            stage="resource_plan",
            message="even the separate reference path exceeds max_resident_bytes",
            details={
                "estimated_resident_bytes": metrics["separate_estimated_resident_bytes"],
                "max_resident_bytes": limits.max_resident_bytes,
            },
        )
    return None


def build_case_plan(
    task: TaskCase,
    pair: FusionPair | None,
    *,
    limits: ResourceLimits,
    batch_size: int = 1,
) -> CasePlan:
    limits = limits.merged(task.policy_options.get("limits"))
    mode = task.policy_mode
    supported_modes = {"fusion_first", "balanced", "memory_first", "locality_first"}
    diagnostics: list[Diagnostic] = []
    reasons: list[str] = []
    priority = ["hard_resource_limit", "policy_priority", "memory", "locality", "traversal_count"]

    requested_block = int(task.block_size)
    effective_block = max(limits.min_block_size, min(requested_block, limits.max_block_size))
    effective_dtype = task.dtype
    tier = {"float16": "fp16", "float32": "fp32", "float64": "fp64"}.get(effective_dtype, effective_dtype)
    metrics = estimate_metrics(task, block_size=effective_block, dtype=effective_dtype)
    waves = max(1, math.ceil(max(1, batch_size) / max(1, limits.max_parallel_cases)))

    feasible = pair is not None and pair.feasible
    if mode not in supported_modes:
        diagnostics.append(
            Diagnostic(
                code=E_POLICY_INVALID,
                case_id=task.case_id,
                stage="policy",
                message=f"unsupported policy mode: {mode}",
                details={"mode": mode, "supported_modes": sorted(supported_modes)},
            )
        )
        feasible = False
        reasons.append("invalid policy configuration")

    if pair is None:
        reasons.append("no adjacent activation/transpose pair")
        upstream = E_NO_ADJACENT_PAIR
    elif not pair.feasible:
        reasons.extend(pair.reasons or ("candidate pair failed feasibility rules",))
        upstream = pair.reason_code or E_FUSION_INFEASIBLE
    else:
        upstream = None

    # Axis diagnostics are normally produced by the parser.  Defensive checks
    # keep this planner usable as an independent entry point.
    if len(task.axis_order) != len(task.input_shape) or sorted(task.axis_order) != list(
        range(len(task.input_shape))
    ):
        diagnostics.append(
            Diagnostic(
                code=E_AXIS_INVALID,
                case_id=task.case_id,
                stage="feasibility",
                message="axis_order is not a valid permutation",
                details={"axis_order": list(task.axis_order)},
            )
        )
        feasible = False
        upstream = E_AXIS_INVALID

    # Block-count policy.  Expand blocks if that stays within memory; otherwise
    # the fused candidate cannot satisfy the explicit block cap.
    resource_fallback: tuple[str, str] | None = None
    if feasible and metrics["block_count"] > limits.max_blocks:
        if limits.allow_block_expand and task.numel > 0:
            needed = math.ceil(task.numel / max(1, limits.max_blocks))
            expanded = min(limits.max_block_size, max(effective_block, needed))
            expanded_metrics = estimate_metrics(task, block_size=expanded, dtype=effective_dtype)
            if (
                expanded_metrics["block_count"] <= limits.max_blocks
                and expanded_metrics["fused_estimated_resident_bytes"]
                <= limits.max_resident_bytes
            ):
                effective_block = expanded
                metrics = expanded_metrics
                reasons.append(
                    f"expanded block_size {requested_block}->{effective_block} to satisfy max_blocks"
                )
            else:
                resource_fallback = (
                    "block_count",
                    E_RESOURCE_LIMIT,
                )
                feasible = False
                reasons.append("fused block count stays above max_blocks after permitted expansion")
        else:
            resource_fallback = ("block_count", E_RESOURCE_LIMIT)
            feasible = False
            reasons.append("fused block count exceeds max_blocks")

    # Memory policy: shrink blocks first, then optionally lower precision, then
    # use the explicitly permitted separate fallback.
    if feasible and metrics["fused_estimated_resident_bytes"] > limits.max_resident_bytes:
        if limits.allow_block_shrink:
            candidate = effective_block
            while candidate > limits.min_block_size:
                candidate = max(limits.min_block_size, candidate // 2)
                candidate_metrics = estimate_metrics(task, block_size=candidate, dtype=effective_dtype)
                if candidate_metrics["fused_estimated_resident_bytes"] <= limits.max_resident_bytes:
                    effective_block = candidate
                    metrics = candidate_metrics
                    reasons.append(
                        f"shrunk block_size {requested_block}->{effective_block} to satisfy memory cap"
                    )
                    break
                if candidate == limits.min_block_size:
                    break
        if (
            limits.allow_precision_downgrade
            and metrics["fused_estimated_resident_bytes"] > limits.max_resident_bytes
            and task.dtype == "float64"
        ):
            effective_dtype = "float32"
            tier = "fp32-downgraded"
            metrics = estimate_metrics(task, block_size=effective_block, dtype=effective_dtype)
            reasons.append("lowered precision tier float64->float32 under memory pressure")
        if metrics["fused_estimated_resident_bytes"] > limits.max_resident_bytes:
            resource_fallback = ("memory", E_RESOURCE_LIMIT)
            feasible = False
            reasons.append("fused resident-memory estimate remains above cap")

    # Explicit policy priority.  Memory first means the extra fused block
    # working set wins over traversal savings; locality first protects the
    # reorder layout; balanced requires both a locality floor and bounded
    # memory overhead.
    policy_reason: str | None = None
    policy_code: str | None = None
    if feasible:
        if mode == "memory_first" and metrics["fused_single_block_memory_higher"]:
            policy_reason = "memory_first: fused block footprint exceeds separate block footprint"
            policy_code = E_POLICY_MEMORY
        elif mode == "locality_first" and metrics["locality_score"] < 0.5:
            policy_reason = (
                f"locality_first: scatter locality {metrics['locality_score']:.4f} < 0.5"
            )
            policy_code = E_POLICY_LOCALITY
        elif mode == "balanced":
            overhead = metrics["fused_block_resident_bytes"] / max(
                1, metrics["separate_block_resident_bytes"]
            )
            if metrics["locality_score"] < 0.5:
                policy_reason = (
                    f"balanced: scatter locality {metrics['locality_score']:.4f} < 0.5"
                )
                policy_code = E_POLICY_LOCALITY
            elif overhead > 2.0:
                policy_reason = f"balanced: fused/separate block overhead {overhead:.3f} > 2.0"
                policy_code = E_POLICY_MEMORY
        elif mode == "fusion_first":
            policy_reason = None
    if policy_reason:
        feasible = False
        reasons.append(policy_reason)
        upstream = policy_code

    # The float64 per-operator reference is mandatory for adjudication.  If it
    # cannot fit the resident-memory cap, overcommitting would violate the
    # resource contract even when a fused product itself would fit.
    separate_diag = _check_separate_resource(metrics, limits)
    if separate_diag is not None:
        separate_diag.case_id = task.case_id
        diagnostics.append(separate_diag)
        feasible = False
        if resource_fallback is None:
            resource_fallback = ("reference_memory", E_RESOURCE_LIMIT)
        reasons.append("mandatory float64 reference path exceeds max_resident_bytes")

    if diagnostics and any(d.severity == "error" for d in diagnostics):
        feasible = False

    if feasible:
        reasons.append(
            "fused: removes one intermediate write and one traversal while satisfying resource/policy gates"
        )
        path = "fused"
        fallback_category = None
        upstream_code = None
    else:
        path = "separate"
        if resource_fallback:
            fallback_category = resource_fallback[0]
            upstream_code = resource_fallback[1]
            if separate_diag is not None:
                reasons.append(
                    f"hard resource fallback ({fallback_category}) cannot be delegated"
                )
            elif limits.strict:
                diagnostics.append(
                    Diagnostic(
                        code=E_RESOURCE_LIMIT,
                        case_id=task.case_id,
                        stage="resource_plan",
                        message="strict resource limit rejected the case plan",
                        details={
                            "fallback_category": fallback_category,
                            "metrics": metrics,
                            "limits": limits.to_dict(),
                        },
                    )
                )
            else:
                diagnostics.append(
                    Diagnostic(
                        code=E_RESOURCE_LIMIT,
                        case_id=task.case_id,
                        stage="resource_plan",
                        message=f"resource pressure ({fallback_category}): explicit fallback to separate path",
                        severity="warning",
                        details={"metrics": metrics, "limits": limits.to_dict()},
                    )
                )
                reasons.append(f"resource fallback ({fallback_category}) to separate execution")
        elif policy_reason:
            fallback_category = "policy"
            upstream_code = policy_code
        elif pair is None:
            fallback_category = "infeasible"
            upstream_code = E_NO_ADJACENT_PAIR
        elif not pair.feasible:
            fallback_category = "infeasible"
            upstream_code = pair.reason_code or E_FUSION_INFEASIBLE
        else:
            fallback_category = "policy"
            upstream_code = E_FUSION_INFEASIBLE

    metrics.update(
        {
            "requested_block_size": requested_block,
            "effective_block_size": effective_block,
            "requested_dtype": task.dtype,
            "effective_dtype": effective_dtype,
            "precision_tier": tier,
            "fused_cache_key_inputs": {
                "includes_block_size": True,
                "includes_eps": True,
                "includes_axis_order": True,
                "includes_precision_tier": True,
            },
        }
    )
    return CasePlan(
        case_id=task.case_id,
        fuse=feasible and path == "fused",
        path=path,
        feasible=bool(pair and pair.feasible),
        requested_block_size=requested_block,
        effective_block_size=effective_block,
        effective_dtype=effective_dtype,
        precision_tier=tier,
        priority=priority,
        metrics=metrics,
        reasons=reasons,
        fallback_category=fallback_category,
        upstream_code=upstream_code,
        diagnostics=diagnostics,
        block_count=metrics["block_count"],
        waves=waves,
    )
