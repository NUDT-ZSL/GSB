from __future__ import annotations

import json
from typing import Any

from .canonical import semantic_digest


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, dict):
        return dict(value)
    return {}


def equivalent_observations(
    left: dict[str, Any],
    right: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    left_version = int(left["schema_version"])
    right_version = int(right["schema_version"])
    if left_version == right_version:
        return str(left["digest"]) == str(right["digest"])
    if sorted(_json_list(left["status_codes"])) != sorted(
        _json_list(right["status_codes"])
    ):
        return False
    if int(left["logical_clock_ms"]) != int(right["logical_clock_ms"]):
        return False
    if abs(float(left["observed_at_ms"]) - float(right["observed_at_ms"])) > float(
        config.get("version_time_tolerance_ms", 5)
    ):
        return False
    if abs(
        float(left["latency_summary_ms"]) - float(right["latency_summary_ms"])
    ) > float(config.get("version_latency_tolerance_ms", 1)):
        return False
    return True


def difference_fields(
    left: dict[str, Any],
    right: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    fields: list[str] = []
    if sorted(_json_list(left["status_codes"])) != sorted(
        _json_list(right["status_codes"])
    ):
        fields.append("status_codes")
    if int(left["logical_clock_ms"]) != int(right["logical_clock_ms"]):
        fields.append("logical_clock_ms")
    if abs(float(left["observed_at_ms"]) - float(right["observed_at_ms"])) > float(
        config.get("version_time_tolerance_ms", 5)
    ):
        fields.append("observed_at_ms")
    if abs(
        float(left["latency_summary_ms"]) - float(right["latency_summary_ms"])
    ) > float(config.get("version_latency_tolerance_ms", 1)):
        fields.append("latency_summary_ms")
    if _json_dict(left.get("latency_histogram_json", left.get("latency_histogram"))) != _json_dict(
        right.get("latency_histogram_json", right.get("latency_histogram"))
    ):
        fields.append("latency_histogram")
    return fields


def effective_event(
    obs_seq: int,
    group: list[dict[str, Any]],
) -> dict[str, Any]:
    preferred = sorted(
        group,
        key=lambda row: (
            -int(row["schema_version"]),
            int(row["logical_clock_ms"]),
            str(row["digest"]),
            str(row["shard_id"]),
        ),
    )[0]
    statuses = sorted(_json_list(preferred["status_codes"]))
    outcome = str(preferred["outcome"])
    basis = semantic_digest(
        str(preferred["instance_id"]),
        obs_seq,
        outcome,
        statuses,
    )
    return {
        "kind": "observation",
        "instance_id": str(preferred["instance_id"]),
        "obs_seq": obs_seq,
        "logical_clock_ms": int(preferred["logical_clock_ms"]),
        "observed_at_ms": int(preferred["observed_at_ms"]),
        "status_codes": statuses,
        "outcome": outcome,
        "basis_digest": basis,
        "source_digest": str(preferred["digest"]),
        "schema_version": int(preferred["schema_version"]),
        "shard_ids": sorted({str(row["shard_id"]) for row in group}),
    }


def same_version_conflict(group: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for left_index, left in enumerate(group):
        for right in group[left_index + 1 :]:
            if int(left["schema_version"]) != int(right["schema_version"]):
                continue
            if str(left["digest"]) != str(right["digest"]):
                return left, right
    return None


def version_conflict(
    group: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]] | None:
    v1_rows = [row for row in group if int(row["schema_version"]) == 1]
    v2_rows = [row for row in group if int(row["schema_version"]) == 2]
    for left in v1_rows:
        for right in v2_rows:
            if not equivalent_observations(left, right, config):
                return left, right, difference_fields(left, right, config)
    return None
