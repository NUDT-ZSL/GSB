from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .models import (
    AffinityExpression,
    PodSpec,
    Toleration,
    TopologySpreadConstraint,
)


@dataclass(frozen=True)
class FilterRejection:
    plugin: str
    error_code: str
    evidence: Mapping[str, Any]
    source_field: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "error_code": self.error_code,
            "evidence": dict(self.evidence),
            "source_field": self.source_field,
        }


@dataclass(frozen=True)
class FilterContext:
    pod: PodSpec
    node: Mapping[str, Any]
    nodes: Sequence[Mapping[str, Any]]
    allocations: Sequence[Mapping[str, Any]]


def _label_expression_matches(
    expression: AffinityExpression,
    labels: Mapping[str, str],
) -> bool:
    present = expression.key in labels
    value = labels.get(expression.key)
    if expression.operator == "In":
        return present and value in expression.values
    if expression.operator == "NotIn":
        return not present or value not in expression.values
    if expression.operator == "Exists":
        return present
    if expression.operator == "DoesNotExist":
        return not present
    if expression.operator in ("Gt", "Lt"):
        if not present or value is None:
            return False
        try:
            node_value = int(value)
            target_value = int(expression.values[0])
        except (TypeError, ValueError):
            return False
        if expression.operator == "Gt":
            return node_value > target_value
        return node_value < target_value
    return False


def _selector_matches(
    selector: Mapping[str, str],
    labels: Mapping[str, str],
) -> bool:
    return all(labels.get(key) == value for key, value in selector.items())


def _toleration_matches(taint: Mapping[str, Any], toleration: Toleration) -> bool:
    if taint["key"] != toleration.key:
        return False
    if toleration.effect is not None and taint["effect"] != toleration.effect:
        return False
    if toleration.operator == "Exists":
        return True
    return taint.get("value") == toleration.value


def filter_resource_fit(context: FilterContext) -> FilterRejection | None:
    if context.pod.cpu_millicores > context.node["cpu_remaining"]:
        return FilterRejection(
            plugin="ResourceFit",
            error_code="E_FILTER_RESOURCE_CPU",
            evidence={
                "kind": "cpu_request_exceeds_remaining",
                "requested_millicores": context.pod.cpu_millicores,
                "remaining_millicores": context.node["cpu_remaining"],
                "deficit_millicores": (
                    context.pod.cpu_millicores - context.node["cpu_remaining"]
                ),
            },
            source_field=f"pods[{context.pod.name}].cpu_millicores",
        )
    if context.pod.memory_mib > context.node["memory_remaining"]:
        return FilterRejection(
            plugin="ResourceFit",
            error_code="E_FILTER_RESOURCE_MEMORY",
            evidence={
                "kind": "memory_request_exceeds_remaining",
                "requested_mib": context.pod.memory_mib,
                "remaining_mib": context.node["memory_remaining"],
                "deficit_mib": (
                    context.pod.memory_mib - context.node["memory_remaining"]
                ),
            },
            source_field=f"pods[{context.pod.name}].memory_mib",
        )
    return None


def filter_taint_tolerance(context: FilterContext) -> FilterRejection | None:
    node_labels = context.node["labels"]
    for index, taint in enumerate(context.node["taints"]):
        if taint["effect"] not in ("NoSchedule", "NoExecute"):
            continue
        if any(
            _toleration_matches(taint, toleration)
            for toleration in context.pod.tolerations
        ):
            continue
        return FilterRejection(
            plugin="TaintTolerance",
            error_code="E_FILTER_TAINT",
            evidence={
                "kind": "taint_not_tolerated",
                "taint": dict(taint),
                "pod": context.pod.name,
                "node_labels": dict(node_labels),
            },
            source_field=f"nodes[{context.node['name']}].taints[{index}]",
        )
    return None


def filter_node_affinity(context: FilterContext) -> FilterRejection | None:
    node_labels = context.node["labels"]
    for index, expression in enumerate(context.pod.node_affinity.required):
        if _label_expression_matches(expression, node_labels):
            continue
        return FilterRejection(
            plugin="NodeAffinity",
            error_code="E_FILTER_NODE_AFFINITY",
            evidence={
                "kind": "node_affinity_required_mismatch",
                "expression": expression.to_dict(),
                "node_label_value": node_labels.get(expression.key),
                "node_has_key": expression.key in node_labels,
            },
            source_field=(
                f"pods[{context.pod.name}].node_affinity.required[{index}]"
            ),
        )
    for index, expression in enumerate(context.pod.node_anti_affinity.required):
        if not _label_expression_matches(expression, node_labels):
            continue
        return FilterRejection(
            plugin="NodeAffinity",
            error_code="E_FILTER_NODE_ANTI_AFFINITY",
            evidence={
                "kind": "node_anti_affinity_required_match",
                "expression": expression.to_dict(),
                "node_label_value": node_labels.get(expression.key),
            },
            source_field=(
                f"pods[{context.pod.name}].node_anti_affinity.required[{index}]"
            ),
        )
    return None


