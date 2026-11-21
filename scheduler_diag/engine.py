from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from .clock import LogicalClock
from .filters import FilterContext, FilterRejection, evaluate_filters
from .models import Bundle, Injection, PodSpec, SCORE_PLUGINS
from .scoring import score_nodes


def _node_by_name(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(node["name"]): node for node in state["nodes"]}


def _injection_evidence(
    *,
    injection: Injection,
    pod: PodSpec,
    node: Mapping[str, Any],
    bundle: Bundle,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "kind": f"injected_{injection.type}",
        "injection_id": injection.id,
        "injected": True,
        "round": injection.round,
        "pod": injection.pod,
        "node": injection.node,
        "plugin": injection.plugin,
        "message": injection.message,
    }
    evidence.update(dict(injection.details))
    if injection.type == "node_label_conflict":
        evidence.setdefault(
            "conflicting_labels",
            {
                key: value
                for key, value in sorted(node["labels"].items())
                if key in injection.details
            },
        )
    elif injection.type == "resource_overcommit":
        evidence.setdefault("requested_millicores", pod.cpu_millicores)
        evidence.setdefault("requested_mib", pod.memory_mib)
        evidence.setdefault("remaining_millicores", node["cpu_remaining"])
        evidence.setdefault("remaining_mib", node["memory_remaining"])
    elif injection.type == "invalid_weight":
        evidence.setdefault(
            "configured_weight",
            str(bundle.config.score_weights.get(injection.plugin)),
        )
        evidence.setdefault("injected_weight", "-1")
    elif injection.type == "plugin_missing":
        evidence.setdefault("missing_plugin", injection.plugin)
    return evidence


def _injection_elimination(
    *,
    injection: Injection,
    pod: PodSpec,
    node: Mapping[str, Any],
    bundle: Bundle,
) -> dict[str, Any]:
    return {
        "plugin": injection.plugin,
        "node": injection.node,
        "error_code": injection.error_code,
        "evidence": _injection_evidence(
            injection=injection,
            pod=pod,
            node=node,
            bundle=bundle,
        ),
        "source_field": f"injections[{injection.id}]",
    }


def _filter_elimination(rejection: FilterRejection, node_name: str) -> dict[str, Any]:
    return {
        "plugin": rejection.plugin,
        "node": node_name,
        "error_code": rejection.error_code,
        "evidence": dict(rejection.evidence),
        "source_field": rejection.source_field,
    }


def _base_entry(
    *,
    bundle: Bundle,
    pod: PodSpec,
    round_number: int,
    clock: LogicalClock,
) -> dict[str, Any]:
    return {
        "round": round_number,
        "pod": pod.name,
        "input_digest": bundle.digest,
        "input_summary": bundle.summary,
        "logical_ms": clock.logical_ms,
        "logical_round_ms": clock.round_elapsed_ms,
        "injection_phase": None,
    }


def _terminal(
    *,
    bundle: Bundle,
    pod: PodSpec,
    round_number: int,
    status: str,
    plugin: str,
    node: str | None,
    error_code: str | None,
    clock: LogicalClock,
    eliminations: list[dict[str, Any]],
    scoring: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], None]:
    entry = _base_entry(
        bundle=bundle,
        pod=pod,
        round_number=round_number,
        clock=clock,
    )
    entry.update(
        {
            "status": status,
            "plugin": plugin,
            "selected_node": node if status == "bound" else None,
            "attribution_node": node,
            "error_code": error_code,
            "before_resources": None,
            "after_resources": None,
            "eliminations": eliminations,
            "scoring": dict(scoring) if scoring is not None else None,
        }
    )
    return entry, None


def _injection_failure(
    *,
    bundle: Bundle,
    pod: PodSpec,
    round_number: int,
    injection: Injection,
    node: Mapping[str, Any],
    clock: LogicalClock,
    scoring: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], None]:
    entry, _ = _terminal(
        bundle=bundle,
        pod=pod,
        round_number=round_number,
        status=(
            "injected_filter_failure"
            if injection.phase == "filter"
            else "injected_score_failure"
        ),
        plugin=injection.plugin,
        node=injection.node,
        error_code=injection.error_code,
        clock=clock,
        eliminations=[
            _injection_elimination(
                injection=injection,
                pod=pod,
                node=node,
                bundle=bundle,
            )
        ],
        scoring=scoring,
    )
    entry["injection_phase"] = injection.phase
    entry["injection"] = injection.to_dict()
    return entry, None


def _time_budget_failure(
    *,
    bundle: Bundle,
    pod: PodSpec,
    round_number: int,
    clock: LogicalClock,
    budget_ms: int,
) -> tuple[dict[str, Any], None]:
    entry, _ = _terminal(
        bundle=bundle,
        pod=pod,
        round_number=round_number,
        status="time_budget_failure",
        plugin="RoundTimeBudget",
        node=None,
        error_code="E_TIME_BUDGET",
        clock=clock,
        eliminations=[
            {
                "plugin": "RoundTimeBudget",
                "node": None,
                "error_code": "E_TIME_BUDGET",
                "evidence": {
                    "kind": "round_time_budget_exceeded",
                    "budget_ms": budget_ms,
                    "round_elapsed_ms": clock.round_elapsed_ms,
                },
                "source_field": "config.round_time_budget_ms",
            }
        ],
    )
    return entry, None


