from __future__ import annotations

from typing import Any

from .util import canonical_json, sha256_json


def _v1_effective_generation(snapshot: dict[str, Any]) -> int:
    return int(snapshot["state_generation"])


def _v1_time_conversion(snapshot: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    scale = 1000
    relative = int(item["relative_failure_seconds"])
    absolute = int(snapshot["logical_clock_ms"]) - relative * scale
    return {
        "input_field": "relative_failure_seconds",
        "input_value": relative,
        "logical_clock_ms": int(snapshot["logical_clock_ms"]),
        "scale_ms_per_second": scale,
        "rounding": "exact_integer",
        "rounding_direction": "none",
        "formula": "logical_clock_ms - relative_failure_seconds * 1000",
        "output_field": "absolute_failure_ms",
        "output_value": absolute,
    }


def _v1_source(snapshot: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": "v1",
        "package_version": snapshot["package_version"],
        "snapshot_id": snapshot["v1"]["snapshot_id"],
        "logical_clock_ms": snapshot["v1"]["logical_clock_ms"],
        "state_generation": _v1_effective_generation(snapshot["v1"]),
        "source_priority": snapshot["v1"]["source_priority"],
        "original_instance_id": item["original_instance_id"],
        "raw": {
            "instance_id": item["original_instance_id"],
            "relative_failure_seconds": item["relative_failure_seconds"],
            "consecutive_failures": item["consecutive_failures"],
            "status": item["status"],
        },
        "time_representation": "relative_seconds",
        "time_conversion": _v1_time_conversion(snapshot["v1"], item),
        "derived": {
            "health_status": item["status"],
            "absolute_failure_ms": _v1_time_conversion(snapshot["v1"], item)["output_value"],
            "consecutive_failures": item["consecutive_failures"],
            "consecutive_successes": 0,
            "effective_weight": snapshot["v1"]["default_effective_weight"],
            "tombstone": False,
        },
    }


def _v2_source(snapshot: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": "v2",
        "package_version": snapshot["package_version"],
        "snapshot_id": snapshot["v2"]["snapshot_id"],
        "logical_clock_ms": snapshot["v2"]["logical_clock_ms"],
        "state_generation": item["state_generation"],
        "source_priority": snapshot["v2"]["source_priority"],
        "original_instance_id": item["original_instance_id"],
        "raw": {
            "canonical_instance_id": item["canonical_instance_id"],
            "original_instance_id": item["original_instance_id"],
            "absolute_failure_ms": item["absolute_failure_ms"],
            "state_generation": item["state_generation"],
            "consecutive_failures": item["consecutive_failures"],
            "consecutive_successes": item["consecutive_successes"],
            "effective_weight": item["effective_weight"],
            "tombstone": item["tombstone"],
            "status": item["status"],
            "shard_id": item["shard_id"],
        },
        "time_representation": "absolute_epoch_ms",
        "time_conversion": None,
        "derived": {
            "health_status": item["status"],
            "absolute_failure_ms": item["absolute_failure_ms"],
            "consecutive_failures": item["consecutive_failures"],
            "consecutive_successes": item["consecutive_successes"],
            "effective_weight": item["effective_weight"],
            "tombstone": item["tombstone"],
        },
    }


def _decision_rank(source: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(source["logical_clock_ms"]),
        int(source["source_priority"]),
        int(source["state_generation"]),
    )


def _semantics_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # The two formats intentionally use different field names. Compare only
    # semantic fields when deciding whether an ambiguous tie actually matters.
    semantic_left = (
        left["derived"]["health_status"],
        left["derived"]["consecutive_failures"],
        left["derived"]["consecutive_successes"],
        left["derived"]["effective_weight"],
        left["derived"]["tombstone"],
    )
    semantic_right = (
        right["derived"]["health_status"],
        right["derived"]["consecutive_failures"],
        right["derived"]["consecutive_successes"],
        right["derived"]["effective_weight"],
        right["derived"]["tombstone"],
    )
    return semantic_left == semantic_right


def merge_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    all_ids = sorted(set(snapshot["v1"]["instances"]) | set(snapshot["v2"]["instances"]))
    for instance_id in all_ids:
        sources: dict[str, dict[str, Any]] = {}
        v1_item = snapshot["v1"]["instances"].get(instance_id)
        v2_item = snapshot["v2"]["instances"].get(instance_id)
        if v1_item is not None:
            sources["v1"] = _v1_source(snapshot, v1_item)
        if v2_item is not None:
            sources["v2"] = _v2_source(snapshot, v2_item)

        ranked = sorted(
            sources.items(),
            key=lambda pair: (_decision_rank(pair[1]), pair[0] == "v2"),
            reverse=True,
        )
        selected_format, selected = ranked[0]
        manual = False
        decision_reason = "highest logical_clock_ms, then source_priority, then state_generation"
        if len(ranked) > 1:
            first_rank = _decision_rank(ranked[0][1])
            second_rank = _decision_rank(ranked[1][1])
            if first_rank == second_rank and not _semantics_equal(
                ranked[0][1], ranked[1][1]
            ):
                manual = True
                selected_format = None
                decision_reason = (
                    "equal logical_clock_ms, state_generation, and source_priority "
                    "for conflicting source values"
                )
            elif first_rank == second_rank:
                decision_reason = (
                    "equal rank and equivalent source semantics; v2 retained for "
                    "source-format tie-break"
                )

        if manual:
            status = "pending_manual"
            generation = max(int(src["state_generation"]) for src in sources.values())
            counts_failures = None
            counts_successes = None
            weight = 0
            failure_ms = None
            tombstone = False
        else:
            derived = selected["derived"]
            status = derived["health_status"]
            generation = int(selected["state_generation"])
            counts_failures = int(derived["consecutive_failures"])
            counts_successes = int(derived["consecutive_successes"])
            weight = int(derived["effective_weight"])
            failure_ms = int(derived["absolute_failure_ms"])
            tombstone = bool(derived["tombstone"])

        decision = {
            "selected_source": selected_format,
            "manual": manual,
            "rule": decision_reason,
            "rank_order": ["logical_clock_ms", "source_priority", "state_generation"],
            "candidates": [
                {
                    "source": source_format,
                    "logical_clock_ms": source["logical_clock_ms"],
                    "state_generation": source["state_generation"],
                    "source_priority": source["source_priority"],
                    "rank": list(_decision_rank(source)),
                    "status": source["derived"]["health_status"],
                }
                for source_format, source in sorted(sources.items())
            ],
        }
        merged[instance_id] = {
            "instance_id": instance_id,
            "selected_source": selected_format,
            "health_status": status,
            "state_generation": generation,
            "consecutive_failures": counts_failures,
            "consecutive_successes": counts_successes,
            "effective_weight": weight,
            "failure_at_ms": failure_ms,
            "tombstone": tombstone,
            "manual": manual,
            "sources": sources,
            "decision": decision,
        }

    canonical_records = [
        merged[instance_id] for instance_id in sorted(merged)
    ]
    return {
        "schema": "lb-merge-view/v1",
        "snapshot_version": snapshot["package_version"],
        "logical_clock_ms": snapshot["logical_clock_ms"],
        "source_snapshots": {
            "v1": {
                "snapshot_id": snapshot["v1"]["snapshot_id"],
                "logical_clock_ms": snapshot["v1"]["logical_clock_ms"],
                "state_generation": snapshot["v1"]["state_generation"],
                "source_priority": snapshot["v1"]["source_priority"],
            },
            "v2": {
                "snapshot_id": snapshot["v2"]["snapshot_id"],
                "logical_clock_ms": snapshot["v2"]["logical_clock_ms"],
                "state_generation_floor": snapshot["v2"]["state_generation_floor"],
                "source_priority": snapshot["v2"]["source_priority"],
            },
        },
        "instances": canonical_records,
        "merged_digest": sha256_json(canonical_records),
        "canonical_digest_input": canonical_json(canonical_records),
    }