def _topology_domain(
    node: Mapping[str, Any],
    topology_key: str,
) -> str:
    labels = node["labels"]
    if topology_key in labels:
        return str(labels[topology_key])
    return str(node["name"])


def filter_pod_anti_affinity(context: FilterContext) -> FilterRejection | None:
    rule = context.pod.pod_anti_affinity
    if rule is None:
        return None
    node_by_name = {str(node["name"]): node for node in context.nodes}
    candidate_domain = _topology_domain(context.node, rule.topology_key)
    for allocation in sorted(
        context.allocations,
        key=lambda item: (int(item["round"]), str(item["pod"])),
    ):
        labels = allocation.get("labels", {})
        if not _selector_matches(rule.label_selector, labels):
            continue
        allocated_node = node_by_name.get(str(allocation["node"]))
        if allocated_node is None:
            continue
        domain = _topology_domain(allocated_node, rule.topology_key)
        if domain != candidate_domain:
            continue
        return FilterRejection(
            plugin="PodAntiAffinity",
            error_code="E_FILTER_POD_ANTI_AFFINITY",
            evidence={
                "kind": "matching_pod_in_topology_domain",
                "label_selector": dict(rule.label_selector),
                "topology_key": rule.topology_key,
                "domain": domain,
                "conflicting_pod": allocation["pod"],
                "conflicting_round": allocation["round"],
                "conflicting_node": allocation["node"],
            },
            source_field=f"pods[{context.pod.name}].pod_anti_affinity",
        )
    return None


def topology_counts_after_placement(
    *,
    context: FilterContext,
    constraint: TopologySpreadConstraint,
) -> tuple[dict[str, int], str]:
    domains = {
        _topology_domain(node, constraint.topology_key)
        for node in context.nodes
    }
    counts = {domain: 0 for domain in sorted(domains)}
    node_by_name = {str(node["name"]): node for node in context.nodes}
    for allocation in context.allocations:
        labels = allocation.get("labels", {})
        if not _selector_matches(constraint.label_selector, labels):
            continue
        allocated_node = node_by_name.get(str(allocation["node"]))
        if allocated_node is None:
            continue
        domain = _topology_domain(allocated_node, constraint.topology_key)
        counts[domain] = counts.get(domain, 0) + 1
    candidate_domain = _topology_domain(
        context.node, constraint.topology_key
    )
    counts[candidate_domain] = counts.get(candidate_domain, 0) + 1
    return counts, candidate_domain


def filter_topology_spread(context: FilterContext) -> FilterRejection | None:
    for index, constraint in enumerate(context.pod.topology_spread):
        counts, candidate_domain = topology_counts_after_placement(
            context=context,
            constraint=constraint,
        )
        maximum = max(counts.values(), default=0)
        minimum = min(counts.values(), default=0)
        skew = maximum - minimum
        if (
            skew <= constraint.max_skew
            or constraint.when_unsatisfiable == "ScheduleAnyway"
        ):
            continue
        return FilterRejection(
            plugin="TopologySpread",
            error_code="E_FILTER_TOPOLOGY_SPREAD",
            evidence={
                "kind": "topology_skew_exceeds_max",
                "topology_key": constraint.topology_key,
                "label_selector": dict(constraint.label_selector),
                "max_skew": constraint.max_skew,
                "candidate_domain": candidate_domain,
                "counts_after_placement": dict(sorted(counts.items())),
                "skew_after_placement": skew,
                "when_unsatisfiable": constraint.when_unsatisfiable,
            },
            source_field=(
                f"pods[{context.pod.name}].topology_spread[{index}]"
            ),
        )
    return None


FILTER_FUNCTIONS = {
    "ResourceFit": filter_resource_fit,
    "TaintTolerance": filter_taint_tolerance,
    "NodeAffinity": filter_node_affinity,
    "PodAntiAffinity": filter_pod_anti_affinity,
    "TopologySpread": filter_topology_spread,
}


def evaluate_filters(
    context: FilterContext,
    plugin_order: Sequence[str],
) -> tuple[FilterRejection | None, list[str]]:
    executed: list[str] = []
    for plugin in plugin_order:
        executed.append(plugin)
        rejection = FILTER_FUNCTIONS[plugin](context)
        if rejection is not None:
            return rejection, executed
    return None, executed
