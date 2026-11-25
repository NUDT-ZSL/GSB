"""Deterministic resource budgeting and quality/memory/latency arbitration."""

from __future__ import annotations

import math
import resource as stdlib_resource
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .binning import estimate_boundary_comparisons
from .codes import EngineError, ErrorCode
from .hashing import canonical_hash


MI_B = 1024 * 1024


def read_runtime_memory() -> Dict[str, int]:
    """Return current self RSS and peak child RSS in bytes on Linux/WSL."""

    current_self_rss = 0
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                current_self_rss = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        current_self_rss = int(
            stdlib_resource.getrusage(stdlib_resource.RUSAGE_SELF).ru_maxrss
        ) * 1024

    try:
        max_child_rss = int(
            stdlib_resource.getrusage(stdlib_resource.RUSAGE_CHILDREN).ru_maxrss
        ) * 1024
    except (ValueError, OSError):
        max_child_rss = 0
    return {
        "current_self_rss_bytes": current_self_rss,
        "peak_child_rss_bytes": max_child_rss,
    }


def precision_digits(quant_scale: float) -> int:
    if quant_scale >= 1.0:
        return 0
    return max(0, int(math.ceil(-math.log10(float(quant_scale)) - 1e-12)))


def estimate_workload(
    *,
    row_count: int,
    specs: Mapping[str, Mapping[str, Any]],
    workers: int,
    batch_size: int,
    quant_scales: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    column_count = len(specs)
    bin_counts = {name: int(spec["bin_count"]) for name, spec in specs.items()}
    if quant_scales:
        for name, scale in quant_scales.items():
            if name in bin_counts:
                pass
    bin_count_sum = sum(bin_counts.values())
    feature_dimension = 0
    target_slots = 0
    token_width_total = 0
    for name, spec in specs.items():
        count = int(spec["bin_count"])
        method = spec["encoding_method"]
        scale = float((quant_scales or {}).get(name, spec["quant_scale"]))
        token_width = 4 + precision_digits(scale)
        if method == "one_hot":
            feature_dimension += count
            token_width_total += count * token_width
        else:
            feature_dimension += 1
            target_slots += count
            token_width_total += token_width

    batch_count = max(1, math.ceil(row_count / max(1, batch_size)))
    # Row assignment happens in workers; target statistics perform a second
    # assignment pass for target-encoded columns, so retain a conservative 2x.
    comparisons = estimate_boundary_comparisons(row_count, column_count, bin_count_sum)
    comparisons += row_count * bin_count_sum
    serialized_encoding_length = (
        row_count * token_width_total
        + target_slots * 12
        + sum(bin_counts.values()) * 24
    )

    baseline_rss = 18 * MI_B
    main_rss = row_count * (320 + 48 * column_count + 16 * feature_dimension)
    worker_rss = workers * (
        12 * MI_B
        + batch_size * (420 + 56 * column_count + 24 * feature_dimension)
    )
    estimated_memory = baseline_rss + main_rss + worker_rss

    work_units = max(1, comparisons)
    latency_units = math.ceil(work_units / max(1, workers)) + batch_count * 500
    return {
        "batch_count": batch_count,
        "feature_dimension": feature_dimension,
        "target_statistic_slots": target_slots,
        "bin_count_sum": bin_count_sum,
        "comparisons": comparisons,
        "encoding_length": serialized_encoding_length,
        "estimated_memory_bytes": estimated_memory,
        "latency_units": latency_units,
    }


def _candidate_workers(requested: int, slot_limit: int, policy: Mapping[str, Any]) -> List[int]:
    upper = min(requested, slot_limit)
    if not policy.get("allow_worker_reduction", True):
        return [requested] if requested <= slot_limit else []
    values = {upper}
    current = upper
    while current > 1:
        current = max(1, current // 2)
        values.add(current)
    values.add(1)
    return sorted(values, reverse=True)


def _candidate_batch_sizes(requested: int, policy: Mapping[str, Any]) -> List[int]:
    if not policy.get("allow_batch_reduction", True):
        return [requested]
    values = {requested}
    current = requested
    while current > 1:
        current = max(1, math.ceil(current / 2))
        values.add(current)
    values.add(1)
    return sorted(values, reverse=True)


def _candidate_bin_factors(policy: Mapping[str, Any]) -> List[float]:
    if not policy.get("allow_bin_reduction", True):
        return [1.0]
    return [1.0, 0.75, 0.5, 0.25]


def _candidate_quant_multipliers(policy: Mapping[str, Any]) -> List[int]:
    if not policy.get("allow_quant_coarsening", True):
        return [1]
    return [1, 2, 4, 8, 16]


def plan_resources(
    config: Mapping[str, Any],
    row_count: int,
    *,
    requested_workers: Optional[int] = None,
    requested_batch_size: Optional[int] = None,
) -> Dict[str, Any]:
    budget = config["resource_budget"]
    policy = config.get("degradation_policy", {})
    requested_workers = int(
        requested_workers
        if requested_workers is not None
        else config["scheduler"]["workers"]
    )
    requested_batch_size = int(
        requested_batch_size
        if requested_batch_size is not None
        else config["scheduler"]["batch_size"]
    )
    if requested_workers <= 0 or requested_batch_size <= 0:
        raise EngineError(
            ErrorCode.E_RESOURCE_LIMIT,
            "workers and batch_size must be positive",
            {"workers": requested_workers, "batch_size": requested_batch_size},
        )

    slot_limit = int(budget["max_parallel_slots"])
    priority = list(config.get("priority", ["quality", "memory", "latency"]))
    requested_bin_counts = {
        name: int(spec["bin_count"]) for name, spec in config["column_specs"].items()
    }
    requested_scales = {
        name: float(spec["quant_scale"]) for name, spec in config["column_specs"].items()
    }

    decisions: List[Dict[str, Any]] = []
    requested_usage = estimate_workload(
        row_count=row_count,
        specs=config["column_specs"],
        workers=requested_workers,
        batch_size=requested_batch_size,
    )
    requested_violations: List[str] = []
    if requested_workers > slot_limit:
        requested_violations.append("parallel_slots")
    if requested_usage["estimated_memory_bytes"] > budget["max_memory_bytes"]:
        requested_violations.append("memory")
    if requested_usage["comparisons"] > budget["max_comparisons"]:
        requested_violations.append("comparisons")
    if requested_usage["encoding_length"] > budget["max_encoding_length"]:
        requested_violations.append("encoding_length")

    baseline_workers = min(requested_workers, slot_limit)
    baseline_usage = estimate_workload(
        row_count=row_count,
        specs=config["column_specs"],
        workers=baseline_workers,
        batch_size=requested_batch_size,
    )
    baseline_violations: List[str] = []
    if baseline_usage["estimated_memory_bytes"] > budget["max_memory_bytes"]:
        baseline_violations.append("memory")
    if baseline_usage["comparisons"] > budget["max_comparisons"]:
        baseline_violations.append("comparisons")
    if baseline_usage["encoding_length"] > budget["max_encoding_length"]:
        baseline_violations.append("encoding_length")

    if not baseline_violations:
        if policy.get("on_limit", "degrade") == "error" and requested_violations:
            raise EngineError(
                ErrorCode.E_RESOURCE_LIMIT,
                "requested resource plan exceeds budget and on_limit policy is error",
                {
                    "violations": requested_violations,
                    "selected_alternative": {
                        "workers": baseline_workers,
                        "batch_size": requested_batch_size,
                    },
                },
            )
        if requested_workers > slot_limit:
            decisions.append(
                {
                    "code": ErrorCode.E_RESOURCE_LIMIT,
                    "status": "degraded",
                    "resource": "parallel_slots",
                    "requested": requested_workers,
                    "effective": baseline_workers,
                    "slot_limit": slot_limit,
                    "reason": "requested workers exceed max_parallel_slots",
                    "alternatives": ["cap_workers", "queue_tasks"],
                    "decision_basis": f"max_parallel_slots={slot_limit}",
                }
            )
        plan = {
            "ok": True,
            "code": ErrorCode.E_RESOURCE_LIMIT if decisions else ErrorCode.OK,
            "requested": {
                "workers": requested_workers,
                "batch_size": requested_batch_size,
                "bin_counts": requested_bin_counts,
                "quant_scales": requested_scales,
            },
            "workers": baseline_workers,
            "batch_size": requested_batch_size,
            "bin_counts": requested_bin_counts,
            "quant_scales": requested_scales,
            "batch_count": baseline_usage["batch_count"],
            "estimates": baseline_usage,
            "budget": dict(budget),
            "priority": list(priority),
            "decisions": decisions,
            "alternatives_considered": 1,
            "feasible_count": 1,
        }
        plan["plan_id"] = canonical_hash(
            {key: value for key, value in plan.items() if key != "plan_id"}
        )
        return plan

    candidates: List[Dict[str, Any]] = []
    if "memory" in baseline_violations:
        worker_values = _candidate_workers(baseline_workers, slot_limit, policy)
        batch_values = _candidate_batch_sizes(requested_batch_size, policy)
    else:
        # Worker and batch sizing do not change total comparison work; keep the
        # requested scheduling shape when only comparison/length must be fixed.
        worker_values = [baseline_workers]
        batch_values = [requested_batch_size]
    if not worker_values:
        raise EngineError(
            ErrorCode.E_RESOURCE_LIMIT,
            "worker count exceeds parallel slot limit and worker reduction is disabled",
            {"requested_workers": requested_workers, "max_parallel_slots": slot_limit},
        )

    for workers in worker_values:
        for batch_size in batch_values:
            for bin_factor in _candidate_bin_factors(policy):
                bin_counts = {
                    name: max(1, int(math.floor(count * bin_factor)))
                    for name, count in requested_bin_counts.items()
                }
                candidate_specs: Dict[str, Dict[str, Any]] = {}
                for name, spec in config["column_specs"].items():
                    candidate_specs[name] = dict(spec)
                    candidate_specs[name]["bin_count"] = bin_counts[name]
                for quant_multiplier in _candidate_quant_multipliers(policy):
                    quant_scales = {
                        name: scale * quant_multiplier
                        for name, scale in requested_scales.items()
                    }
                    usage = estimate_workload(
                        row_count=row_count,
                        specs=candidate_specs,
                        workers=workers,
                        batch_size=batch_size,
                        quant_scales=quant_scales,
                    )
                    violations = []
                    if workers > slot_limit:
                        violations.append("parallel_slots")
                    if usage["estimated_memory_bytes"] > budget["max_memory_bytes"]:
                        violations.append("memory")
                    if usage["comparisons"] > budget["max_comparisons"]:
                        violations.append("comparisons")
                    if usage["encoding_length"] > budget["max_encoding_length"]:
                        violations.append("encoding_length")

                    bin_loss = (
                        sum(requested_bin_counts[name] - bin_counts[name] for name in bin_counts)
                        / max(1, sum(requested_bin_counts.values()))
                    )
                    quality_loss = bin_loss + 0.05 * math.log2(quant_multiplier)
                    candidates.append(
                        {
                            "workers": workers,
                            "batch_size": batch_size,
                            "bin_counts": bin_counts,
                            "quant_scales": quant_scales,
                            "quant_multiplier": quant_multiplier,
                            "bin_factor": bin_factor,
                            "usage": usage,
                            "violations": violations,
                            "feasible": not violations,
                            "quality_loss": quality_loss,
                        }
                    )

    feasible = [candidate for candidate in candidates if candidate["feasible"]]
    if not feasible:
        minimal = min(
            candidates,
            key=lambda item: (
                item["usage"]["estimated_memory_bytes"],
                item["usage"]["comparisons"],
            ),
        )
        error = EngineError(
            ErrorCode.E_RESOURCE_LIMIT,
            "no resource-degraded configuration can satisfy the declared budget",
            {
                "budget": budget,
                "minimum_estimate": minimal["usage"],
                "minimum_violations": minimal["violations"],
                "requested": {
                    "workers": requested_workers,
                    "batch_size": requested_batch_size,
                    "bin_counts": requested_bin_counts,
                    "quant_scales": requested_scales,
                },
            },
        )
        if policy.get("on_limit", "degrade") == "error":
            raise error
        raise error

    def sort_key(candidate: Mapping[str, Any]):
        usage = candidate["usage"]
        metrics = {
            "quality": candidate["quality_loss"],
            "memory": usage["estimated_memory_bytes"],
            "latency": usage["latency_units"],
        }
        return tuple(metrics[item] for item in priority)

    selected = min(feasible, key=sort_key)

    if policy.get("on_limit", "degrade") == "error":
        requested_usage = estimate_workload(
            row_count=row_count,
            specs=config["column_specs"],
            workers=min(requested_workers, slot_limit),
            batch_size=requested_batch_size,
        )
        requested_violations = []
        if requested_workers > slot_limit:
            requested_violations.append("parallel_slots")
        if requested_usage["estimated_memory_bytes"] > budget["max_memory_bytes"]:
            requested_violations.append("memory")
        if requested_usage["comparisons"] > budget["max_comparisons"]:
            requested_violations.append("comparisons")
        if requested_usage["encoding_length"] > budget["max_encoding_length"]:
            requested_violations.append("encoding_length")
        if requested_violations:
            raise EngineError(
                ErrorCode.E_RESOURCE_LIMIT,
                "requested resource plan exceeds budget and on_limit policy is error",
                {
                    "violations": requested_violations,
                    "requested_estimate": requested_usage,
                    "budget": budget,
                    "selected_alternative": {
                        "workers": selected["workers"],
                        "batch_size": selected["batch_size"],
                        "bin_counts": selected["bin_counts"],
                        "quant_scales": selected["quant_scales"],
                    },
                },
            )

    if selected["workers"] < requested_workers or requested_workers > slot_limit:
        decisions.append(
            {
                "code": ErrorCode.E_RESOURCE_LIMIT,
                "status": "degraded",
                "resource": "parallel_slots"
                if requested_workers > slot_limit
                else "workers",
                "requested": requested_workers,
                "effective": selected["workers"],
                "slot_limit": slot_limit,
                "reason": "parallelism exceeded slot limit or was reduced for resource budget",
                "alternatives": ["cap_workers", "reduce_batch_size", "reduce_bin_count"],
                "decision_basis": f"priority={priority}; max_parallel_slots={slot_limit}",
            }
        )
    if selected["batch_size"] < requested_batch_size:
        decisions.append(
            {
                "code": ErrorCode.E_RESOURCE_LIMIT,
                "status": "degraded",
                "resource": "batch_size",
                "requested": requested_batch_size,
                "effective": selected["batch_size"],
                "reason": "reduced batch size to lower resident memory",
                "alternatives": ["reduce_workers", "reduce_bin_count"],
                "decision_basis": f"priority={priority}",
            }
        )
    for name, effective_count in selected["bin_counts"].items():
        requested_count = requested_bin_counts[name]
        if effective_count < requested_count:
            decisions.append(
                {
                    "code": ErrorCode.E_RESOURCE_LIMIT,
                    "status": "degraded",
                    "resource": "bin_count",
                    "column": name,
                    "requested": requested_count,
                    "effective": effective_count,
                    "reason": "reduced bin count to satisfy comparison/storage/memory budget",
                    "alternatives": ["reduce_workers", "reduce_batch_size"],
                    "decision_basis": f"priority={priority}",
                }
            )
    for name, effective_scale in selected["quant_scales"].items():
        requested_scale = requested_scales[name]
        if effective_scale > requested_scale:
            decisions.append(
                {
                    "code": ErrorCode.E_RESOURCE_LIMIT,
                    "status": "degraded",
                    "resource": "quant_scale",
                    "column": name,
                    "requested": requested_scale,
                    "effective": effective_scale,
                    "reason": "coarsened quantization to shorten encoded representation",
                    "alternatives": ["reduce_bin_count", "reduce_batch_size"],
                    "decision_basis": f"priority={priority}",
                }
            )

    plan = {
        "ok": True,
        "code": ErrorCode.E_RESOURCE_LIMIT if decisions else ErrorCode.OK,
        "requested": {
            "workers": requested_workers,
            "batch_size": requested_batch_size,
            "bin_counts": requested_bin_counts,
            "quant_scales": requested_scales,
        },
        "workers": selected["workers"],
        "batch_size": selected["batch_size"],
        "bin_counts": selected["bin_counts"],
        "quant_scales": selected["quant_scales"],
        "batch_count": selected["usage"]["batch_count"],
        "estimates": selected["usage"],
        "budget": dict(budget),
        "priority": list(priority),
        "decisions": decisions,
        "alternatives_considered": len(candidates),
        "feasible_count": len(feasible),
    }
    plan["plan_id"] = canonical_hash({k: v for k, v in plan.items() if k != "plan_id"})
    return plan
