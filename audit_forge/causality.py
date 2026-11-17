from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .models import Event


def _event_sort_key(event: Event) -> tuple[int, str]:
    return (event.seq, event.event_id)


def group_by_trace(events: Iterable[Event]) -> dict[str, list[Event]]:
    traces: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        traces[event.trace_id].append(event)
    for trace_events in traces.values():
        trace_events.sort(key=_event_sort_key)
    return dict(sorted(traces.items()))


def analyze_causal_relationships(
    events: Iterable[Event],
) -> tuple[list[dict[str, Any]], set[str]]:
    event_list = sorted(events, key=lambda event: event.event_id)
    by_id = {event.event_id: event for event in event_list}
    anomalies: list[dict[str, Any]] = []
    local_parent: dict[str, str] = {}
    known_cycles: set[tuple[str, ...]] = set()

    for event in event_list:
        parent_id = event.parent_event_id
        if parent_id is None:
            continue
        if parent_id == event.event_id:
            anomalies.append(
                {
                    "code": "E_CYCLE",
                    "severity": "error",
                    "kind": "self_loop",
                    "message": "event references itself as parent",
                    "event_id": event.event_id,
                    "event_ids": [event.event_id],
                    "trace_id": event.trace_id,
                    "parent_event_id": parent_id,
                }
            )
            continue

        parent = by_id.get(parent_id)
        if parent is None:
            anomalies.append(
                {
                    "code": "E_ORPHAN",
                    "severity": "error",
                    "message": "parent event is missing",
                    "event_id": event.event_id,
                    "trace_id": event.trace_id,
                    "parent_event_id": parent_id,
                }
            )
            continue

        if parent.trace_id != event.trace_id:
            anomalies.append(
                {
                    "code": "E_CROSS_TRACE",
                    "severity": "error",
                    "message": "parent event belongs to another trace",
                    "event_id": event.event_id,
                    "trace_id": event.trace_id,
                    "parent_event_id": parent_id,
                    "parent_trace_id": parent.trace_id,
                }
            )
            continue

        local_parent[event.event_id] = parent_id

    completed: set[str] = set()
    cycle_nodes: set[str] = set()
    for start in sorted(local_parent):
        if start in completed:
            continue
        chain: list[str] = []
        position: dict[str, int] = {}
        current = start
        while current not in position and current not in completed:
            position[current] = len(chain)
            chain.append(current)
            parent_id = local_parent.get(current)
            if parent_id is None:
                current = ""
                break
            current = parent_id

        if current and current in position:
            cycle = tuple(sorted(chain[position[current] :]))
            if cycle and cycle not in known_cycles:
                known_cycles.add(cycle)
                trace_ids = sorted({by_id[event_id].trace_id for event_id in cycle})
                cycle_nodes.update(cycle)
                anomalies.append(
                    {
                        "code": "E_CYCLE",
                        "severity": "error",
                        "kind": "cycle",
                        "message": "parent relationships contain a cycle",
                        "event_id": cycle[0],
                        "event_ids": list(cycle),
                        "trace_id": trace_ids[0],
                        "trace_ids": trace_ids,
                    }
                )
        completed.update(chain)

    anomalies.sort(
        key=lambda item: (
            item["code"],
            item.get("trace_id", ""),
            item.get("event_id", ""),
        )
    )
    return anomalies, cycle_nodes


def _ancestry_for_event(
    event: Event,
    local_parent: dict[str, str],
    cycle_nodes: set[str],
) -> list[str] | None:
    if event.event_id in cycle_nodes:
        return None
    path = [event.event_id]
    current = event.event_id
    seen = {current}
    while True:
        parent_id = local_parent.get(current)
        if parent_id is None:
            path.reverse()
            return path
        if parent_id in seen or parent_id in cycle_nodes:
            return None
        path.append(parent_id)
        seen.add(parent_id)
        current = parent_id


def analyze_trace(
    trace_id: str,
    events: Iterable[Event],
    causal_anomalies: list[dict[str, Any]] | None = None,
    cycle_nodes: set[str] | None = None,
) -> dict[str, Any]:
    all_events = list(events)
    trace_events = sorted(
        (event for event in all_events if event.trace_id == trace_id),
        key=_event_sort_key,
    )
    by_id = {event.event_id: event for event in all_events}
    anomalies, detected_cycles = analyze_causal_relationships(all_events)
    if causal_anomalies is not None:
        anomalies = causal_anomalies
    if cycle_nodes is not None:
        detected_cycles = cycle_nodes

    local_parent: dict[str, str] = {}
    children: dict[str, list[str]] = defaultdict(list)
    for event in trace_events:
        parent_id = event.parent_event_id
        if (
            parent_id is not None
            and parent_id != event.event_id
            and parent_id in by_id
            and by_id[parent_id].trace_id == trace_id
        ):
            local_parent[event.event_id] = parent_id
            children[parent_id].append(event.event_id)

    for child_ids in children.values():
        child_ids.sort()

    event_rows: list[dict[str, Any]] = []
    roots: list[str] = []
    for event in trace_events:
        ancestry = _ancestry_for_event(event, local_parent, detected_cycles)
        if ancestry is not None and len(ancestry) == 1:
            roots.append(event.event_id)
        row = event.as_dict()
        row["depth"] = None if ancestry is None else len(ancestry) - 1
        row["ancestry"] = ancestry
        event_rows.append(row)

    chain_event_ids: set[str] = set()
    chains: list[dict[str, Any]] = []
    for root_id in roots:
        stack = [root_id]
        members: list[str] = []
        while stack:
            current = stack.pop()
            if current in chain_event_ids:
                continue
            chain_event_ids.add(current)
            members.append(current)
            stack.extend(reversed(children.get(current, [])))
        chains.append({"root_event_id": root_id, "event_ids": members})

    trace_anomalies = [
        anomaly
        for anomaly in anomalies
        if anomaly.get("trace_id") == trace_id
        or trace_id in anomaly.get("trace_ids", [])
    ]
    edges = [
        {"parent_event_id": parent_id, "event_id": child_id}
        for child_id, parent_id in sorted(local_parent.items())
    ]
    return {
        "trace_id": trace_id,
        "event_count": len(trace_events),
        "roots": roots,
        "unattached_event_ids": [
            event.event_id
            for event in trace_events
            if event.event_id not in chain_event_ids
        ],
        "edges": edges,
        "chains": chains,
        "events": event_rows,
        "anomalies": trace_anomalies,
    }


def analyze_all_traces(
    events: Iterable[Event],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    event_list = list(events)
    anomalies, cycle_nodes = analyze_causal_relationships(event_list)
    traces = group_by_trace(event_list)
    analyses = [
        analyze_trace(
            trace_id,
            trace_events,
            causal_anomalies=anomalies,
            cycle_nodes=cycle_nodes,
        )
        for trace_id, trace_events in traces.items()
    ]
    return analyses, anomalies, cycle_nodes
