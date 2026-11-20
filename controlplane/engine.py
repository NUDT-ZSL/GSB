"""High-level cluster computation composition."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from .canonical import stable_digest
from .merge import (
    apply_cross_cluster_sensitivity,
    compute_cluster_base as build_cluster_base,
)
from .validation import Sample


def compute_cluster_base(sample: Sample, cluster_name: str) -> dict[str, Any]:
    cluster_map = sample.cluster_by_name()
    cluster = cluster_map[cluster_name]
    result = build_cluster_base(sample, cluster)
    result["config_version"] = sample.version_id
    return result


def finalize_cluster_results(
    sample: Sample, base_results: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Apply cross-cluster adjudication without repeating merge/derivation."""

    results = {
        name: copy.deepcopy(dict(result))
        for name, result in sorted(base_results.items())
    }
    apply_cross_cluster_sensitivity(sample, results)
    for name, result in results.items():
        records = sorted(
            {record["id"]: record for record in result["precision_sensitive_records"]}.values(),
            key=lambda item: item["id"],
        )
        result["precision_sensitive_records"] = records
        result["precision_sensitive"] = bool(records)
        result["precision_sensitive_count"] = len(records)
        result["sensitivity_ids"] = [record["id"] for record in records]
        digest_input = {
            "cluster": result["cluster"],
            "config_version": sample.version_id,
            "effective_config": result["effective_config"],
            "derived": result["derived"],
            "cross_layer_inconsistencies": result[
                "cross_layer_inconsistencies"
            ],
            "override_denials": result["override_denials"],
            "precision_sensitive_records": records,
            "dispatch_eligible": result["dispatch_eligible"],
        }
        result["effective_config_digest"] = "cfg-" + stable_digest(
            digest_input, 20
        )
    return results


def scheduler_inputs(
    cluster_results: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "cluster": result["cluster"],
            "region": result["region"],
            "priority": result["priority"],
            "dispatch_rank": {
                "fixed": result["derived"]["weighted_sampling_rate"]["fixed"],
                "scale": result["derived"]["weighted_sampling_rate"]["scale"],
            },
            "dispatch_profile": result["dispatch_profile"],
            "dispatch_eligible": result["dispatch_eligible"],
            "dispatch_ineligibility_reasons": result[
                "dispatch_ineligibility_reasons"
            ],
        }
        for name, result in sorted(cluster_results.items())
    }
