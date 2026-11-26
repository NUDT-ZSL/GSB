from __future__ import annotations

import copy
from typing import Any

from .hashing import sha256_object


def reverse_level_closure(edges: list[dict[str, Any]], changed_levels: set[str]) -> set[str]:
    reverse: dict[str, set[str]] = {}
    for edge in edges:
        source = edge["service_level"]
        target = edge["depends_on_service_level"]
        reverse.setdefault(target, set()).add(source)
    closure = set(changed_levels)
    queue = list(changed_levels)
    while queue:
        current = queue.pop()
        for dependent in reverse.get(current, set()):
            if dependent not in closure:
                closure.add(dependent)
                queue.append(dependent)
    return closure


def apply_dependency_patch(edges: list[dict[str, Any]], patch: dict[str, Any]) -> list[dict[str, Any]]:
    dependency = patch.get("dependency", {})
    source = dependency.get("service_level", patch.get("service_level"))
    target = dependency.get("depends_on_service_level")
    action = dependency.get("action", patch.get("action", "add"))
    edge_key = f"{source}->{target}"
    result = [dict(edge) for edge in edges]
    if action == "add":
        if not any(edge["edge_key"] == edge_key for edge in result):
            result.append(
                {
                    "edge_key": edge_key,
                    "service_level": source,
                    "depends_on_service_level": target,
                    "source": f"patch:{patch['patch_id']}",
                    "patch_id": patch["patch_id"],
                }
            )
    elif action == "remove":
        result = [edge for edge in result if edge["edge_key"] != edge_key]
    else:
        raise ValueError(f"unsupported dependency action {action}")
    return sorted(result, key=lambda edge: edge["edge_key"])


