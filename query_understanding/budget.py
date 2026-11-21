"""Comparison and resident-memory admission control."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import E_MATCH_BUDGET, E_RESOURCE_LIMIT


@dataclass(frozen=True)
class BudgetPolicy:
    total_compare_budget: int = 2_000_000
    memory_limit_mb: float = 512.0
    approximate_candidate_limit: int = 48
    minimum_batch_size: int = 8
    priority_order: tuple[str, ...] = ("precision", "memory", "progress")

    @classmethod
    def from_dict(cls, row: dict[str, Any] | None) -> "BudgetPolicy":
        row = row or {}
        return cls(
            total_compare_budget=int(row.get("total_compare_budget", 2_000_000)),
            memory_limit_mb=float(row.get("memory_limit_mb", 512.0)),
            approximate_candidate_limit=int(
                row.get("approximate_candidate_limit", 48)
            ),
            minimum_batch_size=int(row.get("minimum_batch_size", 8)),
            priority_order=tuple(
                row.get("priority_order", ["precision", "memory", "progress"])
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_compare_budget": self.total_compare_budget,
            "memory_limit_mb": self.memory_limit_mb,
            "approximate_candidate_limit": self.approximate_candidate_limit,
            "minimum_batch_size": self.minimum_batch_size,
            "priority_order": list(self.priority_order),
        }


def estimate_path_cost(observation: dict[str, Any], path: str, policy: BudgetPolicy) -> dict[str, Any]:
    query_count = int(observation["query_count"])
    dictionary_entries = int(observation["dictionary_entry_count"])
    avg_tokens = float(observation["avg_tokens"])
    if path == "exact":
        candidate_count = dictionary_entries
    elif path == "pruned_approx":
        candidate_count = min(dictionary_entries, policy.approximate_candidate_limit)
    else:
        raise ValueError(f"unknown path: {path}")
    token_factor = max(1, int(math.ceil(avg_tokens)))
    per_query_comparisons = max(1, candidate_count * token_factor)
    compare_total = query_count * per_query_comparisons

    base_memory_mb = 4.0 + dictionary_entries * 0.0008
    per_query_memory_mb = 0.004 + avg_tokens * 0.00025
    memory_total_mb = base_memory_mb + query_count * per_query_memory_mb
    available_memory_mb = float(observation["memory_available_mb"])
    compare_capacity = (
        policy.total_compare_budget // per_query_comparisons
        if per_query_comparisons > 0
        else query_count
    )
    usable_memory_mb = max(0.0, available_memory_mb - base_memory_mb)
    memory_capacity = (
        int(usable_memory_mb // per_query_memory_mb)
        if per_query_memory_mb > 0
        else query_count
    )
    return {
        "path": path,
        "candidate_count_per_query": candidate_count,
        "token_factor": token_factor,
        "per_query_comparisons": per_query_comparisons,
        "estimated_comparisons_total": compare_total,
        "compare_budget": policy.total_compare_budget,
        "base_memory_mb": round(base_memory_mb, 6),
        "per_query_memory_mb": round(per_query_memory_mb, 6),
        "estimated_memory_total_mb": round(memory_total_mb, 6),
        "memory_available_mb": round(available_memory_mb, 6),
        "compare_capacity_queries": compare_capacity,
        "memory_capacity_queries": max(0, memory_capacity),
        "formula": {
            "comparisons": "query_count * candidate_count * ceil(avg_tokens)",
            "memory": "4MB + dict_entries*0.0008MB + query_count*(0.004MB + avg_tokens*0.00025MB)",
        },
    }


def admit_budget(
    observation: dict[str, Any],
    path: str,
    policy: BudgetPolicy,
    *,
    allow_downbatch: bool = True,
) -> dict[str, Any]:
    query_count = int(observation["query_count"])
    cost = estimate_path_cost(observation, path, policy)
    capacity = min(
        query_count,
        int(cost["compare_capacity_queries"]),
        int(cost["memory_capacity_queries"]),
    )
    limiting = "comparisons"
    if int(cost["memory_capacity_queries"]) < int(cost["compare_capacity_queries"]):
        limiting = "memory"

    base = {
        "path": path,
        "requested_query_count": query_count,
        "cost": cost,
        "priority_order": list(policy.priority_order),
        "priority_interpretation": {
            "precision": "never shrink candidate_count to fit; reduce batch instead",
            "memory": "never exceed memory_available_mb; split batch before rejection",
            "progress": "process at least minimum_batch_size when feasible",
        },
    }
    if capacity >= query_count:
        return {
            **base,
            "ok": True,
            "status": "accepted",
            "decision_code": "ADMITTED_FULL_BATCH",
            "error_code": None,
            "batch_size": query_count,
            "batch_count": 1,
            "reasons": [
                "estimated comparisons and memory fit the configured limits",
                "full query set is admitted without precision loss",
            ],
        }
    if allow_downbatch and capacity >= policy.minimum_batch_size:
        return {
            **base,
            "ok": True,
            "status": "downbatch",
            "decision_code": "ADMITTED_DOWNBATCH",
            "error_code": None,
            "batch_size": capacity,
            "batch_count": int(math.ceil(query_count / capacity)),
            "reasons": [
                f"{limiting} limit binds before the full query set",
                "precision is preserved by keeping the candidate set unchanged",
                f"progress is preserved by admitting batches of {capacity}",
            ],
        }

    compare_capacity = int(cost["compare_capacity_queries"])
    memory_capacity = int(cost["memory_capacity_queries"])
    if not allow_downbatch and capacity >= policy.minimum_batch_size:
        code = E_MATCH_BUDGET if limiting == "comparisons" else E_RESOURCE_LIMIT
        reasons = [
            f"{limiting} capacity admits only {capacity} of {query_count} queries",
            "downbatching is disabled, so the requested single batch is rejected",
            "candidate precision was not silently reduced",
        ]
    elif memory_capacity < policy.minimum_batch_size and compare_capacity >= policy.minimum_batch_size:
        code = E_RESOURCE_LIMIT
        reasons = [
            "memory capacity is below the minimum batch size",
            "cannot make progress without exceeding the resident-memory envelope",
        ]
    elif compare_capacity < policy.minimum_batch_size and memory_capacity >= policy.minimum_batch_size:
        code = E_MATCH_BUDGET
        reasons = [
            "comparison capacity is below the minimum batch size",
            "cannot make progress without exceeding the total comparison budget",
        ]
    else:
        code = E_RESOURCE_LIMIT if "memory" in policy.priority_order[:2] else E_MATCH_BUDGET
        reasons = [
            "both comparison and memory capacities are below the minimum batch size",
            f"priority order {list(policy.priority_order)} selects {code}",
        ]
    return {
        **base,
        "ok": False,
        "status": "rejected",
        "decision_code": code,
        "error_code": code,
        "batch_size": 0,
        "batch_count": 0,
        "limiting_resource": limiting,
        "reasons": reasons,
    }

