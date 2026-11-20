"""Deterministic comparison/memory admission and degradation planning."""

from __future__ import annotations

import copy
import math
from typing import Any, Callable

from .config import CandidateSpec, EngineConfig, METHOD_ORDER

PLANNER_VERSION = "budget-planner-v1"


def _chunk(n_rows: int, requested: int) -> int:
    return max(1, min(int(requested), n_rows))


def estimate_resources(
    *,
    method: str,
    params: dict[str, Any],
    n_rows: int,
    n_features: int,
    row_chunk_size: int,
) -> dict[str, Any]:
    chunk = _chunk(n_rows, row_chunk_size)
    base_data = n_rows * n_features * 8
    score_array = n_rows * 8

    if method == "isolation_forest":
        n_trees = int(params["n_trees"])
        sample_size = min(int(params["sample_size"]), n_rows)
        depth = int(params.get("max_depth", 0))
        if depth <= 0:
            depth = max(1, int(math.ceil(math.log2(max(2, sample_size)))) + 1)
        nodes_per_tree = max(1, 2 * sample_size)
        tree_memory = n_trees * nodes_per_tree * 48
        traversal_memory = chunk * 8 + score_array
        memory = base_data + tree_memory + traversal_memory + 4096
        comparisons = n_trees * sample_size * max(1, depth) + n_rows * max(1, depth)
        components = {
            "input_matrix_bytes": base_data,
            "tree_nodes_bytes": tree_memory,
            "score_and_traversal_bytes": traversal_memory + 4096,
        }
        return {
            "method": method,
            "comparisons": int(comparisons),
            "memory_bytes": int(memory),
            "row_chunk_size": chunk,
            "depth_used": depth,
            "components": components,
            "comparison_formula": "n_trees * sample_size * depth + n_rows * depth",
            "memory_formula": "input + n_trees*(2*sample_size)*48 + chunk*8 + scores + 4096",
            "estimate_version": "analytic-v1",
        }

    if method == "knn":
        n_neighbors = int(params["n_neighbors"])
        distance_block = chunk * n_rows * 8
        neighbor_buffer = n_rows + n_neighbors
        memory = base_data + distance_block + score_array + neighbor_buffer * 8 + 4096
        comparisons = n_rows * (n_rows - 1) + n_rows * n_neighbors
        components = {
            "input_matrix_bytes": base_data,
            "distance_block_bytes": distance_block,
            "score_and_neighbor_buffers_bytes": score_array + neighbor_buffer * 8 + 4096,
        }
        return {
            "method": method,
            "comparisons": int(comparisons),
            "memory_bytes": int(memory),
            "row_chunk_size": chunk,
            "components": components,
            "comparison_formula": "n_rows*(n_rows-1) + n_rows*n_neighbors",
            "memory_formula": "input + chunk*n_rows*8 + scores + neighbor buffers + 4096",
            "estimate_version": "analytic-v1",
        }

    if method == "mahalanobis":
        covariance_bytes = n_features * n_features * 8
        inverse_bytes = n_features * n_features * 8
        work_bytes = n_features * n_features * 8
        chunk_bytes = chunk * n_features * 8
        memory = base_data + covariance_bytes + inverse_bytes + work_bytes + chunk_bytes + score_array + 4096
        comparisons = n_rows * n_rows * n_features + n_rows * n_features + n_features**3
        components = {
            "input_matrix_bytes": base_data,
            "covariance_bytes": covariance_bytes,
            "inverse_covariance_bytes": inverse_bytes,
            "inverse_workspace_bytes": work_bytes,
            "chunk_workspace_bytes": chunk_bytes + score_array + 4096,
        }
        return {
            "method": method,
            "comparisons": int(comparisons),
            "memory_bytes": int(memory),
            "row_chunk_size": chunk,
            "components": components,
            "comparison_formula": "n_rows*n_rows*n_features + n_rows*n_features + n_features^3",
            "memory_formula": "input + covariance + inverse + inverse workspace + chunk workspace",
            "estimate_version": "analytic-v1",
        }

    raise ValueError(f"unsupported method {method}")