def apply_threshold_patch(
    thresholds: dict[str, dict[str, Any]], patch: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    result = copy.deepcopy(thresholds)
    target = patch["target"]
    key = f"{target['service_level']}.{target['metric']}"
    clock = int(patch["logical_clock_ms"])
    if patch["kind"] == "threshold_update":
        threshold_payload = patch.get("threshold", {})
        value = threshold_payload.get("value", patch.get("value"))
        operator = threshold_payload.get("operator", patch.get("operator"))
        if operator is None:
            operator = "min" if target["metric"] == "throughput_attainment" else "max"
        result[key] = {
            "threshold_key": key,
            "service_level": target["service_level"],
            "metric": target["metric"],
            "active": True,
            "tombstoned": False,
            "value": str(value),
            "operator": operator,
            "source_version": f"patch:{patch['patch_id']}",
            "source_format": "patch_json",
            "logical_clock_ms": clock,
            "generation": int(patch.get("generation", clock)),
            "original_time_representation": {"patch_logical_clock_ms": clock},
            "merge_reason": "committed_patch_overrides_snapshot",
            "patch_id": patch["patch_id"],
        }
    elif patch["kind"] == "tombstone":
        existing = result.get(key, {})
        result[key] = {
            **existing,
            "threshold_key": key,
            "service_level": target["service_level"],
            "metric": target["metric"],
            "active": False,
            "tombstoned": True,
            "value": None,
            "source_version": f"patch-tombstone:{patch['patch_id']}",
            "source_format": "patch_json",
            "logical_clock_ms": clock,
            "generation": int(patch.get("generation", clock)),
            "merge_reason": "committed_patch_tombstone",
            "patch_id": patch["patch_id"],
        }
    return result


def apply_metric_parameter_patch(
    metric_definitions: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(metric_definitions)
    metric = patch["metric"]
    result.setdefault("metrics", {}).setdefault(metric, {})
    result["metrics"][metric].update(copy.deepcopy(patch.get("parameters", {})))
    return result


def plan_invalidation(
    patch: dict[str, Any],
    values: list[dict[str, Any]],
    sensitivity_assessments: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    kind = patch["kind"]
    affected_value_keys: set[str] = set()
    affected_ledger_keys: set[str] = set()
    affected_conclusion_keys: set[str] = set()
    affected_obs_keys: set[str] = set()
    affected_sensitivity_keys: set[str] = set()
    closure_levels: set[str] = set()
    closure_reason = ""

    def value_key(item: dict[str, Any]) -> str:
        return f"{item['obs_key']}:{item['metric']}"

    if kind == "metric_parameter_update":
        metric = patch["metric"]
        closure_reason = "metric_parameter_edge:metric_definition->all_observations_of_metric"
        for value in values:
            if value["metric"] == metric:
                key = value_key(value)
                affected_value_keys.add(key)
                affected_ledger_keys.add(key)
                affected_conclusion_keys.add(key)
                affected_obs_keys.add(value["obs_key"])
        for record in sensitivity_assessments:
            if record["metric"] == metric:
                affected_sensitivity_keys.add(record["sensitivity_key"])

    elif kind in {"threshold_update", "tombstone"}:
        target = patch["target"]
        level = target["service_level"]
        metric = target["metric"]
        closure_levels = reverse_level_closure(edges, {level})
        closure_reason = "reverse_service_level_dependency_closure_plus_level_to_metric_edges"
        for value in values:
            direct = value["service_level"] == level and value["metric"] == metric
            propagated = value["service_level"] in closure_levels and value["service_level"] != level
            if direct or propagated:
                key = value_key(value)
                affected_conclusion_keys.add(key)
                affected_obs_keys.add(value["obs_key"])
        for record in sensitivity_assessments:
            endpoint_a = f"{record['obs_a']}:{record['metric']}"
            endpoint_b = f"{record['obs_b']}:{record['metric']}"
            if record["metric"] == metric and (
                endpoint_a in affected_conclusion_keys or endpoint_b in affected_conclusion_keys
            ):
                affected_sensitivity_keys.add(record["sensitivity_key"])

    elif kind == "dependency_change":
        dependency = patch.get("dependency", {})
        level = dependency.get("service_level", patch.get("service_level"))
        closure_levels = reverse_level_closure(edges, {level})
        closure_reason = "changed_service_level_then_reverse_dependents_then_all_level_metric_edges"
        for value in values:
            if value["service_level"] in closure_levels:
                key = value_key(value)
                affected_conclusion_keys.add(key)
                affected_obs_keys.add(value["obs_key"])
        for record in sensitivity_assessments:
            if record["obs_a"] in affected_obs_keys or record["obs_b"] in affected_obs_keys:
                affected_sensitivity_keys.add(record["sensitivity_key"])

    all_value_keys = {value_key(value) for value in values}
    all_conclusion_keys = set(all_value_keys)
    all_sensitivity_keys = {item["sensitivity_key"] for item in sensitivity_assessments}
    plan = {
        "patch_id": patch["patch_id"],
        "patch_kind": kind,
        "logical_clock_ms": patch["logical_clock_ms"],
        "closure_reason": closure_reason,
        "dependency_closure_levels": sorted(closure_levels),
        "affected_observation_keys": sorted(affected_obs_keys),
        "recompute": {
            "metric_values": sorted(affected_value_keys),
            "error_ledger": sorted(affected_ledger_keys),
            "conclusions": sorted(affected_conclusion_keys),
            "sensitivity": sorted(affected_sensitivity_keys),
        },
        "reuse": {
            "metric_values": sorted(all_value_keys - affected_value_keys),
            "error_ledger": sorted(all_value_keys - affected_ledger_keys),
            "conclusions": sorted(all_conclusion_keys - affected_conclusion_keys),
            "sensitivity": sorted(all_sensitivity_keys - affected_sensitivity_keys),
        },
    }
    plan["closure_digest"] = sha256_object(plan)
    return plan


def reused_digests_match(
    plan: dict[str, Any],
    parent_report: dict[str, Any],
    child_report: dict[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    parent_values = {f"{item['obs_key']}:{item['metric']}": item for item in parent_report["evaluation"]["values"]}
    child_values = {f"{item['obs_key']}:{item['metric']}": item for item in child_report["evaluation"]["values"]}
    for key in plan["reuse"]["metric_values"]:
        same = parent_values[key]["value_digest"] == child_values[key]["value_digest"]
        checks.append({"artifact": "metric_values", "key": key, "unchanged": same})
    parent_conclusions = {
        f"{item['obs_key']}:{item['metric']}": item for item in parent_report["evaluation"]["conclusions"]
    }
    child_conclusions = {
        f"{item['obs_key']}:{item['metric']}": item for item in child_report["evaluation"]["conclusions"]
    }
    for key in plan["reuse"]["conclusions"]:
        same = parent_conclusions[key]["conclusion_digest"] == child_conclusions[key]["conclusion_digest"]
        checks.append({"artifact": "conclusions", "key": key, "unchanged": same})
    parent_sens = {item["sensitivity_key"]: item for item in parent_report["evaluation"]["sensitivity_assessments"]}
    child_sens = {item["sensitivity_key"]: item for item in child_report["evaluation"]["sensitivity_assessments"]}
    for key in plan["reuse"]["sensitivity"]:
        same = parent_sens[key]["record_digest"] == child_sens[key]["record_digest"]
        checks.append({"artifact": "sensitivity", "key": key, "unchanged": same})

    parent_edges = {edge["edge_key"]: edge for edge in parent_report["threshold_merge"]["dependency_edges"]}
    child_edges = {edge["edge_key"]: edge for edge in child_report["threshold_merge"]["dependency_edges"]}
    target_edge_key = None
    patch = child_report.get("patch")
    if patch and patch.get("kind") == "dependency_change":
        dependency = patch.get("dependency", {})
        source = dependency.get("service_level", patch.get("service_level"))
        target = dependency.get("depends_on_service_level")
        target_edge_key = f"{source}->{target}"
    for key, parent_edge in sorted(parent_edges.items()):
        if key == target_edge_key and key not in child_edges:
            continue
        child_edge = child_edges.get(key)
        same = child_edge is not None and sha256_object(parent_edge) == sha256_object(child_edge)
        checks.append({"artifact": "dependency_edges", "key": key, "unchanged": same})
    return checks
