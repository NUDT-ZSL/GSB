"""Deterministic data-processing pipeline bound to one configuration version."""

from __future__ import annotations

import math
from typing import Any

from .canonical import digest_value
from .errors import bad_config
from .validation import OUTPUT_FORMATS, input_digest

PIPELINE_NAMESPACE = "pipeline"


def _config_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    for namespace in snapshot["namespaces"]:
        if namespace["namespace"] == PIPELINE_NAMESPACE:
            return {config["key"]: config for config in namespace["configs"]}
    raise bad_config(f"configuration namespace {PIPELINE_NAMESPACE!r} is missing")


def validate_pipeline_config(snapshot: dict[str, Any]) -> dict[str, Any]:
    configs = _config_map(snapshot)
    required = {"filter_threshold", "aggregate_weight", "output_format"}
    missing = sorted(required - set(configs))
    if missing:
        raise bad_config(
            f"pipeline configuration is missing keys: {', '.join(missing)}"
        )

    threshold_config = configs["filter_threshold"]
    weight_config = configs["aggregate_weight"]
    format_config = configs["output_format"]
    if threshold_config["type"] != "number":
        raise bad_config("pipeline.filter_threshold must have type 'number'")
    if weight_config["type"] != "number":
        raise bad_config("pipeline.aggregate_weight must have type 'number'")
    if format_config["type"] != "string":
        raise bad_config("pipeline.output_format must have type 'string'")

    threshold = float(threshold_config["value"])
    weight = float(weight_config["value"])
    output_format = format_config["value"]
    if not math.isfinite(threshold):
        raise bad_config("pipeline.filter_threshold must be finite")
    if not math.isfinite(weight):
        raise bad_config("pipeline.aggregate_weight must be finite")
    if output_format not in OUTPUT_FORMATS:
        raise bad_config(
            "pipeline.output_format must be one of "
            + ", ".join(sorted(OUTPUT_FORMATS))
        )
    return {
        "filter_threshold": threshold,
        "aggregate_weight": weight,
        "output_format": output_format,
    }


def _format_summary(count: int, weighted_sum: float, output_format: str) -> str:
    if output_format == "fixed3":
        return f"count={count};weighted_sum={weighted_sum:.3f}"
    if output_format == "scientific6":
        return f"count={count};weighted_sum={weighted_sum:.6e}"
    if output_format == "decimal2":
        return f"count={count};weighted_sum={weighted_sum:.2f}"
    raise bad_config(f"unsupported output format: {output_format}")


def compute_pipeline(
    snapshot: dict[str, Any],
    normalized_input: dict[str, Any],
    *,
    client_id: str,
    round_no: int,
) -> dict[str, Any]:
    """Execute a fixed-order left fold and return a version-bound result."""

    parameters = validate_pipeline_config(snapshot)
    threshold = parameters["filter_threshold"]
    weight = parameters["aggregate_weight"]
    output_format = parameters["output_format"]

    included_ids: list[str] = []
    unweighted_sum = 0.0
    weighted_sum = 0.0
    group_sums: dict[str, float] = {}
    group_counts: dict[str, int] = {}

    # The input order is part of the sample digest. Do not sort or parallelize this
    # fold: each binary64 addition must occur in exactly this sequence.
    for record in normalized_input["records"]:
        value = float(record["value"])
        if value < threshold:
            continue
        term = value * weight
        unweighted_sum = unweighted_sum + value
        weighted_sum = weighted_sum + term
        group = record["group"]
        group_sums[group] = group_sums.get(group, 0.0) + term
        group_counts[group] = group_counts.get(group, 0) + 1
        included_ids.append(record["id"])

    groups = []
    for group in sorted(group_counts):
        groups.append(
            {
                "group": group,
                "count": group_counts[group],
                "weighted_sum": group_sums[group],
            }
        )

    payload: dict[str, Any] = {
        "schema_version": 1,
        "client_id": client_id,
        "round": round_no,
        "config_version": snapshot["version"],
        "config_digest": snapshot["content_digest"],
        "input_digest": input_digest(normalized_input),
        "pipeline": {
            "filter_threshold": threshold,
            "aggregate_weight": weight,
            "output_format": output_format,
            "included_ids": included_ids,
            "included_count": len(included_ids),
            "unweighted_sum": unweighted_sum,
            "weighted_sum": weighted_sum,
            "groups": groups,
            "summary": _format_summary(
                len(included_ids), weighted_sum, output_format
            ),
        },
    }
    payload["result_digest"] = digest_value(payload)
    return payload


def result_payload_digest(result: dict[str, Any]) -> str:
    payload = dict(result)
    payload.pop("result_digest", None)
    return digest_value(payload)