def _quality_proxy(method: str, params: dict[str, Any], n_rows: int) -> tuple[float, str]:
    if method == "isolation_forest":
        sample_size = min(int(params["sample_size"]), n_rows)
        depth = max(1, int(math.ceil(math.log2(max(2, sample_size)))) + 1)
        return (
            float(params["n_trees"] * sample_size * depth),
            "n_trees * sample_size * effective_depth",
        )
    if method == "knn":
        return float(params["n_neighbors"]), "n_neighbors (higher k is quality-preferred)"
    return 1.0, "full covariance sensitivity baseline"


def _priority_key(
    priority_order: tuple[str, ...],
    *,
    quality: float,
    memory: int,
    comparisons: int,
    method: str,
    candidate_index: int,
    task_id: str,
) -> tuple[Any, ...]:
    key: list[Any] = []
    for token in priority_order:
        if token == "quality":
            key.append(-quality)
        elif token == "memory":
            key.append(memory)
        elif token == "progress":
            key.append(comparisons)
    key.extend([METHOD_ORDER[method], candidate_index, task_id])
    return tuple(key)


def _decision_base(
    candidate: CandidateSpec,
    *,
    original_params: dict[str, Any],
    original_estimate: dict[str, Any],
) -> dict[str, Any]:
    quality, basis = _quality_proxy(candidate.method, original_params, 1)
    return {
        "candidate_id": candidate.candidate_id,
        "task_id": candidate.candidate_id,
        "method": candidate.method,
        "candidate_index": candidate.candidate_index,
        "seed": candidate.seed,
        "original_params": copy.deepcopy(original_params),
        "quality_proxy": quality,
        "quality_proxy_basis": basis,
        "original_estimate": original_estimate,
        "degradations": [],
    }


def _degrade_memory(
    candidate: CandidateSpec,
    *,
    params: dict[str, Any],
    row_chunk_size: int,
    n_rows: int,
    n_features: int,
    max_memory_bytes: int,
) -> tuple[dict[str, Any] | None, int, list[dict[str, Any]], dict[str, Any]]:
    estimate = estimate_resources(
        method=candidate.method,
        params=params,
        n_rows=n_rows,
        n_features=n_features,
        row_chunk_size=row_chunk_size,
    )
    if estimate["memory_bytes"] <= max_memory_bytes:
        return params, row_chunk_size, [], estimate

    reasons: list[dict[str, Any]] = []
    if candidate.method == "isolation_forest":
        original_trees = int(params["n_trees"])
        for trees in range(original_trees - 1, 0, -1):
            reduced = {**params, "n_trees": trees}
            reduced_estimate = estimate_resources(
                method=candidate.method,
                params=reduced,
                n_rows=n_rows,
                n_features=n_features,
                row_chunk_size=row_chunk_size,
            )
            if reduced_estimate["memory_bytes"] <= max_memory_bytes:
                reasons.append(
                    {
                        "code": "E_MEMORY_LIMIT",
                        "stage": "admission",
                        "action": "degraded",
                        "field": "n_trees",
                        "before": original_trees,
                        "after": trees,
                        "reason": "reduced tree count to fit resident-memory limit",
                    }
                )
                return reduced, row_chunk_size, reasons, reduced_estimate

    if candidate.method == "knn":
        original_chunk = row_chunk_size
        for chunk in range(min(row_chunk_size, n_rows) - 1, 0, -1):
            reduced_estimate = estimate_resources(
                method=candidate.method,
                params=params,
                n_rows=n_rows,
                n_features=n_features,
                row_chunk_size=chunk,
            )
            if reduced_estimate["memory_bytes"] <= max_memory_bytes:
                reasons.append(
                    {
                        "code": "E_MEMORY_LIMIT",
                        "stage": "admission",
                        "action": "degraded",
                        "field": "row_chunk_size",
                        "before": original_chunk,
                        "after": chunk,
                        "reason": "reduced distance block size to fit resident-memory limit",
                    }
                )
                return params, chunk, reasons, reduced_estimate

    reasons.append(
        {
            "code": "E_MEMORY_LIMIT",
            "stage": "admission",
            "action": "rejected",
            "field": "budgets.max_memory_bytes",
            "required_bytes": estimate["memory_bytes"],
            "limit_bytes": max_memory_bytes,
            "reason": "no deterministic degradation can fit the resident-memory limit",
        }
    )
    return None, row_chunk_size, reasons, estimate


