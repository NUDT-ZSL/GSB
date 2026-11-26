from __future__ import annotations

from decimal import Decimal
from typing import Any

from .hashing import sha256_object
from .precision import Ledger, round_scaled_fraction, decimal_to_fraction


SOURCE_PRIORITY = {"v1_json": 1, "v2_shard_json_sqlite": 2}


def convert_v1_relative_time(v1: dict[str, Any], origin_ms: int) -> dict[str, Any]:
    seconds = int(v1["effective_after_seconds"])
    scale = 1000
    raw_ms = seconds * scale
    effective_at_ms = origin_ms + raw_ms
    return {
        "relative_field": "effective_after_seconds",
        "relative_value_seconds": seconds,
        "origin_ms": origin_ms,
        "scale_ms_per_second": scale,
        "value_before_rounding_ms": raw_ms,
        "rounded_value_ms": raw_ms,
        "effective_at_ms": effective_at_ms,
        "rounding_mode": "exact_integer_conversion",
        "rounding_direction": "exact",
        "discarded_remainder": 0,
    }


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        int(candidate["logical_clock_ms"]),
        int(candidate["source_priority"]),
        int(candidate["generation"]),
        str(candidate["candidate_id"]),
    )


def merge_thresholds(input_data: Any) -> dict[str, Any]:
    origin_ms = input_data.origin_ms
    time_conversion = convert_v1_relative_time(input_data.v1, origin_ms)
    candidates: list[dict[str, Any]] = []

    v1_clock = input_data.v1.get("logical_clock_ms")
    if v1_clock is None:
        v1_clock = time_conversion["effective_at_ms"]
    for key, threshold in input_data.v1["normalized_thresholds"].items():
        candidates.append(
            {
                "candidate_id": f"v1:{key}",
                "threshold_key": key,
                "service_level": threshold["service_level"],
                "metric": threshold["metric"],
                "value": threshold["value"],
                "operator": threshold["operator"],
                "tombstone": False,
                "source_version": "v1",
                "source_format": "v1_json",
                "source_priority": SOURCE_PRIORITY["v1_json"],
                "logical_clock_ms": int(v1_clock),
                "generation": int(input_data.v1.get("generation", 1)),
                "original_time_representation": {
                    "logical_clock_ms": int(v1_clock),
                    "effective_after_seconds": input_data.v1["effective_after_seconds"],
                    "conversion": time_conversion,
                },
            }
        )

    for shard in input_data.v2_shards:
        effective = int(shard["effective_at_ms"])
        clock = int(shard.get("logical_clock_ms", effective))
        generation = int(shard["generation"])
        source = f"v2:{shard['shard_id']}"
        for key, threshold in shard.get("normalized_thresholds", {}).items():
            candidates.append(
                {
                    "candidate_id": f"{source}:threshold:{key}",
                    "threshold_key": key,
                    "service_level": threshold["service_level"],
                    "metric": threshold["metric"],
                    "value": threshold["value"],
                    "operator": threshold["operator"],
                    "tombstone": False,
                    "source_version": "v2",
                    "source_format": "v2_shard_json_sqlite",
                    "source_priority": SOURCE_PRIORITY["v2_shard_json_sqlite"],
                    "shard_id": shard["shard_id"],
                    "logical_clock_ms": clock,
                    "generation": generation,
                    "original_time_representation": {
                        "effective_at_ms": effective,
                        "logical_clock_ms": clock,
                        "generation": generation,
                        "shard_id": shard["shard_id"],
                    },
                }
            )
        for tombstone in shard.get("tombstones", []):
            key = f"{tombstone['service_level']}.{tombstone['metric']}"
            candidates.append(
                {
                    "candidate_id": f"{source}:tombstone:{key}",
                    "threshold_key": key,
                    "service_level": tombstone["service_level"],
                    "metric": tombstone["metric"],
                    "value": None,
                    "operator": None,
                    "tombstone": True,
                    "source_version": "v2",
                    "source_format": "v2_shard_json_sqlite",
                    "source_priority": SOURCE_PRIORITY["v2_shard_json_sqlite"],
                    "shard_id": shard["shard_id"],
                    "logical_clock_ms": int(tombstone.get("logical_clock_ms", clock)),
                    "generation": int(tombstone.get("generation", generation)),
                    "original_time_representation": {
                        "effective_at_ms": int(tombstone.get("effective_at_ms", effective)),
                        "logical_clock_ms": int(tombstone.get("logical_clock_ms", clock)),
                        "generation": int(tombstone.get("generation", generation)),
                        "shard_id": shard["shard_id"],
                        "tombstone": True,
                    },
                }
            )

    by_key: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_key.setdefault(candidate["threshold_key"], []).append(candidate)

    thresholds: dict[str, dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []
    for key in sorted(by_key):
        ordered = sorted(by_key[key], key=_candidate_sort_key)
        winner = ordered[-1]
        runner_up = ordered[-2] if len(ordered) > 1 else None
        if runner_up is None:
            reason = "only_one_candidate"
        elif winner["logical_clock_ms"] > runner_up["logical_clock_ms"]:
            reason = "higher_declared_logical_clock"
        elif winner["source_priority"] > runner_up["source_priority"]:
            reason = "same_logical_clock_source_format_priority_v2_over_v1"
        elif winner["generation"] > runner_up["generation"]:
            reason = "same_logical_clock_and_source_higher_generation"
        else:
            reason = "deterministic_candidate_id_tie_break"
        decision = {
            "threshold_key": key,
            "winner_candidate_id": winner["candidate_id"],
            "runner_up_candidate_id": runner_up["candidate_id"] if runner_up else None,
            "decision_rule": "lexicographic(logical_clock_ms, source_format_priority, generation, candidate_id)",
            "reason": reason,
            "candidates": [
                {
                    "candidate_id": item["candidate_id"],
                    "logical_clock_ms": item["logical_clock_ms"],
                    "generation": item["generation"],
                    "source_priority": item["source_priority"],
                    "tombstone": item["tombstone"],
                    "value": None if item["value"] is None else format(item["value"], "f") if isinstance(item["value"], Decimal) else item["value"],
                }
                for item in ordered
            ],
        }
        decisions.append(decision)
        thresholds[key] = {
            "threshold_key": key,
            "service_level": winner["service_level"],
            "metric": winner["metric"],
            "active": not winner["tombstone"],
            "tombstoned": winner["tombstone"],
            "value": None if winner["value"] is None else format(winner["value"], "f") if isinstance(winner["value"], Decimal) else winner["value"],
            "operator": winner["operator"],
            "source_version": winner["source_version"],
            "source_format": winner["source_format"],
            "logical_clock_ms": winner["logical_clock_ms"],
            "generation": winner["generation"],
            "original_time_representation": winner["original_time_representation"],
            "merge_reason": reason,
            "decision_digest": sha256_object(decision),
        }

    dependency_edges = _initial_dependency_edges(input_data)
    dependency_edges_digest = sha256_object(dependency_edges)
    return {
        "thresholds": thresholds,
        "decisions": decisions,
        "v1_time_conversion": time_conversion,
        "dependency_edges": dependency_edges,
        "dependency_edges_digest": dependency_edges_digest,
        "merge_digest": sha256_object(
            {
                "thresholds": thresholds,
                "decisions": decisions,
                "edges": dependency_edges,
                "edge_digest": dependency_edges_digest,
            }
        ),
    }


def _initial_dependency_edges(input_data: Any) -> list[dict[str, Any]]:
    edges: dict[str, dict[str, Any]] = {}
    for row in input_data.v2_metadata.get("dependencies", []):
        source = row.get("service_level")
        target = row.get("depends_on_service_level")
        if source and target:
            key = f"{source}->{target}"
            edges[key] = {
                "edge_key": key,
                "service_level": source,
                "depends_on_service_level": target,
                "source": "v2_sqlite_metadata",
            }
    for shard in input_data.v2_shards:
        for dep in shard.get("dependencies", []):
            key = f"{dep['service_level']}->{dep['depends_on_service_level']}"
            edges[key] = {
                "edge_key": key,
                "service_level": dep["service_level"],
                "depends_on_service_level": dep["depends_on_service_level"],
                "source": f"v2_shard:{shard['shard_id']}",
            }
    return sorted(edges.values(), key=lambda item: item["edge_key"])


def threshold_fixed_value(
    threshold: dict[str, Any],
    scale: int,
    rounding: str,
    ledger: Ledger | None = None,
) -> dict[str, Any]:
    value = Decimal(str(threshold["value"]))
    fraction = decimal_to_fraction(value)
    if ledger is None:
        scaled = fraction * scale
        q, rem = divmod(scaled.numerator, scaled.denominator)
        if rem and rounding == "half_up" and 2 * rem >= scaled.denominator:
            q += 1
        return {"fixed_value": q, "scale": scale, "rounding_direction": "up" if rem and q else "down"}
    fixed_value, entry = round_scaled_fraction(
        fraction,
        scale,
        rounding,
        ledger,
        window_no=-1,
        service_name="__threshold_snapshot__",
        instance_id=threshold["service_level"],
        metric=threshold["metric"],
        stage=f"threshold:{threshold['threshold_key']}",
    )
    return {"fixed_value": fixed_value, "scale": scale, "ledger_seq": entry["seq"]}
