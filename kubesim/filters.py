from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import (
    FILTER_ORDER,
    Pod,
    ScheduleConfig,
    Taint,
    Toleration,
    labels_match,
)
from .runtime import ClusterState, NodeState


@dataclass(frozen=True)
class PluginDecision:
    plugin: str
    sequence: int
    passed: bool
    reason: str | None
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "sequence": self.sequence,
            "passed": self.passed,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class NodeFilterResult:
    node_name: str
    plugins: tuple[PluginDecision, ...]
    passed: bool
    first_failure_sequence: int | None
    first_failure_plugin: str | None
    first_failure_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node_name,
            "passed": self.passed,
            "plugins": [decision.to_dict() for decision in self.plugins],
            "first_failure_sequence": self.first_failure_sequence,
            "first_failure_plugin": self.first_failure_plugin,
            "first_failure_reason": self.first_failure_reason,
        }


def _tolerates(toleration: Toleration, taint: Taint) -> bool:
    if toleration.effect is not None and toleration.effect != taint.effect:
        return False
    if toleration.operator == "Exists":
        return toleration.key == "" or toleration.key == taint.key
    return toleration.key == taint.key and toleration.value == taint.value


def _resource_decision(
    pod: Pod,
    node: NodeState,
) -> PluginDecision:
    request = pod.resources
    remaining = node.remaining
    if request.cpu_millicores > remaining.cpu_millicores:
        return PluginDecision(
            "resource_feasibility",
            1,
            False,
            "INSUFFICIENT_CPU",
            {
                "requested_cpu_millicores": request.cpu_millicores,
                "remaining_cpu_millicores": remaining.cpu_millicores,
            },
        )
    if request.memory_mib > remaining.memory_mib:
        return PluginDecision(
            "resource_feasibility",
            1,
            False,
            "INSUFFICIENT_MEMORY",
            {
                "requested_memory_mib": request.memory_mib,
                "remaining_memory_mib": remaining.memory_mib,
            },
        )
    if request.gpu > remaining.gpu:
        return PluginDecision(
            "resource_feasibility",
            1,
            False,
            "INSUFFICIENT_GPU",
            {"requested_gpu": request.gpu, "remaining_gpu": remaining.gpu},
        )
    return PluginDecision(
        "resource_feasibility",
        1,
        True,
        None,
        {
            "requested": request.to_dict(),
            "remaining": remaining.to_dict(),
            "integer_units": {
                "cpu": "millicores",
                "memory": "MiB",
                "gpu": "cards",
            },
        },
    )


def _taint_decision(pod: Pod, node: NodeState) -> PluginDecision:
    offenders: list[dict[str, Any]] = []
    for taint in node.taints:
        if taint.effect not in {"NoSchedule", "NoExecute"}:
            continue
        if not any(_tolerates(toleration, taint) for toleration in pod.tolerations):
            offenders.append(taint.to_dict())
    if offenders:
        return PluginDecision(
            "taint_toleration",
            2,
            False,
            "UNRECOGNIZED_TAINT",
            {"untolerated_taints": offenders},
        )
    return PluginDecision(
        "taint_toleration",
        2,
        True,
        None,
        {
            "evaluated_taints": [
                taint.to_dict()
                for taint in node.taints
                if taint.effect in {"NoSchedule", "NoExecute"}
            ]
        },
    )


def _term_matches(labels: dict[str, str], term) -> bool:
    return labels_match(labels, (), term)


def _affinity_decision(pod: Pod, node: NodeState) -> PluginDecision:
    affinity = pod.node_affinity
    selector_failures: list[dict[str, Any]] = []
    for key, value in affinity.required_selector:
        if node.labels.get(key) != value:
            selector_failures.append(
                {"key": key, "operator": "=", "expected": value, "actual": node.labels.get(key)}
            )
    term_matches = [
        _term_matches(node.labels, term) for term in affinity.required_terms
    ]
    terms_ok = not affinity.required_terms or any(term_matches)
    if selector_failures or not terms_ok:
        return PluginDecision(
            "node_affinity",
            3,
            False,
            "NODE_AFFINITY_MISMATCH",
            {
                "selector_failures": selector_failures,
                "required_term_matches": term_matches,
            },
        )
    return PluginDecision(
        "node_affinity",
        3,
        True,
        None,
        {
            "selector_matches": len(affinity.required_selector),
            "required_term_matches": term_matches,
        },
    )


def _anti_affinity_decision(
    pod: Pod,
    node: NodeState,
    state: ClusterState,
) -> PluginDecision:
    conflicts: list[dict[str, Any]] = []
    nodes = state.nodes_by_name
    for rule_index, rule in enumerate(pod.anti_affinity):
        candidate_domain = node.labels.get(rule.topology_key)
        if candidate_domain is None:
            return PluginDecision(
                "pod_anti_affinity",
                4,
                False,
                "MISSING_TOPOLOGY_KEY",
                {"rule_index": rule_index, "topology_key": rule.topology_key},
            )
        for bound_pod in state.bound_pods:
            if bound_pod.name == pod.name:
                continue
            if not labels_match(
                bound_pod.labels,
                rule.match_labels,
                rule.match_expressions,
            ):
                continue
            bound_node = nodes.get(bound_pod.node_name)
            if bound_node is None:
                continue
            if bound_node.labels.get(rule.topology_key) == candidate_domain:
                conflicts.append(
                    {
                        "rule_index": rule_index,
                        "topology_key": rule.topology_key,
                        "domain": candidate_domain,
                        "conflicting_pod": bound_pod.name,
                        "conflicting_node": bound_pod.node_name,
                    }
                )
    if conflicts:
        return PluginDecision(
            "pod_anti_affinity",
            4,
            False,
            "POD_ANTI_AFFINITY_CONFLICT",
            {"conflicts": conflicts},
        )
    return PluginDecision(
        "pod_anti_affinity",
        4,
        True,
        None,
        {"rules_evaluated": len(pod.anti_affinity)},
    )