def _score_leader(scoring: Mapping[str, Any]) -> str | None:
    ranking = scoring.get("ranking", [])
    if not ranking:
        return None
    first = ranking[0]
    terms = first.get("normalized_terms", {})
    if not terms:
        return None
    return sorted(
        SCORE_PLUGINS,
        key=lambda plugin: (-Decimal(str(terms.get(plugin, "0"))), plugin),
    )[0]


def evaluate_pod(
    *,
    bundle: Bundle,
    pod: PodSpec,
    round_number: int,
    state: Mapping[str, Any],
    clock: LogicalClock,
) -> tuple[dict[str, Any], dict[str, Any] | None, Injection | None]:
    clock.enter_round(round_number)
    nodes = list(state["nodes"])
    node_map = _node_by_name(state)
    allocations = list(state["allocations"])
    injection = bundle.injections_by_pod_round.get((pod.name, round_number))

    if injection is not None and injection.phase == "filter":
        clock.tick(1)
        entry, binding = _injection_failure(
            bundle=bundle,
            pod=pod,
            round_number=round_number,
            injection=injection,
            node=node_map[injection.node],
            clock=clock,
        )
        return entry, binding, injection

    eliminations: list[dict[str, Any]] = []
    node_evaluations: list[dict[str, Any]] = []
    feasible_contexts: list[FilterContext] = []
    for node in nodes:
        context = FilterContext(
            pod=pod,
            node=node,
            nodes=nodes,
            allocations=allocations,
        )
        rejection, executed = evaluate_filters(
            context,
            bundle.config.filter_plugins,
        )
        clock.tick(len(executed))
        evaluation: dict[str, Any] = {
            "node": node["name"],
            "executed_plugins": executed,
            "result": "eliminated" if rejection is not None else "feasible",
        }
        if rejection is not None:
            elimination = _filter_elimination(rejection, str(node["name"]))
            eliminations.append(elimination)
            evaluation["elimination"] = elimination
        else:
            feasible_contexts.append(context)
        node_evaluations.append(evaluation)

    if not feasible_contexts:
        entry, binding = _terminal(
            bundle=bundle,
            pod=pod,
            round_number=round_number,
            status="unschedulable",
            plugin="FilterChain",
            node=None,
            error_code="E_NO_FEASIBLE_NODE",
            clock=clock,
            eliminations=eliminations,
        )
        entry["node_evaluations"] = node_evaluations
        return entry, binding, injection

    scoring = score_nodes(feasible_contexts, bundle.config.score_weights)
    clock.tick(len(feasible_contexts) * len(SCORE_PLUGINS))
    ranking_order = scoring.pop("_ranking_order")

    if injection is not None and injection.phase == "score":
        entry, binding = _injection_failure(
            bundle=bundle,
            pod=pod,
            round_number=round_number,
            injection=injection,
            node=node_map[injection.node],
            clock=clock,
            scoring=scoring,
        )
        entry["node_evaluations"] = node_evaluations
        return entry, binding, injection

    if clock.round_elapsed_ms > bundle.config.round_time_budget_ms:
        entry, binding = _time_budget_failure(
            bundle=bundle,
            pod=pod,
            round_number=round_number,
            clock=clock,
            budget_ms=bundle.config.round_time_budget_ms,
        )
        entry["node_evaluations"] = node_evaluations
        entry["scoring"] = scoring
        return entry, binding, injection

    selected_node = ranking_order[0]
    target = node_map[selected_node]
    before = {
        "cpu_millicores": int(target["cpu_remaining"]),
        "memory_mib": int(target["memory_remaining"]),
    }
    after = {
        "cpu_millicores": before["cpu_millicores"] - pod.cpu_millicores,
        "memory_mib": before["memory_mib"] - pod.memory_mib,
    }
    entry = _base_entry(
        bundle=bundle,
        pod=pod,
        round_number=round_number,
        clock=clock,
    )
    entry.update(
        {
            "status": "bound",
            "plugin": "Bind",
            "score_leader": _score_leader(scoring),
            "selected_node": selected_node,
            "attribution_node": selected_node,
            "error_code": None,
            "before_resources": before,
            "after_resources": after,
            "eliminations": eliminations,
            "node_evaluations": node_evaluations,
            "scoring": scoring,
        }
    )
    binding = {
        "round": round_number,
        "pod": pod.name,
        "node": selected_node,
        "cpu_millicores": pod.cpu_millicores,
        "memory_mib": pod.memory_mib,
    }
    return entry, binding, injection
