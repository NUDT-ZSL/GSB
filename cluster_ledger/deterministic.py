"""Deterministic sample-data aggregation.

The same declaration, task, cluster, config version and sample file always
produce the same summary and component map. No wall clock or random input is
part of the calculation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


SUPPORTED_AGGREGATIONS = ("sha256", "sum", "count")


class AggregationDataError(ValueError):
    def __init__(self, message: str, field_path: str) -> None:
        super().__init__(message)
        self.field_path = field_path


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AggregateResult:
    aggregation_mode: str
    summary: str
    components: dict[str, str]
    entry_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "aggregation_mode": self.aggregation_mode,
            "summary": self.summary,
            "components": self.components,
            "entry_count": self.entry_count,
        }


def _lookup_version_data(sample_data: Mapping[str, Any], cluster_id: str,
                         config_version: int) -> Mapping[str, Any]:
    clusters = sample_data.get("clusters")
    if not isinstance(clusters, Mapping):
        raise AggregationDataError("sample data has no clusters object", "$.clusters")
    cluster_data = clusters.get(cluster_id)
    if not isinstance(cluster_data, Mapping):
        raise AggregationDataError(
            f"sample data missing cluster {cluster_id}", f"$.clusters.{cluster_id}"
        )
    version_data = cluster_data.get(str(config_version))
    if not isinstance(version_data, Mapping):
        raise AggregationDataError(
            f"sample data missing config version {config_version}",
            f"$.clusters.{cluster_id}.{config_version}",
        )
    return version_data


def _validate_scalar(value: Any, field_path: str) -> Any:
    if isinstance(value, (dict, list, tuple)) or value is None or isinstance(value, bool):
        raise AggregationDataError("sample values must be strings, integers, or floats", field_path)
    if isinstance(value, float):
        # JSON NaN/Infinity would not be deterministic across libraries.
        if value != value or value in (float("inf"), float("-inf")):
            raise AggregationDataError("sample value must be finite", field_path)
    return value


def compute_aggregate(
    *,
    cluster_id: str,
    nodes: Iterable[str],
    config_version: int,
    config_keys: Iterable[str],
    aggregation: Mapping[str, Any],
    sample_data: Mapping[str, Any],
) -> AggregateResult:
    mode = aggregation.get("mode")
    if mode not in SUPPORTED_AGGREGATIONS:
        raise AggregationDataError(f"unsupported aggregation mode: {mode!r}", "aggregation.mode")

    version_data = _lookup_version_data(sample_data, cluster_id, config_version)
    ordered_nodes = sorted(nodes)
    ordered_keys = sorted(config_keys)
    components: dict[str, str] = {}
    entry_count = 0
    numeric_total = 0

    for key in ordered_keys:
        key_data = version_data.get(key)
        if not isinstance(key_data, Mapping):
            raise AggregationDataError(
                f"sample data missing config key {key}",
                f"$.clusters.{cluster_id}.{config_version}.{key}",
            )
        entries: list[list[Any]] = []
        key_numeric_total = 0
        for node in ordered_nodes:
            if node not in key_data:
                raise AggregationDataError(
                    f"sample data missing node {node}",
                    f"$.clusters.{cluster_id}.{config_version}.{key}.{node}",
                )
            value = _validate_scalar(
                key_data[node],
                f"$.clusters.{cluster_id}.{config_version}.{key}.{node}",
            )
            entries.append([node, value])
            entry_count += 1
            if isinstance(value, int) and not isinstance(value, bool):
                key_numeric_total += value
            elif isinstance(value, float) and value.is_integer():
                key_numeric_total += int(value)
            elif mode in ("sum", "count") and mode == "sum":
                raise AggregationDataError(
                    "sum aggregation requires integer sample values",
                    f"$.clusters.{cluster_id}.{config_version}.{key}.{node}",
                )

        if mode == "sha256":
            components[key] = "sha256:" + digest_text(canonical_json(entries))
        elif mode == "sum":
            components[key] = f"sum:{key_numeric_total}"
            numeric_total += key_numeric_total
        else:  # count
            components[key] = f"count:{len(entries)}"

    if mode == "sha256":
        summary = "sha256:" + digest_text(canonical_json(components))
    elif mode == "sum":
        summary = f"sum:{numeric_total}"
    else:
        summary = f"count:{entry_count}"

    return AggregateResult(
        aggregation_mode=mode,
        summary=summary,
        components=components,
        entry_count=entry_count,
    )


def first_difference(left: Mapping[str, str], right: Mapping[str, str]) -> str:
    """Return the first stable component key that differs."""

    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            return key
    return "$summary"


def task_result_digest(results: Iterable[Mapping[str, Any]]) -> str:
    """Aggregate complete per-cluster results into one replay digest."""

    rows = []
    for result in results:
        rows.append(
            {
                "cluster_id": result["cluster_id"],
                "config_version": int(result["config_version"]),
                "summary": result["summary"],
                "components": result.get("components", {}),
            }
        )
    rows.sort(key=lambda item: item["cluster_id"])
    return "sha256:" + digest_text(canonical_json(rows))

