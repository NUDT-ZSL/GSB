"""Cross-environment consistency and dependency-derived quantities."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from .canonical import decimal_of, decimal_text
from .errors import EngineError
from .injection import InjectionController
from .validation import RESOURCE_FIELDS


CONSISTENCY_PARAMETERS = ("timeout_ms", "retry_factor", "resource_limit")


def _active(service: dict[str, Any]) -> bool:
    return not service["tombstoned"] and not service["expired_at_clock"]


def _value(service: dict[str, Any], parameter: str) -> Any:
    record = service["params"].get(parameter)
    return None if record is None else record["value"]


def _graph(
    environments: dict[str, dict[str, dict[str, Any]]],
    environment_order: list[str],
    injections: InjectionController,
) -> dict[str, dict[str, list[str]]]:
    graph: dict[str, dict[str, list[str]]] = {}
    for environment in environment_order:
        graph[environment] = {}
        for service_name in sorted(environments[environment]):
            service = environments[environment][service_name]
            if not _active(service):
                continue
            graph[environment][service_name] = sorted(service["upstreams"])
    cycle_injection = injections.find("evaluate", "dependency_cycle")
    if cycle_injection is not None:
        environment = cycle_injection.parameters.get(
            "environment", environment_order[0]
        )
        service = cycle_injection.parameters.get("service", "auth-service")
        upstream = cycle_injection.parameters.get("add_upstream", "gateway")
        if environment in graph and service in graph[environment]:
            graph[environment][service] = sorted(
                set(graph[environment][service] + [upstream])
            )
    return graph


def _topological_order(
    environment: str,
    graph: dict[str, list[str]],
) -> list[str]:
    state: dict[str, int] = {}
    stack: list[str] = []
    order: list[str] = []

    def visit(node: str) -> None:
        current = state.get(node, 0)
        if current == 1:
            start = stack.index(node)
            cycle = stack[start:] + [node]
            raise EngineError(
                "E_DEPENDENCY_CYCLE",
                "dependency graph contains a cycle",
                stage="evaluate",
                details={
                    "environment": environment,
                    "cycle": cycle,
                },
            )
        if current == 2:
            return
        state[node] = 1
        stack.append(node)
        for upstream in graph.get(node, []):
            if upstream not in graph:
                raise EngineError(
                    "E_DEPENDENCY_MISSING",
                    "service depends on an unavailable upstream service",
                    stage="evaluate",
                    details={
                        "environment": environment,
                        "service": node,
                        "upstream": upstream,
                    },
                )
            visit(upstream)
        stack.pop()
        state[node] = 2
        order.append(node)

    for node in sorted(graph):
        visit(node)
    return order


def _derive(
    environments: dict[str, dict[str, dict[str, Any]]],
    environment_order: list[str],
    graph: dict[str, dict[str, list[str]]],
) -> dict[str, dict[str, Any]]:
    derivations: dict[str, dict[str, Any]] = {}
    for environment in environment_order:
        order = _topological_order(environment, graph[environment])
        chain_timeout: dict[str, int] = {}
        effective_resource: dict[str, dict[str, int]] = {}
        worst_upstream: dict[str, str | None] = {}
        chain_path: dict[str, list[str]] = {}
        for service_name in order:
            service = environments[environment][service_name]
            own_timeout = int(_value(service, "timeout_ms"))
            own_resource = _value(service, "resource_limit")
            best_upstream: str | None = None
            best_timeout = 0
            for upstream in sorted(graph[environment][service_name]):
                candidate_timeout = chain_timeout[upstream]
                if candidate_timeout > best_timeout:
                    best_timeout = candidate_timeout
                    best_upstream = upstream
            chain_timeout[service_name] = own_timeout + best_timeout
            if best_upstream is None:
                resource = {
                    field: int(own_resource[field]) for field in RESOURCE_FIELDS
                }
                chain_path[service_name] = [service_name]
                worst_upstream[service_name] = None
            else:
                upstream_resource = effective_resource[best_upstream]
                resource = {
                    field: min(
                        int(own_resource[field]),
                        int(upstream_resource[field]),
                    )
                    for field in RESOURCE_FIELDS
                }
                chain_path[service_name] = [
                    service_name,
                    *chain_path[best_upstream],
                ]
                worst_upstream[service_name] = best_upstream
            effective_resource[service_name] = resource
            derivations.setdefault(environment, {})[service_name] = {
                "timeout_ms": chain_timeout[service_name],
                "resource_limit": resource,
                "worst_upstream": worst_upstream[service_name],
                "dependency_chain": chain_path[service_name],
            }
    return derivations


def _scalar_divergence(
    values: dict[str, Any],
    *,
    tolerance: Decimal,
    baseline_environment: str,
) -> dict[str, Any] | None:
    decimals = {environment: decimal_of(value) for environment, value in values.items()}
    baseline = decimals[baseline_environment]
    involved = [
        environment
        for environment, value in decimals.items()
        if abs(value - baseline) > tolerance
    ]
    if not involved:
        return None
    low = min(decimals.values())
    high = max(decimals.values())
    maximum_difference = high - low
    return {
        "values": values,
        "baseline_environment": baseline_environment,
        "involved_environments": involved,
        "max_difference": decimal_text(maximum_difference),
        "tolerance": decimal_text(tolerance),
        "excess": decimal_text(maximum_difference - tolerance),
    }


def _parameter_divergence(
    service_name: str,
    parameter: str,
    per_environment: dict[str, dict[str, Any]],
    environment_order: list[str],
    tolerances: dict[str, Any],
) -> dict[str, Any] | None:
    baseline = environment_order[0]
    if parameter != "resource_limit":
        values = {
            environment: _value(per_environment[environment], parameter)
            for environment in environment_order
        }
        divergence = _scalar_divergence(
            values,
            tolerance=decimal_of(tolerances[parameter]),
            baseline_environment=baseline,
        )
        if divergence is None:
            return None
        return {
            "service": service_name,
            "parameter": parameter,
            **divergence,
        }

    field_results: list[dict[str, Any]] = []
    for field in RESOURCE_FIELDS:
        values = {
            environment: _value(per_environment[environment], "resource_limit")[
                field
            ]
            for environment in environment_order
        }
        divergence = _scalar_divergence(
            values,
            tolerance=decimal_of(tolerances["resource_limit"][field]),
            baseline_environment=baseline,
        )
        if divergence is not None:
            field_results.append({"field": field, **divergence})
    if not field_results:
        return None
    return {
        "service": service_name,
        "parameter": "resource_limit",
        "fields": field_results,
        "involved_environments": sorted(
            {
                environment
                for result in field_results
                for environment in result["involved_environments"]
            },
            key=environment_order.index,
        ),
    }


def _consistency(
    environments: dict[str, dict[str, dict[str, Any]]],
    environment_order: list[str],
    tolerances: dict[str, Any],
) -> dict[str, Any]:
    all_services = sorted(
        {
            service
            for environment in environment_order
            for service in environments[environment]
        }
    )
    results: list[dict[str, Any]] = []
    first_divergence: dict[str, Any] | None = None
    for service_name in all_services:
        per_environment: dict[str, dict[str, Any]] = {}
        states: list[str] = []
        for environment in environment_order:
            service = environments[environment].get(service_name)
            if service is None:
                states.append("missing")
            elif service["tombstoned"]:
                states.append("tombstoned")
            elif service["expired_at_clock"]:
                states.append("expired")
            else:
                states.append("active")
                per_environment[environment] = service
        if any(state != "active" for state in states):
            results.append(
                {
                    "service": service_name,
                    "status": (
                        "active"
                        if all(state == "active" for state in states)
                        else next(state for state in states if state != "active")
                    ),
                    "consistent": None,
                    "divergences": [],
                    "environment_states": dict(zip(environment_order, states)),
                }
            )
            continue
        divergences: list[dict[str, Any]] = []
        for parameter in CONSISTENCY_PARAMETERS:
            divergence = _parameter_divergence(
                service_name,
                parameter,
                per_environment,
                environment_order,
                tolerances,
            )
            if divergence is not None:
                divergences.append(divergence)
        result = {
            "service": service_name,
            "status": "active",
            "consistent": not divergences,
            "divergences": divergences,
            "first_divergence": divergences[0] if divergences else None,
            "environment_states": dict(zip(environment_order, states)),
        }
        if divergences and first_divergence is None:
            first_divergence = divergences[0]
        results.append(result)
    return {
        "environments": environment_order,
        "parameters": list(CONSISTENCY_PARAMETERS),
        "tolerances": tolerances,
        "results": results,
        "consistent": first_divergence is None,
        "first_divergence": first_divergence,
    }


def evaluate_view(
    merged: dict[str, Any],
    *,
    environment_order: list[str],
    tolerances: dict[str, Any],
    injections: InjectionController,
) -> dict[str, Any]:
    environments = merged["environments"]
    missing_environments = sorted(set(environment_order) - set(environments))
    if missing_environments:
        raise EngineError(
            "E_BAD_MANIFEST",
            "merged view is missing declared environments",
            stage="evaluate",
            details={"environments": missing_environments},
        )
    graph = _graph(environments, environment_order, injections)
    derivations = _derive(environments, environment_order, graph)
    consistency = _consistency(environments, environment_order, tolerances)
    output = copy.deepcopy(merged)
    output["derivations"] = derivations
    output["consistency"] = consistency
    output["compatibility"] = {
        "clock_ms": merged["clock_ms"],
        "clock_basis": "injected_clock_ms",
        "wall_clock_read": False,
    }
    return output
