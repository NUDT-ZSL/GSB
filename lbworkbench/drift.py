from __future__ import annotations

from typing import Any


def _source_evidence(source: dict[str, Any] | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "format": source["format"],
        "package_version": source["package_version"],
        "snapshot_id": source["snapshot_id"],
        "logical_clock_ms": source["logical_clock_ms"],
        "state_generation": source["state_generation"],
        "source_priority": source["source_priority"],
        "original_instance_id": source["original_instance_id"],
        "time_representation": source["time_representation"],
        "time_conversion": source["time_conversion"],
        "raw": source["raw"],
        "derived": source["derived"],
    }


def attribute_drift(
    *,
    snapshot_version: str,
    merged: dict[str, Any],
    sources: dict[str, Any],
    observation: dict[str, Any],
    round_row: dict[str, Any],
) -> dict[str, Any] | None:
    """Return deterministic first-trigger evidence for one inconsistent probe."""

    if merged["manual"] or merged["health_status"] == "pending_manual":
        return None
    observed_health = observation["observed_health"]
    if observed_health == merged["health_status"]:
        return None

    v1 = _source_evidence(sources.get("v1"))
    v2 = _source_evidence(sources.get("v2"))
    v1_status = v1["derived"]["health_status"] if v1 else None
    v2_status = v2["derived"]["health_status"] if v2 else None
    v1_generation = v1["state_generation"] if v1 else None
    v2_generation = v2["state_generation"] if v2 else None
    v1_tombstone = bool(v1["derived"]["tombstone"]) if v1 else False
    v2_tombstone = bool(v2["derived"]["tombstone"]) if v2 else False
    conversion = v1["time_conversion"] if v1 else None
    round_clock = int(round_row["logical_clock_ms"])

    fields: list[str] = ["health_status", "probe.observed_health"]
    if v1:
        fields.extend(["v1.status", "v1.relative_failure_seconds"])
    if v2:
        fields.extend(["v2.status", "v2.state_generation", "v2.tombstone"])

    if (v2_tombstone or v1_tombstone) and (
        observed_health == "healthy"
        or (v1 is not None and not v1_tombstone)
        or (v2 is not None and not v2_tombstone)
    ):
        cause = "stale_tombstone"
        reason = (
            "tombstone state is still present in a source snapshot while the "
            "replayed probe or the other source indicates a live instance"
        )
        fields = sorted(
            set(
                fields
                + [
                    "v2.tombstone",
                    "v2.absolute_failure_ms",
                    "merged.health_status",
                ]
            )
        )
    elif (
        v1 is not None
        and v2 is not None
        and v1_status != v2_status
        and v1_generation != v2_generation
    ):
        cause = "generation_conflict"
        reason = (
            "the two generations disagree and their state generations differ; "
            "the merged winner conflicts with the replayed probe"
        )
        fields = sorted(
            set(
                fields
                + [
                    "v1.state_generation",
                    "v2.state_generation",
                    "v1.status",
                    "v2.status",
                ]
            )
        )
    elif (
        conversion is not None
        and (merged["selected_source"] == "v1" or v2 is None)
        and v1_status == "healthy"
        and int(conversion["output_value"]) <= round_clock
    ):
        cause = "time_conversion_error"
        reason = (
            "the v1 relative failure seconds convert to an absolute failure "
            "instant at or before the current logical clock, contradicting the "
            "snapshot status used by the merged view"
        )
        fields = sorted(
            set(
                fields
                + [
                    "v1.relative_failure_seconds",
                    "v1.time_conversion.output_value",
                    "round.logical_clock_ms",
                ]
            )
        )
    else:
        cause = "real_state_change"
        reason = (
            "snapshot sources are internally consistent for this instance, but "
            "the replayed probe reports a different current health state"
        )

    evidence = {
        "cause": cause,
        "reason": reason,
        "snapshot_version": snapshot_version,
        "instance_id": merged["instance_id"],
        "round": {
            "round_id": round_row["round_id"],
            "plan_id": round_row["plan_id"],
            "round_index": round_row["round_index"],
            "logical_clock_ms": round_row["logical_clock_ms"],
        },
        "fields": fields,
        "merged": {
            "selected_source": merged["selected_source"],
            "health_status": merged["health_status"],
            "state_generation": merged["state_generation"],
            "tombstone": bool(merged["tombstone"]),
            "decision": merged["decision"],
        },
        "probe": observation,
        "v1_original": v1,
        "v2_original": v2,
        "time_conversion_intermediate": conversion,
        "observed_health": observed_health,
    }
    return evidence
