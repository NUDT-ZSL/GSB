"""Drift attribution closure over the declared dependency graph."""

from __future__ import annotations

from . import codes
from .spec import BaselineSpec


def _reverse_adjacency(spec: BaselineSpec) -> dict[str, set[str]]:
    # spec.adjacency[dependent] = {dependency}; reverse edge points from a
    # drifting dependency to every config that depends on it (propagation path).
    reverse: dict[str, set[str]] = {key: set() for key in spec.configs}
    for dependent, dependencies in spec.adjacency.items():
        for dependency in dependencies:
            reverse[dependency].add(dependent)
    return reverse


def _cycle_from(start: str, spec: BaselineSpec) -> list[str]:
    """Return the canonical ordered cycle reached from ``start`` or []."""
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    visited_paths: set[str] = set()
    while stack:
        node, path = stack.pop()
        for nxt in sorted(spec.adjacency.get(node, ())):
            if nxt in path:
                cycle = path[path.index(nxt):] + [nxt]
                return cycle[:-1]
            token = "|".join(path + [nxt])
            if token not in visited_paths:
                visited_paths.add(token)
                stack.append((nxt, path + [nxt]))
    return []


def attribute(active_drift_keys: list[str], spec: BaselineSpec, host: str) -> dict:
    """Compute primary/secondary causes and service-level root-cause candidates.

    Active drift keys are the observed primary causes. Propagation follows
    reverse dependency edges to every dependent config. All output ordering is
    lexicographic and therefore independent of snapshot import order.
    """
    active = sorted(set(active_drift_keys))
    cycle = []
    for root in active:
        cycle = _cycle_from(root, spec)
        if cycle:
            break
    if cycle:
        return {
            "host": host,
            "service": spec.service_of(host),
            "code": codes.E_DEP_CYCLE,
            "cycle": cycle,
            "primary_causes": [],
            "secondary_causes": [],
            "service_root_candidates": [],
        }

    reverse = _reverse_adjacency(spec)
    secondary: dict[str, list[str]] = {}
    frontier = list(active)
    seen = set(active)
    while frontier:
        node = frontier.pop()
        for dependent in sorted(reverse.get(node, ())):
            if dependent in seen:
                continue
            seen.add(dependent)
            secondary.setdefault(dependent, []).append(node)
            frontier.append(dependent)

    secondary_rows = [
        {"key": key, "propagated_via": sorted(set(paths))}
        for key, paths in sorted(secondary.items())
    ]
    service = spec.service_of(host)
    candidates = sorted({
        f"{service}:{key}" for key in active
    } | {
        f"{service}:{row['key']}" for row in secondary_rows
    })
    return {
        "host": host,
        "service": service,
        "code": None,
        "cycle": [],
        "primary_causes": active,
        "secondary_causes": secondary_rows,
        "service_root_candidates": candidates,
    }


def attribute_hosts(decisions: list[dict], spec: BaselineSpec) -> list[dict]:
    """Run the closure per host using active, non-revoked, non-frozen drift."""
    by_host: dict[str, list[str]] = {}
    for decision in decisions:
        if decision["verdict"] in (codes.D_NOMINAL, codes.D_REVOKED):
            continue
        by_host.setdefault(decision["host"], []).append(decision["key"])
    return [attribute(keys, spec, host) for host, keys in sorted(by_host.items())]
