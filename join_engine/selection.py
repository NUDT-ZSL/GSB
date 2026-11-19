from __future__ import annotations

from typing import Any

from .models import ExecutionConfig


ALGORITHMS = ("broadcast_hash", "shuffled_hash", "sort_merge")


def _side_observation(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "rows": int(stats["rows"]),
        "estimated_total_bytes": int(stats["estimated_total_bytes"]),
        "max_partition_rows": int(stats["max_partition_rows"]),
        "estimated_max_partition_bytes": int(stats["estimated_max_partition_bytes"]),
        "observed_skew_share": float(stats["observed_skew_share"]),
        "skew_detected": bool(stats["skew_detected"]),
        "generated_rows_digest": stats["generated_rows_digest"],
        "key_distribution_digest": stats["key_distribution_digest"],
    }


def select_algorithm(
    left_stats: dict[str, Any],
    right_stats: dict[str, Any],
    execution: ExecutionConfig,
    *,
    forced: str | None = None,
    scale_name: str | None = None,
) -> dict[str, Any]:
    if forced is not None and forced not in ALGORITHMS:
        raise ValueError(f"unsupported forced algorithm: {forced}")
    memory_limit = execution.memory_limit_bytes
    broadcast_limit = int(memory_limit * execution.broadcast_fraction)
    hash_limit = int(memory_limit * execution.hash_fraction)
    left_total = int(left_stats["estimated_total_bytes"])
    right_total = int(right_stats["estimated_total_bytes"])
    if left_total <= right_total:
        small_side = "left"
        small_total = left_total
        small_rows = int(left_stats["rows"])
        large_side = "right"
        large_total = right_total
        large_rows = int(right_stats["rows"])
    else:
        small_side = "right"
        small_total = right_total
        small_rows = int(right_stats["rows"])
        large_side = "left"
        large_total = left_total
        large_rows = int(left_stats["rows"])
    max_partition_bytes = max(
        int(left_stats["estimated_max_partition_bytes"]),
        int(right_stats["estimated_max_partition_bytes"]),
    )
    max_partition_side = (
        "left"
        if int(left_stats["estimated_max_partition_bytes"])
        >= int(right_stats["estimated_max_partition_bytes"])
        else "right"
    )
    broadcast_fit = small_total <= broadcast_limit
    shuffled_fit = max_partition_bytes <= hash_limit
    if forced is not None:
        algorithm = forced
        first_trigger = f"forced:{forced}"
    elif broadcast_fit:
        algorithm = "broadcast_hash"
        first_trigger = (
            f"broadcast_fit: {small_side} small-side estimated bytes "
            f"{small_total} <= broadcast limit {broadcast_limit}"
        )
    elif shuffled_fit:
        algorithm = "shuffled_hash"
        first_trigger = (
            f"broadcast_rejected: {small_side} small-side estimated bytes "
            f"{small_total} > broadcast limit {broadcast_limit}; "
            f"shuffled_fit: max partition estimated bytes {max_partition_bytes} "
            f"<= hash limit {hash_limit}"
        )
    else:
        algorithm = "sort_merge"
        first_trigger = (
            f"hash_rejected: max partition estimated bytes {max_partition_bytes} "
            f"> hash limit {hash_limit}; sort_merge external spill path selected"
        )
    conditions = [
        {
            "order": 1,
            "name": "broadcast_fit",
            "value": broadcast_fit,
            "observed": {
                "small_side": small_side,
                "small_side_rows": small_rows,
                "small_side_estimated_bytes": small_total,
                "large_side": large_side,
                "large_side_rows": large_rows,
                "large_side_estimated_bytes": large_total,
            },
            "threshold_bytes": broadcast_limit,
            "expression": "small_side_estimated_bytes <= memory_limit * broadcast_fraction",
        },
        {
            "order": 2,
            "name": "shuffled_fit",
            "value": shuffled_fit,
            "observed": {
                "max_partition_side": max_partition_side,
                "max_partition_estimated_bytes": max_partition_bytes,
            },
            "threshold_bytes": hash_limit,
            "expression": "max_partition_estimated_bytes <= memory_limit * hash_fraction",
        },
        {
            "order": 3,
            "name": "sort_merge_external_available",
            "value": True,
            "observed": {"chunk_rows": execution.chunk_rows},
            "threshold_bytes": memory_limit,
            "expression": "external sorted runs bounded by chunk_rows",
        },
    ]
    rejected: dict[str, str] = {}
    if algorithm != "broadcast_hash":
        rejected["broadcast_hash"] = (
            f"small-side estimated bytes {small_total} exceed broadcast limit {broadcast_limit}"
        )
    else:
        rejected["shuffled_hash"] = (
            "broadcast fit the smaller side in memory and avoids partition-local hash rebuilds"
        )
        rejected["sort_merge"] = (
            "broadcast is eligible and has lower observed build cost than global sorting"
        )
    if algorithm != "shuffled_hash":
        if algorithm == "sort_merge":
            rejected["shuffled_hash"] = (
                f"maximum estimated partition bytes {max_partition_bytes} exceed hash limit "
                f"{hash_limit}"
            )
    else:
        rejected["sort_merge"] = (
            f"maximum estimated partition bytes {max_partition_bytes} fit hash limit "
            f"{hash_limit}, so external sorting is unnecessary"
        )
    if algorithm != "sort_merge" and "sort_merge" not in rejected:
        rejected["sort_merge"] = "another eligible algorithm has lower estimated cost"
    return {
        "algorithm": algorithm,
        "forced": forced,
        "first_trigger": first_trigger,
        "conditions": conditions,
        "rejected": rejected,
        "memory_budget": {
            "memory_limit_bytes": memory_limit,
            "broadcast_limit_bytes": broadcast_limit,
            "hash_limit_bytes": hash_limit,
            "memory_limit_mib": execution.memory_limit_mib,
            "broadcast_fraction": execution.broadcast_fraction,
            "hash_fraction": execution.hash_fraction,
        },
        "fallback_policy": (
            "broadcast over budget falls back to partition-local hashing; "
            "shuffled hash over budget falls back per partition to bounded external sort-merge"
        ),
        "observation_source": {
            "kind": "deterministic_generator_observed",
            "left": _side_observation(left_stats),
            "right": _side_observation(right_stats),
        },
        "scale": scale_name,
    }


def decision_summary(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "algorithm": decision["algorithm"],
        "first_trigger": decision["first_trigger"],
        "rejected": decision["rejected"],
        "memory_budget": decision["memory_budget"],
        "fallback_policy": decision["fallback_policy"],
        "conditions": decision["conditions"],
        "scale": decision.get("scale"),
    }