def _degrade_comparisons(
    candidate: CandidateSpec,
    *,
    params: dict[str, Any],
    row_chunk_size: int,
    n_rows: int,
    n_features: int,
    remaining: int,
) -> tuple[dict[str, Any] | None, dict[str, Any], list[dict[str, Any]]]:
    current = estimate_resources(
        method=candidate.method,
        params=params,
        n_rows=n_rows,
        n_features=n_features,
        row_chunk_size=row_chunk_size,
    )
    if current["comparisons"] <= remaining:
        return params, current, []

    if candidate.method == "isolation_forest":
        original_trees = int(params["n_trees"])
        for trees in range(original_trees - 1, 0, -1):
            reduced = {**params, "n_trees": trees}
            reduced_estimate = estimate_resources(
                method=candidate.method,
                params=reduced,
                n_rows=n_rows,
                n_features=n_features,
                row_chunk_size=row_chunk_size,
            )
            if reduced_estimate["comparisons"] <= remaining:
                reason = {
                    "code": "E_COMPARE_BUDGET",
                    "stage": "admission",
                    "action": "degraded",
                    "field": "n_trees",
                    "before": original_trees,
                    "after": trees,
                    "remaining_comparisons": remaining,
                    "reason": "reduced tree count to fit remaining comparison budget",
                }
                return reduced, reduced_estimate, [reason]

    if candidate.method == "knn":
        original_neighbors = int(params["n_neighbors"])
        for neighbors in range(original_neighbors - 1, 0, -1):
            reduced = {**params, "n_neighbors": neighbors}
            reduced_estimate = estimate_resources(
                method=candidate.method,
                params=reduced,
                n_rows=n_rows,
                n_features=n_features,
                row_chunk_size=row_chunk_size,
            )
            if reduced_estimate["comparisons"] <= remaining:
                reason = {
                    "code": "E_COMPARE_BUDGET",
                    "stage": "admission",
                    "action": "degraded",
                    "field": "n_neighbors",
                    "before": original_neighbors,
                    "after": neighbors,
                    "remaining_comparisons": remaining,
                    "reason": "reduced neighbor count to fit remaining comparison budget",
                }
                return reduced, reduced_estimate, [reason]

    return None, current, []


