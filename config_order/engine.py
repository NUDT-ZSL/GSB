"""Deterministic per-key ordering, conflict diagnosis, and resolution."""

from __future__ import annotations

import copy
from typing import Any

from .canonical import canonical_dumps, sha256_canonical
from .errors import E_CHAIN, E_CONFLICT, E_CYCLE, Issue


ALGORITHM = "explicit-topological-bias-tiebreak-v1"


def rule_fingerprint(scenario: dict[str, Any]) -> str:
    """Hash only inputs that can alter ordering or binding adjudication."""
    ordering_inputs = {
        "schema_version": scenario["schema_version"],
        "sources": [
            {"id": source["id"], "kind": source["kind"]}
            for source in sorted(scenario["sources"], key=lambda item: item["id"])
        ],
        "environments": [
            {
                "id": environment["id"],
                "level": environment["level"],
                "parent": environment["parent"],
                "available_sources": environment["available_sources"],
                "priority_bias": environment["priority_bias"],
                "order_rules": environment["order_rules"],
                "key_order_rules": environment["key_order_rules"],
                "bindings": environment["bindings"],
            }
            for environment in sorted(
                scenario["environments"],
                key=lambda item: item["id"],
            )
        ],
    }
    return sha256_canonical(ordering_inputs)


def input_fingerprint(scenario: dict[str, Any]) -> str:
    return sha256_canonical(scenario)


def _edge_sort_key(edge: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        edge["higher"],
        edge["lower"],
        edge["scope"],
        edge["config_key"],
        edge["id"],
    )


def _rules_for_key(
    environment: dict[str, Any], key: str, participants: set[str]
) -> list[dict[str, str]]:
    candidates = list(environment["order_rules"])
    candidates.extend(environment["key_order_rules"].get(key, []))
    by_pair: dict[tuple[str, str], dict[str, str]] = {}
    for rule in candidates:
        if rule["higher"] not in participants or rule["lower"] not in participants:
            continue
        pair = (rule["higher"], rule["lower"])
        existing = by_pair.get(pair)
        if existing is None or _edge_sort_key(rule) < _edge_sort_key(existing):
            by_pair[pair] = rule
    return sorted(by_pair.values(), key=_edge_sort_key)


def _find_cycle(
    nodes: list[str], edges: list[dict[str, str]]
) -> list[str] | None:
    adjacency: dict[str, list[str]] = {node: [] for node in nodes}
    for edge in edges:
        adjacency[edge["higher"]].append(edge["lower"])
    for node in nodes:
        adjacency[node] = sorted(set(adjacency[node]))

    state = {node: 0 for node in nodes}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for neighbor in adjacency[node]:
            if state[neighbor] == 0:
                found = visit(neighbor)
                if found is not None:
                    return found
            elif state[neighbor] == 1:
                start = path.index(neighbor)
                return path[start:] + [neighbor]
        path.pop()
        state[node] = 2
        return None

    for node in nodes:
        if state[node] == 0:
            found = visit(node)
            if found is not None:
                return found
    return None


def _rule_ref(rule: dict[str, str]) -> dict[str, str]:
    return {
        "rule_id": rule["id"],
        "scope": rule["scope"],
        "higher": rule["higher"],
        "lower": rule["lower"],
        "basis": rule["basis"],
    }


def _build_steps(
    environment: dict[str, Any],
    key: str,
    participants: list[str],
    edges: list[dict[str, str]],
    values: dict[str, Any],
) -> list[dict[str, Any]]:
    bias = environment["priority_bias"]
    nodes = set(participants)
    incoming: dict[str, list[dict[str, str]]] = {node: [] for node in nodes}
    outgoing: dict[str, list[dict[str, str]]] = {node: [] for node in nodes}
    indegree = {node: 0 for node in nodes}
    for edge in edges:
        incoming[edge["lower"]].append(edge)
        outgoing[edge["higher"]].append(edge)
        indegree[edge["lower"]] += 1
    for node in nodes:
        incoming[node].sort(key=_edge_sort_key)
        outgoing[node].sort(key=_edge_sort_key)

    ready = sorted(
        (node for node in nodes if indegree[node] == 0),
        key=lambda node: (-bias[node], node),
    )
    steps: list[dict[str, Any]] = []
    while ready:
        candidates = list(ready)
        chosen = ready.pop(0)
        max_bias = bias[chosen]
        if len(candidates) == 1:
            primary_basis = "explicit-order-unblocked"
        elif sum(1 for node in candidates if bias[node] == max_bias) == 1:
            primary_basis = "priority-bias"
        else:
            primary_basis = "stable-source-id-tiebreak"
        step = {
            "position": len(steps),
            "source": chosen,
            "value": copy.deepcopy(values[chosen]),
            "bias": bias[chosen],
            "constraints_before": [
                _rule_ref(rule) for rule in incoming[chosen]
            ],
            "constraints_after": [
                _rule_ref(rule) for rule in outgoing[chosen]
            ],
            "ready_candidates": candidates,
            "selection_basis": primary_basis,
        }
        steps.append(step)
        for edge in outgoing[chosen]:
            indegree[edge["lower"]] -= 1
            if indegree[edge["lower"]] == 0:
                ready.append(edge["lower"])
        ready.sort(key=lambda node: (-bias[node], node))
    return steps


