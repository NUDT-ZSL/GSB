"""Ticket dependency graph and transitive revocation closure."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable

from .normalize import NormalizedDataset


def build_ticket_graph(
    dataset: NormalizedDataset,
    renewal_events: Iterable[dict[str, Any]] = (),
) -> dict[str, set[str]]:
    """Build parent->successor edges from declarations and accepted renewals."""

    graph: dict[str, set[str]] = {ticket_id: set() for ticket_id in dataset.tickets}
    for ticket in dataset.tickets.values():
        parent = ticket.parent_ticket_id
        if parent in dataset.tickets:
            graph[parent].add(ticket.ticket_id)
    for event in renewal_events:
        if event.get("decision") != "accept":
            continue
        source = event.get("ticket_id")
        successor = event.get("successor_ticket_id")
        if (
            isinstance(source, str)
            and isinstance(successor, str)
            and source in dataset.tickets
            and successor in dataset.tickets
        ):
            graph[source].add(successor)
    return graph


def _reachable(
    roots: Iterable[str],
    graph: dict[str, set[str]],
) -> set[str]:
    reached: set[str] = set()
    queue = deque(roots)
    while queue:
        node = queue.popleft()
        if node in reached:
            continue
        reached.add(node)
        queue.extend(sorted(graph.get(node, set())))
    return reached


def _cycles_in_subgraph(
    nodes: set[str],
    graph: dict[str, set[str]],
) -> list[list[str]]:
    """Return deterministic elementary cycle representatives."""

    state: dict[str, int] = {}
    stack: list[str] = []
    found: list[list[str]] = []
    seen_cycles: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for successor in sorted(graph.get(node, set())):
            if successor not in nodes:
                continue
            if state.get(successor, 0) == 0:
                visit(successor)
            elif state.get(successor) == 1:
                start = stack.index(successor)
                cycle = stack[start:]
                canonical = tuple(sorted(cycle))
                if canonical not in seen_cycles:
                    seen_cycles.add(canonical)
                    found.append(sorted(cycle))
        stack.pop()
        state[node] = 2

    for node in sorted(nodes):
        if state.get(node, 0) == 0:
            visit(node)
    return sorted(found)


def compute_revocation_closure(
    dataset: NormalizedDataset,
    *,
    request_id: str,
    target_type: str,
    target_id: str,
    observed_at_ms: int,
    renewal_events: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    graph = build_ticket_graph(dataset, renewal_events)
    if target_type == "ticket":
        if target_id not in dataset.tickets:
            return {
                "request_id": request_id,
                "target_type": target_type,
                "target_id": target_id,
                "observed_at_ms": observed_at_ms,
                "decision": "reject",
                "code": "E_UNKNOWN_TICKET",
                "closure": [],
                "cycle_ticket_ids": [],
            }
        roots = {target_id}
        base_entities = [("ticket", target_id)]
    elif target_type == "device":
        if target_id not in dataset.fingerprints:
            return {
                "request_id": request_id,
                "target_type": target_type,
                "target_id": target_id,
                "observed_at_ms": observed_at_ms,
                "decision": "reject",
                "code": "E_UNKNOWN_DEVICE",
                "closure": [],
                "cycle_ticket_ids": [],
            }
        roots = {
            ticket.ticket_id
            for ticket in dataset.tickets.values()
            if ticket.device_id == target_id
        }
        base_entities = [("device", target_id)]
    else:
        return {
            "request_id": request_id,
            "target_type": target_type,
            "target_id": target_id,
            "observed_at_ms": observed_at_ms,
            "decision": "reject",
            "code": "E_INVALID_RECORD",
            "closure": [],
            "cycle_ticket_ids": [],
        }

    reachable = _reachable(roots, graph)
    cycles = _cycles_in_subgraph(reachable, graph)
    if cycles:
        cycle_ticket_ids = sorted({ticket for cycle in cycles for ticket in cycle})
        return {
            "request_id": request_id,
            "target_type": target_type,
            "target_id": target_id,
            "observed_at_ms": observed_at_ms,
            "decision": "reject",
            "code": "E_TICKET_CYCLE",
            "closure": [],
            "cycle_ticket_ids": cycle_ticket_ids,
            "cycles": cycles,
        }

    entities = set(base_entities)
    entities.update(("ticket", ticket_id) for ticket_id in reachable)
    ordered = [
        {"type": entity_type, "id": entity_id}
        for entity_type, entity_id in sorted(entities)
    ]
    return {
        "request_id": request_id,
        "target_type": target_type,
        "target_id": target_id,
        "observed_at_ms": observed_at_ms,
        "decision": "accept",
        "code": None,
        "closure": ordered,
        "ticket_ids": sorted(reachable),
        "cycle_ticket_ids": [],
    }


def revoke_all(
    dataset: NormalizedDataset,
    *,
    renewal_events: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    events = list(renewal_events)
    return [
        compute_revocation_closure(
            dataset,
            request_id=request.request_id,
            target_type=request.target_type,
            target_id=request.target_id,
            observed_at_ms=request.observed_at_ms,
            renewal_events=events,
        )
        for request in sorted(dataset.revocations, key=lambda item: item.request_id)
    ]

