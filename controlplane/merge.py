"""Deterministic three-layer merge, consistency, and derivation."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence

from .canonical import stable_digest
from .numbers import (
    convert_values,
    decimal_raw,
    product_error_bound,
    sum_error_bounds,
    tolerance_ticks,
)
from .validation import (
    METRICS,
    PARAMETERS,
    ClusterSpec,
    Policy,
    Sample,
)


def _as_report_number(value: Any) -> float:
    return decimal_raw(value)


def _trace_source(
    layer: str,
    scope: str,
    parameter: str,
    fixed: Mapping[str, Any],
    *,
    step: str = "fixed_point_convert",
) -> dict[str, Any]:
    return {
        "layer": layer,
        "scope": scope,
        "parameter": parameter,
        "step": step,
        "scale": fixed["scale"],
        "raw": fixed["raw"],
        "raw_decimal": fixed["raw_decimal"],
        "fixed": fixed["fixed"],
        "rounding": fixed["rounding"],
        "quantization_error": fixed["quantization_error"],
    }


def _layer_candidates(
    sample: Sample, cluster: ClusterSpec, parameter: str
) -> list[tuple[str, str, Any]]:
    candidates: list[tuple[str, str, Any]] = []
    if parameter in sample.templates.global_params:
        candidates.append(
            ("global", "global", sample.templates.global_params[parameter])
        )
    region_params = sample.templates.region_params.get(cluster.region, {})
    if parameter in region_params:
        candidates.append(("region", cluster.region, region_params[parameter]))
    cluster_params = sample.templates.cluster_params.get(cluster.name, {})
    if parameter in cluster_params:
        candidates.append(("cluster", cluster.name, cluster_params[parameter]))
    return candidates


def _fixed_candidates(
    raw_candidates: Sequence[tuple[str, str, Any]], scale: int
) -> list[dict[str, Any]]:
    batch = convert_values([raw for _, _, raw in raw_candidates], scale)
    result: list[dict[str, Any]] = []
    for index, (layer, scope, raw) in enumerate(raw_candidates):
        result.append(
            {
                "layer": layer,
                "scope": scope,
                "raw": decimal_raw(raw),
                "raw_decimal": _decimal_literal(raw),
                "scale": scale,
                "fixed": int(batch.fixed[index]),
                "rounding": batch.rounding[index],
                "rounding_mode": "half_even",
                "quantization_error": batch.quantization_error[index],
            }
        )
    return result


def _decimal_literal(value: Any) -> str:
    dec = Decimal(str(value)) if isinstance(value, float) else value
    if not isinstance(dec, Decimal):
        dec = Decimal(dec)
    if dec == 0:
        return "0"
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _consistency_pairs(
    parameter: str,
    candidates: Sequence[Mapping[str, Any]],
    tol_ticks: int,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for left_index in range(len(candidates)):
        for right_index in range(left_index + 1, len(candidates)):
            left = candidates[left_index]
            right = candidates[right_index]
            difference = abs(int(left["fixed"]) - int(right["fixed"]))
            pairs.append(
                {
                    "parameter": parameter,
                    "layers": [left["layer"], right["layer"]],
                    "scopes": [left["scope"], right["scope"]],
                    "fixed_values": [left["fixed"], right["fixed"]],
                    "raw_values": [left["raw"], right["raw"]],
                    "scale": left["scale"],
                    "difference_ticks": difference,
                    "tolerance_ticks": tol_ticks,
                    "tolerance_margin_ticks": tol_ticks - difference,
                    "consistent": difference <= tol_ticks,
                }
            )
    return pairs


def _merge_parameter(
    sample: Sample,
    cluster: ClusterSpec,
    parameter: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    scale = sample.policy.fixed_point_scales[parameter]
    tol_ticks = tolerance_ticks(
        sample.policy.floating_tolerance[parameter], scale
    )
    raw_candidates = _layer_candidates(sample, cluster, parameter)
    candidates = _fixed_candidates(raw_candidates, scale)
    pairs = _consistency_pairs(parameter, candidates, tol_ticks)
    inconsistent_pairs = [pair for pair in pairs if not pair["consistent"]]
    consistent = not inconsistent_pairs
    any_difference = any(
        int(left["fixed"]) != int(right["fixed"])
        for index, left in enumerate(candidates)
        for right in candidates[index + 1 :]
    )

    source = candidates[-1]
    if consistent:
        authorization = "consistent"
    elif cluster.allow_differential_override:
        authorization = "allowed_by_policy"
    else:
        authorization = "denied"

    fixed = {
        "raw": source["raw"],
        "raw_decimal": source["raw_decimal"],
        "scale": scale,
        "fixed": source["fixed"],
        "rounding": source["rounding"],
        "rounding_mode": "half_even",
        "quantization_error": source["quantization_error"],
    }
    effective = {
        "value": fixed["fixed"] / scale,
        "fixed": fixed,
        "source_layer": source["layer"],
        "source_scope": source["scope"],
        "merge_path": [candidate["layer"] for candidate in candidates],
        "overridden_layers": [
            candidate["layer"] for candidate in candidates[:-1]
        ],
        "candidate_count": len(candidates),
        "candidates": candidates,
        "tolerance_ticks": tol_ticks,
        "tolerance_consistent": consistent,
        "value_differs": any_difference,
        "override_authorization": authorization,
        "override_allowed": authorization != "denied",
    }
    sensitivity: list[dict[str, Any]] = []
    for pair in pairs:
        difference = int(pair["difference_ticks"])
        if 0 < difference < tol_ticks:
            record: dict[str, Any] = {
                "kind": "override_candidate_delta",
                "cluster": cluster.name,
                "parameter": parameter,
                "region": cluster.region,
                "layers": pair["layers"],
                "scopes": pair["scopes"],
                "fixed_values": pair["fixed_values"],
                "difference_ticks": difference,
                "tolerance_ticks": tol_ticks,
                "tolerance_margin_ticks": tol_ticks - difference,
                "decision_dependency": "merge_override_selection",
                "source_chain": [
                    _trace_source(
                        candidate["layer"],
                        candidate["scope"],
                        parameter,
                        candidate,
                    )
                    for candidate in candidates
                ],
            }
            record["id"] = "ps-" + stable_digest(
                {
                    "kind": record["kind"],
                    "cluster": record["cluster"],
                    "parameter": record["parameter"],
                    "layers": record["layers"],
                    "fixed_values": record["fixed_values"],
                    "difference_ticks": difference,
                    "tolerance_ticks": tol_ticks,
                }
            )
            sensitivity.append(record)
    return effective, inconsistent_pairs, sensitivity


def _region_baseline_timeout(
    sample: Sample, region_name: str
) -> tuple[int, dict[str, Any]]:
    scale = sample.policy.fixed_point_scales["timeout_ms"]
    raw_candidates: list[tuple[str, str, Any]] = [
        ("global", "global", sample.templates.global_params["timeout_ms"])
    ]
    region_values = sample.templates.region_params.get(region_name, {})
    if "timeout_ms" in region_values:
        raw_candidates.append(("region", region_name, region_values["timeout_ms"]))
    candidates = _fixed_candidates(raw_candidates, scale)
    source = candidates[-1]
    return int(source["fixed"]), {
        "region": region_name,
        "fixed": int(source["fixed"]),
        "scale": scale,
        "value": int(source["fixed"]) / scale,
        "sources": [
            _trace_source(
                candidate["layer"],
                candidate["scope"],
                "timeout_ms",
                candidate,
            )
            for candidate in candidates
        ],
    }


def _region_timeout_path(
    sample: Sample,
    target_region: str,
) -> tuple[int, list[str], list[dict[str, Any]]]:
    baselines: dict[str, tuple[int, dict[str, Any]]] = {
        region.name: _region_baseline_timeout(sample, region.name)
        for region in sample.regions
    }
    region_map = sample.region_by_name()
    prefix: dict[str, int] = {}
    path: dict[str, list[str]] = {}
    traces: dict[str, list[dict[str, Any]]] = {}
    for wave in sample.region_waves:
        for name in wave:
            best_prefix = 0
            best_path: list[str] = []
            best_traces: list[dict[str, Any]] = []
            for dependency in sorted(region_map[name].depends_on):
                dependency_value, dependency_trace = baselines[dependency]
                candidate_prefix = (
                    prefix[dependency] + dependency_value
                )
                candidate_path = path[dependency] + [dependency]
                candidate_traces = (
                    traces[dependency] + dependency_trace["sources"]
                )
                if candidate_prefix > best_prefix:
                    best_prefix = candidate_prefix
                    best_path = candidate_path
                    best_traces = candidate_traces
            prefix[name] = best_prefix
            path[name] = best_path
            traces[name] = best_traces
    return prefix[target_region], path[target_region], traces[target_region]


def _error_metrics(
    result_value: float,
    error_bound: float,
    tolerance: Any,
    budget: Any,
    scale: int,
) -> dict[str, Any]:
    budget_dec = budget if isinstance(budget, Decimal) else Decimal(str(budget))
    within = Decimal(str(error_bound)) <= budget_dec
    return {
        "error_bound": error_bound,
        "error_budget": float(budget_dec),
        "within_error_budget": within,
        "tolerance": float(tolerance),
        "tolerance_ticks": tolerance_ticks(tolerance, scale),
    }


def _derive_worst_timeout(
    sample: Sample, cluster: ClusterSpec, effective: Mapping[str, Any]
) -> dict[str, Any]:
    timeout = effective["timeout_ms"]
    scale = timeout["fixed"]["scale"]
    prefix_fixed, path, ancestor_sources = _region_timeout_path(
        sample, cluster.region
    )
    result_fixed = prefix_fixed + int(timeout["fixed"]["fixed"])
    value = result_fixed / scale
    errors = [
        source["quantization_error"] for source in ancestor_sources
    ] + [timeout["fixed"]["quantization_error"]]
    error_bound = sum_error_bounds(errors, value)
    metric = {
        "value": value,
        "fixed": result_fixed,
        "scale": scale,
        "unit": "ms",
        "component_value": timeout["value"],
        "region_prefix_value": prefix_fixed / scale,
        "dependency_path": path,
        "sources": ancestor_sources
        + [
            _trace_source(
                timeout["source_layer"],
                timeout["source_scope"],
                "timeout_ms",
                timeout["fixed"],
            )
        ],
    }
    metric.update(
        _error_metrics(
            value,
            error_bound,
            sample.policy.floating_tolerance["worst_link_timeout_ms"],
            sample.policy.error_budget["worst_link_timeout_ms"],
            scale,
        )
    )
    return metric


def _derive_capacity(
    sample: Sample, cluster: ClusterSpec, effective: Mapping[str, Any]
) -> dict[str, Any]:
    capacity = effective["capacity_limit"]
    scale = capacity["fixed"]["scale"]
    result_fixed = int(capacity["fixed"]["fixed"]) * cluster.capacity_factor
    value = result_fixed / scale
    error_bound = sum_error_bounds(
        [capacity["fixed"]["quantization_error"] * cluster.capacity_factor],
        value,
    )
    sources = [
        _trace_source(
            capacity["source_layer"],
            capacity["source_scope"],
            "capacity_limit",
            capacity["fixed"],
        ),
        {
            "layer": "cluster",
            "scope": cluster.name,
            "parameter": "capacity_unit.factor",
            "step": "capacity_unit_scale",
            "source_unit": cluster.capacity_source_unit,
            "target_unit": cluster.capacity_target_unit,
            "factor": cluster.capacity_factor,
        },
    ]
    metric = {
        "value": value,
        "fixed": result_fixed,
        "scale": scale,
        "unit": cluster.capacity_target_unit,
        "limit_value": capacity["value"],
        "factor": cluster.capacity_factor,
        "sources": sources,
    }
    metric.update(
        _error_metrics(
            value,
            error_bound,
            sample.policy.floating_tolerance["effective_capacity"],
            sample.policy.error_budget["effective_capacity"],
            scale,
        )
    )
    return metric


def _derive_weighted_sampling(
    sample: Sample, effective: Mapping[str, Any]
) -> dict[str, Any]:
    sampling = effective["sampling_rate"]
    weight = effective["replica_weight"]
    sampling_scale = sampling["fixed"]["scale"]
    weight_scale = weight["fixed"]["scale"]
    scale = sampling_scale * weight_scale
    result_fixed = int(sampling["fixed"]["fixed"]) * int(weight["fixed"]["fixed"])
    value = result_fixed / scale
    error_bound = product_error_bound(
        sampling["value"],
        sampling["fixed"]["quantization_error"],
        weight["value"],
        weight["fixed"]["quantization_error"],
    )
    sources = [
        _trace_source(
            sampling["source_layer"],
            sampling["source_scope"],
            "sampling_rate",
            sampling["fixed"],
        ),
        {
            "layer": "derived",
            "scope": "weighted_sampling_rate",
            "parameter": "sampling_rate*replica_weight",
            "step": "fixed_point_multiply",
            "operand_scales": [sampling_scale, weight_scale],
            "result_scale": scale,
        },
        _trace_source(
            weight["source_layer"],
            weight["source_scope"],
            "replica_weight",
            weight["fixed"],
        ),
    ]
    metric = {
        "value": value,
        "fixed": result_fixed,
        "scale": scale,
        "unit": "weighted_ratio",
        "sampling_rate": sampling["value"],
        "replica_weight": weight["value"],
        "sources": sources,
    }
    metric.update(
        _error_metrics(
            value,
            error_bound,
            sample.policy.floating_tolerance["weighted_sampling_rate"],
            sample.policy.error_budget["weighted_sampling_rate"],
            scale,
        )
    )
    return metric


def compute_cluster_base(sample: Sample, cluster: ClusterSpec) -> dict[str, Any]:
    """Merge and derive one cluster without cross-cluster sensitivity records."""

    effective: dict[str, Any] = {}
    inconsistencies: list[dict[str, Any]] = []
    override_sensitivity: list[dict[str, Any]] = []
    denials: list[dict[str, Any]] = []
    for parameter in PARAMETERS:
        merged, inconsistent_pairs, sensitive = _merge_parameter(
            sample, cluster, parameter
        )
        effective[parameter] = merged
        for pair in inconsistent_pairs:
            inconsistencies.append(
                {"cluster": cluster.name, "region": cluster.region, **pair}
            )
        override_sensitivity.extend(sensitive)
        if merged["override_authorization"] == "denied":
            denials.append(
                {
                    "code": "DIFFERENTIAL_OVERRIDE_DENIED",
                    "cluster": cluster.name,
                    "parameter": parameter,
                    "source_layer": merged["source_layer"],
                    "merge_path": merged["merge_path"],
                    "tolerance_ticks": merged["tolerance_ticks"],
                }
            )

    derived = {
        "worst_link_timeout_ms": _derive_worst_timeout(
            sample, cluster, effective
        ),
        "effective_capacity": _derive_capacity(sample, cluster, effective),
        "weighted_sampling_rate": _derive_weighted_sampling(sample, effective),
    }
    budget_exceeded = [
        metric
        for metric in METRICS
        if not derived[metric]["within_error_budget"]
    ]
    return {
        "cluster": cluster.name,
        "region": cluster.region,
        "priority": cluster.priority,
        "allow_differential_override": cluster.allow_differential_override,
        "capacity_unit": {
            "source": cluster.capacity_source_unit,
            "target": cluster.capacity_target_unit,
            "factor": cluster.capacity_factor,
        },
        "dispatch_profile": {
            "latency_ms": cluster.dispatch_latency_ms,
            "fail_attempts": cluster.dispatch_fail_attempts,
        },
        "merge_order": ["global", "region", "cluster"],
        "effective_config": effective,
        "cross_layer_inconsistencies": sorted(
            inconsistencies,
            key=lambda item: (
                item["parameter"],
                item["layers"],
                item["fixed_values"],
            ),
        ),
        "override_denials": denials,
        "dispatch_eligible": not denials,
        "dispatch_ineligibility_reasons": denials,
        "derived": derived,
        "error_budget_exceeded": budget_exceeded,
        "precision_sensitive_records": override_sensitivity,
        "precision_sensitive": bool(override_sensitivity),
    }


def _record_sources_for_parameter(
    cluster_result: Mapping[str, Any], parameter: str
) -> list[dict[str, Any]]:
    effective = cluster_result["effective_config"][parameter]
    return [
        _trace_source(
            candidate["layer"],
            candidate["scope"],
            parameter,
            candidate,
        )
        for candidate in effective["candidates"]
    ]


def apply_cross_cluster_sensitivity(
    sample: Sample, cluster_results: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mark near-tie derived metrics that determine regional dispatch order."""

    records: list[dict[str, Any]] = []
    by_region: dict[str, list[dict[str, Any]]] = {}
    for result in cluster_results.values():
        if result["dispatch_eligible"]:
            by_region.setdefault(result["region"], []).append(result)

    for region in sorted(by_region):
        eligible = by_region[region]
        for metric_name in ("weighted_sampling_rate",):
            ordered = sorted(
                eligible,
                key=lambda item: (
                    -item["priority"],
                    -item["derived"][metric_name]["fixed"],
                    item["cluster"],
                ),
            )
            for left, right in zip(ordered, ordered[1:]):
                if left["priority"] != right["priority"]:
                    continue
                metric_left = left["derived"][metric_name]
                metric_right = right["derived"][metric_name]
                if metric_left["scale"] != metric_right["scale"]:
                    continue
                difference = abs(
                    int(metric_left["fixed"]) - int(metric_right["fixed"])
                )
                tol_ticks = min(
                    int(metric_left["tolerance_ticks"]),
                    int(metric_right["tolerance_ticks"]),
                )
                if not (0 < difference < tol_ticks):
                    continue
                source_chain: list[dict[str, Any]] = []
                if metric_name == "weighted_sampling_rate":
                    for cluster_result, metric in (
                        (left, metric_left),
                        (right, metric_right),
                    ):
                        source_chain.extend(
                            _record_sources_for_parameter(
                                cluster_result, "sampling_rate"
                            )
                        )
                        source_chain.extend(
                            _record_sources_for_parameter(
                                cluster_result, "replica_weight"
                            )
                        )
                else:
                    source_chain.extend(metric_left["sources"])
                    source_chain.extend(metric_right["sources"])
                record: dict[str, Any] = {
                    "kind": "derived_delta",
                    "metric": metric_name,
                    "region": region,
                    "clusters": [left["cluster"], right["cluster"]],
                    "priority": left["priority"],
                    "fixed_values": [
                        metric_left["fixed"],
                        metric_right["fixed"],
                    ],
                    "scale": metric_left["scale"],
                    "difference_ticks": difference,
                    "tolerance_ticks": tol_ticks,
                    "difference": difference / metric_left["scale"],
                    "tolerance": tol_ticks / metric_left["scale"],
                    "error_bound": (
                        metric_left["error_bound"]
                        + metric_right["error_bound"]
                    ),
                    "decision_dependency": (
                        f"regional_rank_by_{metric_name}"
                    ),
                    "source_chain": source_chain,
                }
                record["id"] = "ps-" + stable_digest(
                    {
                        "kind": record["kind"],
                        "metric": metric_name,
                        "clusters": record["clusters"],
                        "fixed_values": record["fixed_values"],
                        "difference_ticks": difference,
                        "tolerance_ticks": tol_ticks,
                    }
                )
                records.append(record)
                for target in (left, right):
                    target["precision_sensitive_records"].append(record)
                    target["precision_sensitive"] = True
    records.sort(key=lambda item: item["id"])
    return records


def recompute_dispatch_eligibility(cluster_result: dict[str, Any]) -> None:
    denials = cluster_result["override_denials"]
    cluster_result["dispatch_eligible"] = not denials
    cluster_result["dispatch_ineligibility_reasons"] = denials