def _non_monotonic_diagnostics(
    environment: dict[str, Any], steps: list[dict[str, Any]]
) -> dict[str, Any]:
    bias = environment["priority_bias"]
    inversions: list[dict[str, Any]] = []
    for left_index, left in enumerate(steps):
        for right in steps[left_index + 1 :]:
            if bias[left["source"]] < bias[right["source"]]:
                inversions.append(
                    {
                        "earlier_source": left["source"],
                        "earlier_bias": bias[left["source"]],
                        "later_source": right["source"],
                        "later_bias": bias[right["source"]],
                        "basis": "EXPLICIT_ORDER_OVERRIDES_BIAS",
                    }
                )
    return {
        "non_monotonic": bool(inversions),
        "bias_inversions": inversions,
    }


def _empty_chain(
    environment_id: str,
    key: str,
    participants: list[str],
    edges: list[dict[str, str]],
    environment: dict[str, Any],
) -> dict[str, Any]:
    return {
        "environment_id": environment_id,
        "key": key,
        "algorithm": ALGORITHM,
        "digest_algorithm": "sha256-canonical-json-v1",
        "participants": participants,
        "rules": [_rule_ref(rule) for rule in edges],
        "priority_bias": {
            source: environment["priority_bias"][source]
            for source in participants
        },
        "steps": [],
        "diagnostics": {"non_monotonic": False, "bias_inversions": []},
    }


def _finalize_chain(chain: dict[str, Any], cycle: list[str] | None) -> dict[str, Any]:
    if cycle is not None:
        chain["diagnostics"]["cycle"] = {
            "sources": cycle,
            "edges": [
                {
                    "higher": cycle[index],
                    "lower": cycle[index + 1],
                }
                for index in range(len(cycle) - 1)
            ],
        }
    chain["digest"] = sha256_canonical(chain)
    return chain


def build_chain(
    environment: dict[str, Any], key: str
) -> dict[str, Any]:
    env_id = environment["id"]
    source_values = environment["values"].get(key, {})
    participants = sorted(source_values)
    participant_set = set(participants)
    edges = _rules_for_key(environment, key, participant_set)
    chain = _empty_chain(env_id, key, participants, edges, environment)
    cycle = _find_cycle(participants, edges)
    if cycle is not None:
        return _finalize_chain(chain, cycle)
    steps = _build_steps(environment, key, participants, edges, source_values)
    chain["steps"] = steps
    chain["diagnostics"].update(_non_monotonic_diagnostics(environment, steps))
    return _finalize_chain(chain, None)


def _trace_chain(chain: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "step": 1,
            "type": "ORDER_CHAIN",
            "digest": chain["digest"],
            "sources": [step["source"] for step in chain["steps"]],
        }
    ]


def _binding(environment: dict[str, Any], key: str) -> dict[str, list[str]]:
    return environment["bindings"].get(key, {"require": [], "forbid": []})


def _error_decision(
    code: str,
    message: str,
    chain: dict[str, Any],
    binding: dict[str, list[str]],
    involved_sources: list[str],
    detail: dict[str, Any],
) -> dict[str, Any]:
    trace = _trace_chain(chain)
    trace.append(
        {
            "step": 2,
            "type": "PRIMARY_CAUSE",
            "code": code,
            "involved_sources": involved_sources,
            "binding": binding,
            "detail": detail,
        }
    )
    return {
        "status": "ERROR",
        "code": code,
        "effective_value": None,
        "selected_source": None,
        "selected_chain_position": None,
        "binding": binding,
        "coverage": [],
        "message": message,
        "involved_sources": involved_sources,
        "trace": trace,
    }


