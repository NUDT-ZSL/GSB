from __future__ import annotations

import hashlib
from typing import Any, Iterable

from .config import InputBundle
from .errors import WorkbenchError
from .scoring import PrecisionMath


def pair_key(left_id: str, right_id: str) -> str:
    first, second = sorted((left_id, right_id))
    return f"{first}|{second}|similarity"


def candidate_pairs(scored: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    identifiers = sorted(item["sample_id"] for item in scored)
    return [
        (identifiers[left], identifiers[right])
        for left in range(len(identifiers))
        for right in range(left + 1, len(identifiers))
    ]


def _jaccard_precision(
    left: set[str], right: set[str], precision: str, threshold: float
) -> tuple[float, bool]:
    math_impl = PrecisionMath(precision)
    dt = math_impl.cast
    intersection = len(left.intersection(right))
    union = len(left.union(right))
    if union == 0:
        raise WorkbenchError("E_EMPTY_SUBWORD_SET", "cannot compare empty subword sets", "dedupe")
    similarity = math_impl.div(dt(intersection), dt(union))
    duplicate = bool(similarity >= dt(threshold))
    return float(similarity), duplicate


def compute_similarity_pair(
    bundle: InputBundle,
    left_id: str,
    right_id: str,
    subwords: dict[str, set[str]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if left_id not in subwords or right_id not in subwords:
        raise WorkbenchError("E_MISSING_FIELD", "candidate subword set is missing", "dedupe")
    if not subwords[left_id] or not subwords[right_id]:
        raise WorkbenchError(
            "E_EMPTY_SUBWORD_SET",
            "similarity requires non-empty subword sets",
            "dedupe",
            left_id,
        )
    threshold = float(bundle.config["dedupe_similarity_threshold"])
    fp32_value, fp32_duplicate = _jaccard_precision(
        subwords[left_id], subwords[right_id], "float32", threshold
    )
    fp64_value, fp64_duplicate = _jaccard_precision(
        subwords[left_id], subwords[right_id], "float64", threshold
    )
    result = {
        "pair_key": pair_key(left_id, right_id),
        "candidate_ids": [left_id, right_id],
        "fp32_similarity": fp32_value,
        "fp64_reference_similarity": fp64_value,
        "similarity_delta": abs(fp32_value - fp64_value),
        "threshold": threshold,
        "fp32_duplicate": fp32_duplicate,
        "fp64_duplicate": fp64_duplicate,
        "decision_precision": "float64",
    }
    diagnostic = None
    if abs(fp64_value - threshold) <= bundle.boundary_epsilon:
        diverged = fp32_duplicate != fp64_duplicate
        diagnostic = {
            "diagnostic_id": f"sim:{pair_key(left_id, right_id)}",
            "kind": "similarity_threshold_boundary",
            "candidate_ids": [left_id, right_id],
            "fp32_value": fp32_value,
            "fp64_reference_value": fp64_value,
            "threshold": threshold,
            "distance": abs(fp64_value - threshold),
            "fp32_duplicate": fp32_duplicate,
            "fp64_duplicate": fp64_duplicate,
            "diverged": diverged,
            "decision_precision": "float64",
        }
        if diverged:
            diagnostic["business_code"] = "E_PRECISION_DIVERGENCE"
    return result, diagnostic


def _group_identifier(sample_ids: Iterable[str]) -> str:
    canonical = "\n".join(sorted(sample_ids)).encode("utf-8")
    return "grp-" + hashlib.sha256(canonical).hexdigest()[:12]


def build_groups_and_removals(
    bundle: InputBundle,
    scored: list[dict[str, Any]],
    similarities: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    by_id = {item["sample_id"]: item for item in scored}
    ordered_ids = sorted(by_id)
    similarity_lookup = {
        tuple(sorted(item["candidate_ids"])): float(item["fp64_reference_similarity"])
        for item in similarities
    }

    def similarity_between(left: str, right: str) -> float:
        if left == right:
            return 1.0
        return similarity_lookup[tuple(sorted((left, right)))]

    def rank(sample_id: str) -> tuple[float, float, str]:
        item = by_id[sample_id]
        return (
            -float(item["primary_total_score"]),
            -float(item["fp64"]["total_score"]),
            sample_id,
        )

    threshold = bundle.config["dedupe_similarity_threshold"]
    unassigned = set(ordered_ids)
    groups: list[dict[str, Any]] = []
    while unassigned:
        representative = min(unassigned, key=rank)
        members = sorted(
            sample_id
            for sample_id in unassigned
            if sample_id == representative
            or similarity_between(representative, sample_id) >= threshold
        )
        group_id = _group_identifier(members)
        retained_id = representative
        removed = []
        for sample_id in members:
            if sample_id == representative:
                continue
            removed.append(
                {
                    "sample_id": sample_id,
                    "group_id": group_id,
                    "representative_sample_id": representative,
                    "representative_similarity": similarity_between(
                        sample_id, representative
                    ),
                    "retained_primary_total": by_id[representative]["primary_total_score"],
                    "removed_primary_total": by_id[sample_id]["primary_total_score"],
                    "retained_fp64_total": by_id[representative]["fp64"]["total_score"],
                    "removed_fp64_total": by_id[sample_id]["fp64"]["total_score"],
                    "stable_task_key": f"{sample_id}|dedupe",
                }
            )
        group = {
            "group_id": group_id,
            "representative_sample_id": retained_id,
            "member_ids": members,
            "removed_ids": sorted(item["sample_id"] for item in removed),
            "retained_primary_total": by_id[retained_id]["primary_total_score"],
            "retained_fp64_total": by_id[retained_id]["fp64"]["total_score"],
            "stable_task_key": f"{retained_id}|dedupe-group",
        }
        groups.append(group)
        unassigned.difference_update(members)

    groups.sort(key=lambda item: item["group_id"])
    removals: list[dict[str, Any]] = []
    for group in groups:
        representative = group["representative_sample_id"]
        for sample_id in group["removed_ids"]:
            removals.append(
                {
                    "sample_id": sample_id,
                    "group_id": group["group_id"],
                    "representative_sample_id": representative,
                    "representative_similarity": similarity_between(sample_id, representative),
                    "retained_primary_total": by_id[representative]["primary_total_score"],
                    "removed_primary_total": by_id[sample_id]["primary_total_score"],
                    "retained_fp64_total": by_id[representative]["fp64"]["total_score"],
                    "removed_fp64_total": by_id[sample_id]["fp64"]["total_score"],
                    "stable_task_key": f"{sample_id}|dedupe",
                }
            )
    removals.sort(key=lambda item: item["sample_id"])
    decisions = {
        group["representative_sample_id"]: {
            "group_id": group["group_id"],
            "status": "retained",
            "representative_sample_id": group["representative_sample_id"],
        }
        for group in groups
    }
    for row in removals:
        decisions[row["sample_id"]] = {
            "group_id": row["group_id"],
            "status": "removed",
            "representative_sample_id": row["representative_sample_id"],
            "representative_similarity": row["representative_similarity"],
        }
    return groups, removals, decisions
