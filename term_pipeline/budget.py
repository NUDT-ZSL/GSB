"""Comparison and resident-cache budget admission planning."""

from __future__ import annotations

from typing import Any

from .errors import E_COMPARE_BUDGET, E_RESOURCE_LIMIT, PipelineError
from .features import document_feature_counts
from .util import rounded


def _doc_estimate(
    doc: dict[str, Any],
    candidate_count: int,
    feature_count: int,
    budget: dict[str, Any],
) -> dict[str, Any]:
    comparisons = candidate_count * max(1, feature_count)
    compare_cost = (
        candidate_count * budget["candidate_compare_cost"]
        + feature_count * budget["feature_compare_cost"]
    )
    resident_bytes = (
        budget["fixed_doc_bytes"]
        + candidate_count * budget["bytes_per_candidate"]
        + feature_count * budget["bytes_per_feature"]
    )
    return {
        "candidate_count": candidate_count,
        "context_feature_count": feature_count,
        "estimated_candidate_comparisons": comparisons,
        "estimated_compare_cost": int(compare_cost),
        "estimated_resident_bytes": int(resident_bytes),
    }


def plan_admission(
    documents: list[dict[str, Any]],
    scored_documents: list[dict[str, Any]],
    features: dict[str, Any],
    config: dict[str, Any],
    *,
    policy: str | None = None,
) -> dict[str, Any]:
    budget = config["budget"]
    effective_policy = policy or budget.get("policy", "strict")
    candidate_counts = {
        item["doc_id"]: len(item["candidates"]) for item in scored_documents
    }
    feature_counts = document_feature_counts(documents, features)

    decisions: list[dict[str, Any]] = []
    admitted: list[str] = []
    rejected: list[dict[str, Any]] = []
    used_compare_cost = 0
    used_resident_bytes = 0
    first_degradation: dict[str, Any] | None = None

    for doc in sorted(documents, key=lambda item: item["doc_id"]):
        doc_id = doc["doc_id"]
        estimate = _doc_estimate(
            doc,
            candidate_counts.get(doc_id, 0),
            feature_counts.get(doc_id, 0),
            budget,
        )
        reason_code = None
        if (
            used_compare_cost + estimate["estimated_compare_cost"]
            > budget["total_compare_budget"]
        ):
            reason_code = E_COMPARE_BUDGET
        elif (
            used_resident_bytes + estimate["estimated_resident_bytes"]
            > budget["max_resident_cache_mb"] * 1024 * 1024
        ):
            reason_code = E_RESOURCE_LIMIT

        decision = {
            "doc_id": doc_id,
            "shard_id": doc["shard_id"],
            "admitted": reason_code is None,
            "reason_code": reason_code,
            "observations": estimate,
            "cumulative_after": {
                "compare_cost": (
                    used_compare_cost + estimate["estimated_compare_cost"]
                    if reason_code is None
                    else used_compare_cost
                ),
                "resident_bytes": (
                    used_resident_bytes + estimate["estimated_resident_bytes"]
                    if reason_code is None
                    else used_resident_bytes
                ),
            },
        }
        decisions.append(decision)
        if reason_code is None:
            admitted.append(doc_id)
            used_compare_cost += estimate["estimated_compare_cost"]
            used_resident_bytes += estimate["estimated_resident_bytes"]
        else:
            rejected.append(decision)
            if first_degradation is None:
                first_degradation = {
                    "category": (
                        "COMPARE_BUDGET"
                        if reason_code == E_COMPARE_BUDGET
                        else "RESOURCE_LIMIT"
                    ),
                    "trigger_budget": (
                        "total_compare_budget"
                        if reason_code == E_COMPARE_BUDGET
                        else "max_resident_cache_mb"
                    ),
                    "first_rejected_doc_id": doc_id,
                }

    plan = {
        "policy": effective_policy,
        "budgets": {
            "total_compare_budget": budget["total_compare_budget"],
            "max_resident_cache_mb": budget["max_resident_cache_mb"],
        },
        "admitted_doc_ids": admitted,
        "rejected_doc_ids": [item["doc_id"] for item in rejected],
        "batch_sizes": [len(admitted)] if admitted else [],
        "degraded": bool(rejected),
        "degradation": first_degradation,
        "decisions": decisions,
        "usage": {
            "compare_cost": used_compare_cost,
            "resident_bytes": used_resident_bytes,
            "resident_mb": rounded(used_resident_bytes / (1024 * 1024), 12),
        },
    }

    if rejected and effective_policy == "strict":
        first = rejected[0]
        code = first["reason_code"]
        raise PipelineError(
            code,
            (
                "document admission exceeds comparison budget"
                if code == E_COMPARE_BUDGET
                else "document admission exceeds resident cache limit"
            ),
            {
                "first_rejected": first,
                "budget_plan": plan,
            },
        )
    return plan