def _resolve_decision(
    environment: dict[str, Any], key: str, chain: dict[str, Any]
) -> dict[str, Any]:
    binding = _binding(environment, key)
    require = binding["require"][0] if binding["require"] else None
    forbid = set(binding["forbid"])
    participant_sources = [step["source"] for step in chain["steps"]]
    participant_set = set(participant_sources)

    cycle = chain["diagnostics"].get("cycle")
    if cycle is not None:
        cycle_sources = sorted(set(cycle["sources"]))
        cycle_rules = [
            rule
            for rule in chain["rules"]
            if (rule["higher"], rule["lower"])
            in {
                (cycle["sources"][index], cycle["sources"][index + 1])
                for index in range(len(cycle["sources"]) - 1)
            }
        ]
        return _error_decision(
            E_CYCLE,
            "explicit order rules form a cycle",
            chain,
            binding,
            cycle_sources,
            {
                "reason": "ORDER_CYCLE",
                "cycle_sources": cycle["sources"],
                "cycle_edges": cycle["edges"],
                "rule_basis": cycle_rules,
                "affected_sources": cycle_sources,
            },
        )

    if not participant_sources:
        return _error_decision(
            E_CONFLICT,
            "no source supplies a value for the bound key",
            chain,
            binding,
            sorted(set(binding["require"] + binding["forbid"])),
            {
                "reason": "NO_SOURCE_VALUE",
                "required_sources": binding["require"],
                "forbidden_sources": binding["forbid"],
            },
        )

    if require is not None and require not in participant_set:
        return _error_decision(
            E_CONFLICT,
            "the required source does not supply a value for this key",
            chain,
            binding,
            sorted(participant_set | {require}),
            {
                "reason": "REQUIRED_SOURCE_HAS_NO_VALUE",
                "required_source": require,
                "participants": participant_sources,
            },
        )

    eligible = [
        source
        for source in participant_sources
        if source not in forbid and (require is None or source == require)
    ]
    if not eligible:
        return _error_decision(
            E_CONFLICT,
            "binding constraints exclude every source in the order chain",
            chain,
            binding,
            participant_sources,
            {
                "reason": "NO_ELIGIBLE_SOURCE",
                "participants": participant_sources,
                "forbidden_sources": sorted(forbid),
                "required_source": require,
            },
        )

    selected_source = eligible[0]
    selected_step = next(
        step for step in chain["steps"] if step["source"] == selected_source
    )
    coverage: list[dict[str, Any]] = []
    for step in chain["steps"]:
        if step["source"] == selected_source:
            continue
        if step["source"] in forbid:
            reason = "BINDING_FORBIDDEN"
            basis = "binding.forbid"
        else:
            reason = "LOWER_PRIORITY"
            basis = "effective-order-after-selected-source"
        coverage.append(
            {
                "source": step["source"],
                "value": copy.deepcopy(step["value"]),
                "chain_position": step["position"],
                "bias": step["bias"],
                "reason": reason,
                "basis": basis,
                "selected_chain_position": selected_step["position"],
                "priority_delta": selected_step["bias"] - step["bias"],
                "chain_rule_basis": step["constraints_before"]
                + step["constraints_after"],
            }
        )

    trace = _trace_chain(chain)
    trace.extend(
        [
            {
                "step": 2,
                "type": "BINDING_CHECK",
                "binding": binding,
                "eligible_sources": eligible,
                "excluded_sources": [
                    source
                    for source in participant_sources
                    if source not in eligible
                ],
            },
            {
                "step": 3,
                "type": "SELECTION",
                "selected_source": selected_source,
                "selected_chain_position": selected_step["position"],
                "selection_basis": selected_step["selection_basis"],
            },
            {
                "step": 4,
                "type": "COVERAGE",
                "items": coverage,
            },
        ]
    )
    return {
        "status": "RESOLVED",
        "code": "OK",
        "effective_value": copy.deepcopy(selected_step["value"]),
        "selected_source": selected_source,
        "selected_chain_position": selected_step["position"],
        "binding": binding,
        "coverage": coverage,
        "message": "first binding-eligible source in the order chain selected",
        "involved_sources": participant_sources,
        "trace": trace,
    }