def build_plan(config: EngineConfig, *, n_rows: int, n_features: int) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    memory_ok: list[tuple[CandidateSpec, dict[str, Any], dict[str, Any], int, list[dict[str, Any]]]] = []

    for candidate in config.candidates:
        original_params = copy.deepcopy(candidate.params)
        original_estimate = estimate_resources(
            method=candidate.method,
            params=original_params,
            n_rows=n_rows,
            n_features=n_features,
            row_chunk_size=config.batch_size,
        )
        decision = _decision_base(
            candidate,
            original_params=original_params,
            original_estimate=original_estimate,
        )
        quality, basis = _quality_proxy(candidate.method, candidate.params, n_rows)
        decision["quality_proxy"] = quality
        decision["quality_proxy_basis"] = basis

        effective_params, row_chunk, reasons, estimate = _degrade_memory(
            candidate,
            params=copy.deepcopy(candidate.params),
            row_chunk_size=config.batch_size,
            n_rows=n_rows,
            n_features=n_features,
            max_memory_bytes=config.max_memory_bytes,
        )
        if effective_params is None:
            decision["status"] = "rejected"
            decision["effective_params"] = None
            decision["effective_estimate"] = estimate
            decision["degradations"] = reasons
            decision["codes"] = ["E_MEMORY_LIMIT"]
            decisions.append(decision)
            continue
        decisions.append(decision)
        memory_ok.append((candidate, effective_params, estimate, row_chunk, reasons))

    anchors: list[tuple[CandidateSpec, dict[str, Any], dict[str, Any], int, list[dict[str, Any]]]] = []
    for method in ("isolation_forest", "knn", "mahalanobis"):
        same_method = [entry for entry in memory_ok if entry[0].method == method]
        if not same_method:
            continue
        anchor = max(
            same_method,
            key=lambda entry: (
                _quality_proxy(entry[0].method, entry[1], n_rows)[0],
                -entry[2]["comparisons"],
                -entry[0].candidate_index,
                entry[0].candidate_id,
            ),
        )
        anchors.append(anchor)

    def selection_key(entry: tuple[CandidateSpec, dict[str, Any], dict[str, Any], int, list[dict[str, Any]]]) -> tuple[Any, ...]:
        candidate, params, estimate, _, _ = entry
        quality, _ = _quality_proxy(candidate.method, params, n_rows)
        return _priority_key(
            config.priority_order,
            quality=quality,
            memory=estimate["memory_bytes"],
            comparisons=estimate["comparisons"],
            method=candidate.method,
            candidate_index=candidate.candidate_index,
            task_id=candidate.candidate_id,
        )

    anchors.sort(key=selection_key)
    anchor_ids = {entry[0].candidate_id for entry in anchors}
    remainder = [entry for entry in memory_ok if entry[0].candidate_id not in anchor_ids]
    remainder.sort(key=selection_key)
    selection_order = anchors + remainder

    used = 0
    admitted: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    decisions_by_id = {decision["candidate_id"]: decision for decision in decisions}

    for candidate, params, memory_estimate, row_chunk, memory_reasons in selection_order:
        selected_ids.add(candidate.candidate_id)
        decision = decisions_by_id[candidate.candidate_id]
        remaining = config.max_comparisons - used
        current_estimate = estimate_resources(
            method=candidate.method,
            params=params,
            n_rows=n_rows,
            n_features=n_features,
            row_chunk_size=row_chunk,
        )
        if current_estimate["comparisons"] <= remaining:
            effective_params = params
            estimate = current_estimate
            compare_reasons: list[dict[str, Any]] = []
        else:
            effective_params, estimate, compare_reasons = _degrade_comparisons(
                candidate,
                params=params,
                row_chunk_size=row_chunk,
                n_rows=n_rows,
                n_features=n_features,
                remaining=remaining,
            )
            if effective_params is None:
                decision["status"] = "rejected"
                decision["effective_params"] = None
                decision["effective_estimate"] = current_estimate
                decision["degradations"] = memory_reasons + [
                    {
                        "code": "E_COMPARE_BUDGET",
                        "stage": "admission",
                        "action": "rejected",
                        "field": "budgets.max_comparisons",
                        "required_comparisons": current_estimate["comparisons"],
                        "remaining_comparisons": remaining,
                        "reason": "no deterministic degradation fits the remaining comparison budget",
                    }
                ]
                decision["codes"] = sorted(
                    {reason["code"] for reason in decision["degradations"]}
                )
                continue

        used += int(estimate["comparisons"])
        degradations = memory_reasons + compare_reasons
        decision["status"] = "degraded" if degradations else "admitted"
        decision["effective_params"] = copy.deepcopy(effective_params)
        decision["effective_estimate"] = estimate
        decision["degradations"] = degradations
        decision["codes"] = sorted({reason["code"] for reason in degradations})
        quality, basis = _quality_proxy(candidate.method, effective_params, n_rows)
        task = {
            "task_id": candidate.candidate_id,
            "candidate_id": candidate.candidate_id,
            "method": candidate.method,
            "candidate_index": candidate.candidate_index,
            "params": copy.deepcopy(effective_params),
            "original_params": copy.deepcopy(candidate.params),
            "seed": candidate.seed,
            "row_chunk_size": row_chunk,
            "comparison_estimate": int(estimate["comparisons"]),
            "memory_estimate": int(estimate["memory_bytes"]),
            "quality_proxy": quality,
            "quality_proxy_basis": basis,
            "degraded": bool(degradations),
            "degradations": copy.deepcopy(degradations),
        }
        decision["task_id"] = task["task_id"]
        decision["row_chunk_size"] = row_chunk
        admitted.append(task)

    for decision in decisions:
        if decision["candidate_id"] not in selected_ids and decision["status"] != "rejected":
            remaining = config.max_comparisons - used
            decision["status"] = "rejected"
            decision["effective_params"] = None
            decision["degradations"].append(
                {
                    "code": "E_COMPARE_BUDGET",
                    "stage": "admission",
                    "action": "rejected",
                    "field": "budgets.max_comparisons",
                    "remaining_comparisons": remaining,
                    "reason": "candidate could not be scheduled inside the deterministic priority path",
                }
            )
            decision["codes"] = sorted({reason["code"] for reason in decision["degradations"]})

    dispatch_order = [task["task_id"] for task in admitted]
    batches: list[dict[str, Any]] = []
    current_batch: list[dict[str, Any]] = []
    current_memory = 0
    for task in admitted:
        if current_batch and (
            len(current_batch) >= config.workers
            or current_memory + task["memory_estimate"] > config.max_memory_bytes
        ):
            batches.append(_finish_batch(len(batches), current_batch, current_memory))
            current_batch = []
            current_memory = 0
        current_batch.append(task)
        current_memory += task["memory_estimate"]
    if current_batch:
        batches.append(_finish_batch(len(batches), current_batch, current_memory))

    for batch in batches:
        for task_id in batch["task_ids"]:
            task = next(task for task in admitted if task["task_id"] == task_id)
            task["batch_index"] = batch["batch_index"]
            decisions_by_id[task_id]["batch_index"] = batch["batch_index"]

    rejected = [decision for decision in decisions if decision["status"] == "rejected"]
    degraded = [decision for decision in decisions if decision["status"] == "degraded"]
    codes = sorted(
        {
            code
            for decision in decisions
            for code in decision.get("codes", [])
        }
    )
    priority_descriptions = {
        "quality": "prefer larger quality proxy first",
        "memory": "prefer smaller estimated resident memory",
        "progress": "prefer fewer estimated comparisons",
    }
    return {
        "planner_version": PLANNER_VERSION,
        "dataset": {"n_rows": n_rows, "n_features": n_features},
        "budgets": {
            "max_comparisons": config.max_comparisons,
            "max_memory_bytes": config.max_memory_bytes,
        },
        "execution": {
            "workers": config.workers,
            "batch_size": config.batch_size,
            "batch_size_semantics": "maximum sample rows per algorithm chunk",
            "start_method": config.start_method,
        },
        "priority_rule": {
            "order": list(config.priority_order),
            "comparison_key_order": [
                priority_descriptions[token] for token in config.priority_order
            ],
            "anchor_policy": "one highest-quality memory-safe anchor per method, then remaining candidates",
            "tie_breakers": ["method order", "candidate index", "candidate_id"],
            "degradation_policy": {
                "isolation_forest": "reduce n_trees first for comparison budget",
                "knn": "reduce row_chunk_size for memory, n_neighbors for comparison budget",
                "mahalanobis": "no approximation; reject when full inverse cannot fit",
            },
        },
        "decisions": decisions,
        "tasks": admitted,
        "admitted_task_ids": dispatch_order,
        "rejected_task_ids": [decision["candidate_id"] for decision in rejected],
        "degraded_task_ids": [decision["candidate_id"] for decision in degraded],
        "dispatch_order": dispatch_order,
        "execution_batches": batches,
        "totals": {
            "candidate_count": len(decisions),
            "admitted_count": len(admitted),
            "rejected_count": len(rejected),
            "degraded_count": len(degraded),
            "estimated_comparisons": used,
            "remaining_comparisons": config.max_comparisons - used,
            "estimated_peak_batch_memory_bytes": max(
                (batch["estimated_memory_bytes"] for batch in batches), default=0
            ),
        },
        "business_codes": codes,
    }


def _finish_batch(batch_index: int, tasks: list[dict[str, Any]], memory: int) -> dict[str, Any]:
    return {
        "batch_index": batch_index,
        "task_ids": [task["task_id"] for task in tasks],
        "estimated_memory_bytes": int(memory),
        "estimated_comparisons": int(sum(task["comparison_estimate"] for task in tasks)),
    }