def topology_distribution(
    constraint,
    state: ClusterState,
    *,
    excluded_pod: str | None = None,
) -> dict[str, int]:
    nodes = state.nodes_by_name
    domains = sorted(
        {
            node.labels[constraint.topology_key]
            for node in state.nodes
            if constraint.topology_key in node.labels
        }
    )
    counts = {domain: 0 for domain in domains}
    for bound_pod in state.bound_pods:
        if bound_pod.name == excluded_pod:
            continue
        if not labels_match(
            bound_pod.labels,
            constraint.match_labels,
            constraint.match_expressions,
        ):
            continue
        bound_node = nodes.get(bound_pod.node_name)
        if bound_node is None:
            continue
        domain = bound_node.labels.get(constraint.topology_key)
        if domain in counts:
            counts[domain] += 1
    return counts


def _topology_decision(
    pod: Pod,
    node: NodeState,
    state: ClusterState,
) -> PluginDecision:
    checks: list[dict[str, Any]] = []
    for index, constraint in enumerate(pod.topology_spread):
        if constraint.when_unsatisfiable == "ScheduleAnyway":
            checks.append(
                {
                    "constraint_index": index,
                    "topology_key": constraint.topology_key,
                    "enforced": False,
                    "passed": True,
                }
            )
            continue
        candidate_domain = node.labels.get(constraint.topology_key)
        if candidate_domain is None:
            return PluginDecision(
                "topology_spread",
                5,
                False,
                "MISSING_TOPOLOGY_KEY",
                {
                    "constraint_index": index,
                    "topology_key": constraint.topology_key,
                },
            )
        counts = topology_distribution(constraint, state, excluded_pod=pod.name)
        if candidate_domain not in counts:
            return PluginDecision(
                "topology_spread",
                5,
                False,
                "DOMAIN_NOT_ELIGIBLE",
                {
                    "constraint_index": index,
                    "topology_key": constraint.topology_key,
                    "domain": candidate_domain,
                },
            )
        if len(counts) < constraint.min_domains:
            return PluginDecision(
                "topology_spread",
                5,
                False,
                "INSUFFICIENT_DOMAINS",
                {
                    "constraint_index": index,
                    "topology_key": constraint.topology_key,
                    "domains_available": len(counts),
                    "min_domains": constraint.min_domains,
                },
            )
        after = dict(counts)
        after[candidate_domain] += 1
        skew = max(after.values()) - min(after.values())
        passed = skew <= constraint.max_skew
        checks.append(
            {
                "constraint_index": index,
                "topology_key": constraint.topology_key,
                "domain": candidate_domain,
                "counts_before": counts,
                "counts_after": after,
                "skew_after": skew,
                "max_skew": constraint.max_skew,
                "min_domains": constraint.min_domains,
                "enforced": True,
                "passed": passed,
            }
        )
        if not passed:
            return PluginDecision(
                "topology_spread",
                5,
                False,
                "TOPOLOGY_SKEW_EXCEEDED",
                {"checks": checks},
            )
    return PluginDecision(
        "topology_spread",
        5,
        True,
        None,
        {"checks": checks},
    )


def evaluate_filters(
    pod: Pod,
    state: ClusterState,
    config: ScheduleConfig,
) -> tuple[NodeFilterResult, ...]:
    enabled = set(config.filters)
    results: list[NodeFilterResult] = []
    for node in sorted(state.nodes, key=lambda item: item.name):
        decisions: list[PluginDecision] = []
        if "resource_feasibility" in enabled:
            decisions.append(_resource_decision(pod, node))
        if "taint_toleration" in enabled:
            decisions.append(_taint_decision(pod, node))
        if "node_affinity" in enabled:
            decisions.append(_affinity_decision(pod, node))
        if "pod_anti_affinity" in enabled:
            decisions.append(_anti_affinity_decision(pod, node, state))
        if "topology_spread" in enabled:
            decisions.append(_topology_decision(pod, node, state))

        failures = [decision for decision in decisions if not decision.passed]
        first = min(failures, key=lambda item: item.sequence) if failures else None
        results.append(
            NodeFilterResult(
                node_name=node.name,
                plugins=tuple(decisions),
                passed=first is None,
                first_failure_sequence=first.sequence if first else None,
                first_failure_plugin=first.plugin if first else None,
                first_failure_reason=first.reason if first else None,
            )
        )
    return tuple(results)


def plugin_sequence(plugin: str) -> int:
    return FILTER_ORDER.index(plugin) + 1