def diagnose_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    environment_reports: list[dict[str, Any]] = []
    chain_issues: list[Issue] = []
    resolved_count = 0
    conflict_count = 0
    cycle_count = 0
    chain_count = 0
    non_monotonic_keys: list[dict[str, str]] = []

    for environment in sorted(
        scenario["environments"], key=lambda item: item["id"]
    ):
        keys = sorted(
            set(environment["values"]) | set(environment["bindings"])
        )
        key_reports: list[dict[str, Any]] = []
        for key in keys:
            chain = build_chain(environment, key)
            decision = _resolve_decision(environment, key, chain)
            expected = environment["expected_chain_digests"].get(key)
            if expected is not None and expected != chain["digest"]:
                underlying = {
                    "status": decision["status"],
                    "code": decision["code"],
                    "selected_source": decision["selected_source"],
                }
                trace = copy.deepcopy(decision["trace"])
                trace.append(
                    {
                        "step": len(trace) + 1,
                        "type": "CHAIN_DIGEST_CHECK",
                        "expected": expected,
                        "actual": chain["digest"],
                        "status": "MISMATCH",
                    }
                )
                decision = {
                    "status": "ERROR",
                    "code": E_CHAIN,
                    "effective_value": None,
                    "selected_source": None,
                    "selected_chain_position": None,
                    "binding": decision["binding"],
                    "coverage": [],
                    "message": "computed order-chain digest differs from expectation",
                    "involved_sources": chain["participants"],
                    "underlying_result": underlying,
                    "expected_chain_digest": expected,
                    "actual_chain_digest": chain["digest"],
                    "trace": trace,
                }
                chain_issues.append(
                    Issue(
                        code=E_CHAIN,
                        field_path=(
                            f"environments.{environment['id']}."
                            f"expected_chain_digests.{key}"
                        ),
                        message="computed order-chain digest differs from expectation",
                        environment_id=environment["id"],
                        config_key=key,
                        detail={
                            "expected": expected,
                            "actual": chain["digest"],
                        },
                    )
                )

            if decision["code"] == "OK":
                resolved_count += 1
            elif decision["code"] == E_CONFLICT:
                conflict_count += 1
            elif decision["code"] == E_CYCLE:
                cycle_count += 1
            elif decision["code"] == E_CHAIN:
                chain_count += 1
            if chain["diagnostics"]["non_monotonic"]:
                non_monotonic_keys.append(
                    {"environment_id": environment["id"], "key": key}
                )
            key_reports.append(
                {
                    "key": key,
                    "status": decision["status"],
                    "code": decision["code"],
                    "chain": chain,
                    "decision": decision,
                }
            )

        environment_reports.append(
            {
                "environment_id": environment["id"],
                "level": environment["level"],
                "parent": environment["parent"],
                "available_sources": environment["available_sources"],
                "order_context": {
                    "algorithm": ALGORITHM,
                    "priority_bias": environment["priority_bias"],
                    "order_rules": environment["order_rules"],
                    "key_order_rules": environment["key_order_rules"],
                    "bindings": environment["bindings"],
                },
                "keys": key_reports,
            }
        )

    codes = sorted(
        {
            key_report["code"]
            for environment in environment_reports
            for key_report in environment["keys"]
            if key_report["code"] != "OK"
        }
    )
    if chain_count:
        overall_code = E_CHAIN
    elif cycle_count:
        overall_code = E_CYCLE
    elif conflict_count:
        overall_code = E_CONFLICT
    else:
        overall_code = "OK"

    result = {
        "schema_version": "1.0",
        "status": "DIAGNOSED",
        "environment_set_version": scenario["environment_set_version"],
        "algorithm": ALGORITHM,
        "source_registry": scenario["sources"],
        "environments": environment_reports,
        "statistics": {
            "environments": len(environment_reports),
            "keys": resolved_count + conflict_count + cycle_count + chain_count,
            "resolved": resolved_count,
            "conflict": conflict_count,
            "cycle": cycle_count,
            "chain_integrity": chain_count,
            "non_monotonic_keys": len(non_monotonic_keys),
        },
        "non_monotonic_keys": non_monotonic_keys,
        "business_codes": codes,
        "overall_code": overall_code,
        "business_code": overall_code,
    }
    if chain_issues:
        result["chain_issues"] = [issue.as_dict() for issue in chain_issues]
    return result


def explain_order(chain: dict[str, Any]) -> str:
    """Small deterministic trace helper used by tests and the CLI demo."""
    return canonical_dumps(
        [
            {"position": step["position"], "source": step["source"]}
            for step in chain["steps"]
        ]
    )
