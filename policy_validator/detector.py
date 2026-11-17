"""Conflict / shadow / dependency-cycle detection (requirement 2).

All detection is a pure function of the normalized rule set: rules are
processed sorted by rule id and findings are emitted in canonical order,
so conclusions never depend on declaration order in the source file.
"""

from __future__ import annotations

from typing import Any, Iterable

from . import errors
from .condition import dnf_covers, dnf_overlaps
from .models import NormalizedRule

# Action pairs whose effects cannot both apply to the same traffic.
MUTUALLY_EXCLUSIVE_ACTIONS = (frozenset({"allow", "deny"}),)


def _actions_exclusive(a: str, b: str) -> bool:
    return any(a in pair and b in pair for pair in MUTUALLY_EXCLUSIVE_ACTIONS)


def detect_group(rules: Iterable[NormalizedRule]) -> dict[str, Any]:
    """Detect conflicts and shadows inside one target group."""
    ordered = sorted(rules, key=lambda r: r.rule_id)
    conflicts: list[dict[str, Any]] = []
    shadows: list[dict[str, Any]] = []
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            a, b = ordered[i], ordered[j]
            if a.target != b.target:
                continue
            if not dnf_overlaps(a.dnf, b.dnf):
                continue
            if _actions_exclusive(a.action, b.action):
                conflicts.append({
                    "code": errors.E_CONFLICT,
                    "rules": [a.rule_id, b.rule_id],
                    "target": a.target,
                })
            # Shadow: strictly higher priority rule fully covers the lower
            # priority rule's reachable condition on the same target.
            high, low = (a, b) if a.priority > b.priority else (b, a)
            if high.priority > low.priority and dnf_covers(high.dnf, low.dnf):
                shadows.append({
                    "code": errors.E_SHADOW,
                    "rules": [high.rule_id, low.rule_id],
                    "target": a.target,
                })
    return {"conflicts": conflicts, "shadows": shadows}


def detect_cycles(rules: Iterable[NormalizedRule]) -> list[list[str]]:
    """Detect dependency cycles over ``depends_on`` references.

    Returns a sorted list of cycles; each cycle is the sequence of rule ids
    rotated so the smallest id comes first (canonical form).
    """
    graph: dict[str, list[str]] = {}
    for rule in rules:
        graph.setdefault(rule.rule_id, [])
    for rule in rules:
        for dep in rule.depends_on:
            if dep in graph and dep != rule.rule_id:
                graph[rule.rule_id].append(dep)
    for node in graph:
        graph[node] = sorted(set(graph[node]))

    cycles: set[tuple[str, ...]] = set()
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}

    for start in sorted(graph):
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = [start]
        color[start] = GRAY
        while stack:
            node, child_idx = stack[-1]
            neighbors = graph[node]
            if child_idx < len(neighbors):
                stack[-1] = (node, child_idx + 1)
                nxt = neighbors[child_idx]
                if color[nxt] == GRAY:
                    cycle = path[path.index(nxt):]
                    k = cycle.index(min(cycle))
                    cycles.add(tuple(cycle[k:] + cycle[:k]))
                elif color[nxt] == WHITE:
                    color[nxt] = GRAY
                    path.append(nxt)
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                path.pop()
                stack.pop()
    return [list(c) for c in sorted(cycles)]


def merge_group_findings(groups: Iterable[dict[str, Any]]) -> dict[str, Any]:
    conflicts: list[dict[str, Any]] = []
    shadows: list[dict[str, Any]] = []
    for group in groups:
        conflicts.extend(group["conflicts"])
        shadows.extend(group["shadows"])
    conflicts.sort(key=lambda c: (c["rules"][0], c["rules"][1]))
    shadows.sort(key=lambda s: (s["rules"][0], s["rules"][1]))
    return {"conflicts": conflicts, "shadows": shadows}
