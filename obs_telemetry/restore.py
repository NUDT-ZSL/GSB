"""Span tree reconstruction, deterministic malformed-data handling, and critical paths."""

from __future__ import annotations

import copy
from typing import Any, Iterable

from .util import attribute_bytes, compact_json


def _event_sort_key(event: dict[str, Any], fallback: int) -> tuple[Any, ...]:
    sequence = event.get("sequence", fallback)
    try:
        sequence_value = int(sequence)
    except (TypeError, ValueError):
        sequence_value = fallback
    event_time = event.get("event_time_us", 0)
    try:
        time_value = int(event_time)
    except (TypeError, ValueError):
        time_value = 0
    return (sequence_value, time_value, str(event.get("event_id", "")))


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _diagnostic(
    code: str,
    *,
    trace_id: str,
    span_id: str | None = None,
    related_id: str | None = None,
    severity: str = "warning",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "details": details or {},
        "related_id": related_id,
        "severity": severity,
        "span_id": span_id,
        "trace_id": trace_id,
    }


def _node_public(
    node: dict[str, Any],
    *,
    children: list[dict[str, Any]] | None = None,
    include_children: bool = True,
) -> dict[str, Any]:
    result = {
        "attributes": copy.deepcopy(node["attributes"]),
        "children_span_ids": list(node.get("children_span_ids", [])),
        "depth": node.get("depth"),
        "duration_us": node["duration_us"],
        "effective_parent_span_id": node.get("effective_parent_span_id"),
        "end_time_us": node["end_time_us"],
        "event_sequence": node.get("event_sequence"),
        "missing": bool(node.get("missing")),
        "operation": node["operation"],
        "parent_field_present": node.get("parent_field_present", True),
        "parent_span_id": node.get("parent_span_id"),
        "relationship": node.get("relationship"),
        "service": node["service"],
        "span_id": node["span_id"],
        "start_time_us": node["start_time_us"],
        "status": node["status"],
        "synthetic": bool(node.get("synthetic")),
    }
    if include_children:
        result["children"] = children or []
    return result


def _definition_maps(
    chain_definition: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], dict[str, str]]:
    if not chain_definition:
        return {}, {}, {}
    nodes = chain_definition.get("nodes", [])
    by_id: dict[str, dict[str, Any]] = {}
    children: dict[str, list[str]] = {}
    parent_by_id: dict[str, str | None] = {}
    for node in nodes:
        span_id = str(node.get("span_id"))
        by_id[span_id] = node
        parent = node.get("parent_span_id")
        parent_id = str(parent) if parent not in (None, "") else None
        parent_by_id[span_id] = parent_id
        children.setdefault(span_id, [])
    for span_id, parent_id in parent_by_id.items():
        if parent_id is not None:
            children.setdefault(parent_id, []).append(span_id)
    for child_list in children.values():
        child_list.sort()
    return by_id, children, parent_by_id


def _definition_descendants(
    span_id: str,
    definition_children: dict[str, list[str]],
) -> set[str]:
    result: set[str] = set()
    stack = list(definition_children.get(span_id, []))
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        stack.extend(definition_children.get(current, []))
    return result


def _build_children_and_roots(
    nodes: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[str]], list[str]]:
    children: dict[str, list[str]] = {span_id: [] for span_id in nodes}
    roots: list[str] = []
    for span_id, node in nodes.items():
        parent_id = node.get("effective_parent_span_id")
        if parent_id is None or parent_id not in nodes:
            roots.append(span_id)
        else:
            children.setdefault(parent_id, []).append(span_id)
    for child_ids in children.values():
        child_ids.sort(
            key=lambda child_id: (
                nodes[child_id]["start_time_us"],
                child_id,
            )
        )
    roots.sort(
        key=lambda root_id: (
            nodes[root_id]["start_time_us"],
            root_id,
        )
    )
    for node in nodes.values():
        node["children_span_ids"] = list(children.get(node["span_id"], []))
    return children, roots


def _assign_depths(nodes: dict[str, dict[str, Any]], roots: list[str]) -> None:
    for node in nodes.values():
        node["depth"] = None
    stack: list[tuple[str, int, int]] = []
    for root_id in roots:
        real_depth = 0 if not nodes[root_id].get("synthetic") else -1
        stack.append((root_id, real_depth, 0))
    seen: set[str] = set()
    while stack:
        span_id, real_depth, guard = stack.pop()
        if span_id in seen or guard > len(nodes) + 2:
            continue
        seen.add(span_id)
        node = nodes[span_id]
        if node.get("synthetic"):
            node["depth"] = None
            child_depth = real_depth
        else:
            node["depth"] = max(real_depth, 0)
            child_depth = node["depth"] + 1
        for child_id in reversed(node.get("children_span_ids", [])):
            stack.append((child_id, child_depth, guard + 1))


def _critical_path(
    nodes: dict[str, dict[str, Any]],
    children: dict[str, list[str]],
    roots: list[str],
) -> dict[str, Any] | None:
    candidates: list[tuple[int, tuple[str, ...], list[str]]] = []

    def walk(span_id: str, path: list[str]) -> None:
        child_ids = children.get(span_id, [])
        next_path = path + [span_id]
        if not child_ids:
            real_path = [
                current
                for current in next_path
                if not nodes[current].get("synthetic")
            ]
            if real_path:
                total = sum(nodes[current]["duration_us"] for current in real_path)
                candidates.append((total, tuple(real_path), real_path))
            return
        for child_id in child_ids:
            walk(child_id, next_path)

    for root_id in roots:
        walk(root_id, [])
    if not candidates:
        return None

    total, path_tuple, path = min(
        candidates,
        key=lambda candidate: (-candidate[0], candidate[1]),
    )
    path_spans: list[dict[str, Any]] = []
    cumulative = 0
    service_totals: dict[str, dict[str, int]] = {}
    for span_id in path:
        node = nodes[span_id]
        cumulative += node["duration_us"]
        path_spans.append(
            {
                "cumulative_duration_us": cumulative,
                "duration_us": node["duration_us"],
                "operation": node["operation"],
                "service": node["service"],
                "span_id": span_id,
                "start_time_us": node["start_time_us"],
                "status": node["status"],
            }
        )
        service = node["service"]
        service_totals.setdefault(service, {"duration_us": 0, "span_count": 0})
        service_totals[service]["duration_us"] += node["duration_us"]
        service_totals[service]["span_count"] += 1

    starts = [nodes[span_id]["start_time_us"] for span_id in path]
    ends = [nodes[span_id]["end_time_us"] for span_id in path]
    return {
        "by_service": [
            {
                "duration_us": values["duration_us"],
                "service": service,
                "span_count": values["span_count"],
            }
            for service, values in sorted(service_totals.items())
        ],
        "elapsed_us": max(ends) - min(starts),
        "leaf_span_id": path[-1],
        "root_span_id": path[0],
        "span_count": len(path),
        "span_ids": list(path_tuple),
        "spans": path_spans,
        "total_duration_us": total,
    }


def restore_trace(
    *,
    project_id: str,
    trace_id: str,
    events: Iterable[dict[str, Any]],
    chain_definition: dict[str, Any] | None,
    fallback_job_id: str | None = None,
) -> dict[str, Any]:
    """Reconstruct one trace from all events, independent of arrival order."""
    event_list = list(events)
    diagnostics: list[dict[str, Any]] = []
    ordered = sorted(
        enumerate(event_list),
        key=lambda pair: _event_sort_key(pair[1], pair[0]),
    )

    chosen: dict[str, dict[str, Any]] = {}
    duplicate_events: list[dict[str, Any]] = []
    for fallback, (_, event) in enumerate(ordered):
        raw_span_id = event.get("span_id")
        if raw_span_id in (None, ""):
            span_id = (
                "__missing-span__:"
                + str(event.get("event_id", f"sequence-{event.get('sequence', fallback)}"))
            )
            diagnostics.append(
                _diagnostic(
                    "SPAN_ID_MISSING",
                    trace_id=trace_id,
                    span_id=span_id,
                    severity="error",
                    details={"event_id": event.get("event_id")},
                )
            )
        else:
            span_id = str(raw_span_id)

        if span_id in chosen:
            previous = chosen[span_id]
            duplicate_events.append(
                {
                    "chosen_event_id": previous.get("event_id"),
                    "dropped_event_id": event.get("event_id"),
                    "event_sequence": event.get("sequence"),
                    "span_id": span_id,
                }
            )
            diagnostics.append(
                _diagnostic(
                    "DUPLICATE_SPAN_IGNORED",
                    trace_id=trace_id,
                    span_id=span_id,
                    severity="warning",
                    details={
                        "chosen_event_id": previous.get("event_id"),
                        "dropped_event_id": event.get("event_id"),
                    },
                )
            )
            continue
        chosen[span_id] = event

    definition_by_id, definition_children, _ = _definition_maps(chain_definition)
    definition_job_id = (
        str(chain_definition.get("job_id")) if chain_definition and chain_definition.get("job_id") else None
    )
    event_job_ids = sorted(
        {
            str(event.get("job_id"))
            for event in chosen.values()
            if event.get("job_id") not in (None, "")
        }
    )
    if definition_job_id and event_job_ids and event_job_ids != [definition_job_id]:
        diagnostics.append(
            _diagnostic(
                "JOB_ID_CONFLICT",
                trace_id=trace_id,
                severity="error",
                details={"definition_job_id": definition_job_id, "event_job_ids": event_job_ids},
            )
        )
    job_id = definition_job_id or (event_job_ids[0] if event_job_ids else fallback_job_id)
    if job_id is None:
        job_id = "__unassigned__"

    nodes: dict[str, dict[str, Any]] = {}
    real_span_ids: list[str] = []
    for span_id in sorted(
        chosen,
        key=lambda current: _event_sort_key(chosen[current], 0),
    ):
        event = chosen[span_id]
        start_present = "start_time_us" in event
        start_time_us = _int_value(event.get("start_time_us"), -1)
        duration_us = _int_value(event.get("duration_us"), -1)
        if duration_us < 0 and "duration_ms" in event:
            duration_us = _int_value(event.get("duration_ms"), 0) * 1_000
        if duration_us < 0:
            duration_us = 0
            diagnostics.append(
                _diagnostic(
                    "DURATION_MISSING",
                    trace_id=trace_id,
                    span_id=span_id,
                    details={"event_id": event.get("event_id")},
                )
            )
        if start_time_us < 0:
            event_time = _int_value(event.get("event_time_us"), 0)
            start_time_us = event_time - duration_us
            if not start_present:
                diagnostics.append(
                    _diagnostic(
                        "START_TIME_INFERRED",
                        trace_id=trace_id,
                        span_id=span_id,
                        details={"event_id": event.get("event_id"), "start_time_us": start_time_us},
                    )
                )
        attributes = event.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
            diagnostics.append(
                _diagnostic(
                    "ATTRIBUTES_INVALID",
                    trace_id=trace_id,
                    span_id=span_id,
                    details={"event_id": event.get("event_id")},
                )
            )
        parent_present = "parent_span_id" in event
        parent_value = event.get("parent_span_id")
        parent_id = str(parent_value) if parent_value not in (None, "") else None
        status = event.get("status", "UNKNOWN")
        node = {
            "attributes": copy.deepcopy(attributes),
            "children_span_ids": [],
            "depth": None,
            "duration_us": duration_us,
            "effective_parent_span_id": parent_id,
            "end_time_us": start_time_us + duration_us,
            "event_id": event.get("event_id"),
            "event_sequence": event.get("sequence"),
            "missing": False,
            "operation": str(event.get("operation", "unknown")),
            "parent_field_present": parent_present,
            "parent_span_id": parent_id,
            "relationship": "ROOT" if parent_id is None else "CHILD",
            "service": str(event.get("service", "unknown")),
            "span_id": span_id,
            "start_time_us": start_time_us,
            "status": str(status),
            "synthetic": False,
        }
        nodes[span_id] = node
        real_span_ids.append(span_id)

        if not parent_present:
            node["effective_parent_span_id"] = None
            node["relationship"] = "ROOT_PARENT_FIELD_MISSING"
            diagnostics.append(
                _diagnostic(
                    "PARENT_FIELD_MISSING",
                    trace_id=trace_id,
                    span_id=span_id,
                    severity="error",
                    details={"event_id": event.get("event_id")},
                )
            )
        elif parent_id == span_id:
            node["effective_parent_span_id"] = None
            node["relationship"] = "ROOT_SELF_PARENT"
            diagnostics.append(
                _diagnostic(
                    "SELF_PARENT_BROKEN",
                    trace_id=trace_id,
                    span_id=span_id,
                    severity="error",
                    details={"parent_span_id": parent_id},
                )
            )
        elif parent_id is not None and parent_id not in chosen:
            synthetic_id = f"__missing-parent__:{parent_id}"
            expected = definition_by_id.get(parent_id, {})
            if synthetic_id not in nodes:
                reference_starts = [
                    candidate["start_time_us"]
                    for candidate in nodes.values()
                    if candidate.get("effective_parent_span_id") == synthetic_id
                ]
                synthetic_start = min(reference_starts) if reference_starts else node["start_time_us"]
                nodes[synthetic_id] = {
                    "attributes": {},
                    "children_span_ids": [],
                    "depth": None,
                    "duration_us": 0,
                    "effective_parent_span_id": None,
                    "end_time_us": synthetic_start,
                    "event_id": None,
                    "event_sequence": None,
                    "missing": True,
                    "operation": str(expected.get("operation", "missing-parent")),
                    "parent_field_present": True,
                    "parent_span_id": None,
                    "relationship": "SYNTHETIC_MISSING_PARENT",
                    "service": str(expected.get("service", "missing")),
                    "span_id": synthetic_id,
                    "start_time_us": synthetic_start,
                    "status": "MISSING",
                    "synthetic": True,
                }
            node["effective_parent_span_id"] = synthetic_id
            node["relationship"] = "ORPHAN_ATTACHED_TO_MISSING_PARENT"
            diagnostics.append(
                _diagnostic(
                    "PARENT_NOT_FOUND",
                    trace_id=trace_id,
                    span_id=span_id,
                    related_id=parent_id,
                    severity="error",
                    details={
                        "expected_parent_known_in_definition": bool(expected),
                        "synthetic_parent_span_id": synthetic_id,
                    },
                )
            )

    # Break parent cycles deterministically.  The lexicographically first span in
    # each cycle becomes a root, and synthetic cycle roots are not fabricated.
    ordered_ids = sorted(nodes)
    state: dict[str, int] = {}
    for start_id in ordered_ids:
        if state.get(start_id, 0) != 0:
            continue
        path: list[str] = []
        position: dict[str, int] = {}
        current: str | None = start_id
        while current is not None and state.get(current, 0) == 0:
            if current in position:
                cycle = path[position[current]:]
                break_id = min(cycle)
                nodes[break_id]["effective_parent_span_id"] = None
                nodes[break_id]["relationship"] = "ROOT_CYCLE_BROKEN"
                for cycle_id in cycle:
                    diagnostics.append(
                        _diagnostic(
                            "PARENT_CYCLE_BROKEN",
                            trace_id=trace_id,
                            span_id=cycle_id,
                            related_id=break_id,
                            severity="error",
                            details={"cycle_span_ids": sorted(cycle), "broken_at_span_id": break_id},
                        )
                    )
                break
            position[current] = len(path)
            path.append(current)
            parent_id = nodes[current].get("effective_parent_span_id")
            current = parent_id if parent_id in nodes else None
        for path_id in path:
            state[path_id] = 2

    children, roots = _build_children_and_roots(nodes)
    _assign_depths(nodes, roots)

    real_roots = [root_id for root_id in roots if not nodes[root_id].get("synthetic")]
    if len(real_roots) > 1:
        diagnostics.append(
            _diagnostic(
                "MULTIPLE_TRACE_ROOTS",
                trace_id=trace_id,
                severity="warning",
                details={"root_span_ids": real_roots},
            )
        )

    ascending: set[str] = set()

    def build_nested(span_id: str) -> dict[str, Any]:
        if span_id in ascending:
            return _node_public(nodes[span_id], children=[], include_children=True)
        ascending.add(span_id)
        nested = _node_public(
            nodes[span_id],
            children=[
                build_nested(child_id)
                for child_id in children.get(span_id, [])
            ],
            include_children=True,
        )
        ascending.remove(span_id)
        return nested

    tree = [build_nested(root_id) for root_id in roots]

    observed_ids = set(real_span_ids)
    expected_missing_nodes: list[dict[str, Any]] = []
    for expected_id, expected in definition_by_id.items():
        if expected_id in observed_ids:
            continue
        expected_missing_nodes.append(
            {
                "duration_us": _int_value(expected.get("duration_us"), 0),
                "operation": str(expected.get("operation", "unknown")),
                "parent_span_id": expected.get("parent_span_id"),
                "service": str(expected.get("service", "unknown")),
                "span_id": expected_id,
                "start_time_us": _int_value(expected.get("start_time_us"), 0),
                "status": str(expected.get("status", "UNKNOWN")),
            }
        )
    expected_missing_nodes.sort(
        key=lambda item: (item["start_time_us"], item["span_id"])
    )

    diag_map: dict[str, dict[str, Any]] = {}
    for diagnostic in diagnostics:
        key = compact_json(diagnostic)
        diag_map[key] = diagnostic
    diagnostics = sorted(
        diag_map.values(),
        key=lambda item: (
            item["code"],
            item.get("span_id") or "",
            item.get("related_id") or "",
            compact_json(item.get("details", {})),
        ),
    )

    flat_spans: list[dict[str, Any]] = []
    for span_id in sorted(
        real_span_ids,
        key=lambda current: (
            nodes[current]["start_time_us"],
            current,
        ),
    ):
        node = nodes[span_id]
        public = _node_public(node, include_children=False)
        public["attribute_bytes"] = attribute_bytes(node["attributes"])
        flat_spans.append(public)

    critical_path = _critical_path(nodes, children, roots)
    trace_start_us = (
        min(node["start_time_us"] for node in nodes.values() if not node.get("synthetic"))
        if real_span_ids
        else 0
    )
    trace_end_us = (
        max(node["end_time_us"] for node in nodes.values() if not node.get("synthetic"))
        if real_span_ids
        else 0
    )
    ingress = copy.deepcopy(chain_definition.get("ingress", {})) if chain_definition else {}
    if not ingress:
        root_candidates = [nodes[root_id] for root_id in roots if not nodes[root_id].get("synthetic")]
        if root_candidates:
            root_attributes = root_candidates[0].get("attributes", {})
            if isinstance(root_attributes, dict):
                if "sampling.decision" in root_attributes:
                    ingress["sampling_marker"] = root_attributes["sampling.decision"]
                if "sampling.marker_source" in root_attributes:
                    ingress["marker_source"] = root_attributes["sampling.marker_source"]

    return {
        "critical_path": critical_path,
        "diagnostics": diagnostics,
        "duplicate_events": duplicate_events,
        "event_count": len(event_list),
        "expected_missing_nodes": expected_missing_nodes,
        "flat_spans": flat_spans,
        "ingress": ingress,
        "job_id": job_id,
        "job_kind": (
            str(chain_definition.get("job_kind"))
            if chain_definition and chain_definition.get("job_kind")
            else None
        ),
        "observation_id": (
            str(chain_definition.get("observation_id"))
            if chain_definition and chain_definition.get("observation_id")
            else None
        ),
        "observed_span_ids": sorted(observed_ids),
        "project_id": project_id,
        "root_span_ids": roots,
        "span_count": len(real_span_ids),
        "trace_end_us": trace_end_us,
        "trace_id": trace_id,
        "trace_start_us": trace_start_us,
        "tree": tree,
    }


def analyze_failure(
    restoration: dict[str, Any],
    chain_definition: dict[str, Any] | None,
) -> dict[str, Any]:
    """Locate the first temporal error and expected downstream nodes that are absent."""
    error_spans = [
        span
        for span in restoration["flat_spans"]
        if str(span["status"]).upper() == "ERROR"
    ]
    error_spans.sort(key=lambda span: (span["start_time_us"], span["span_id"]))
    definition_by_id, definition_children, _ = _definition_maps(chain_definition)
    observed = set(restoration["observed_span_ids"])
    all_errors = [
        {
            "duration_us": span["duration_us"],
            "operation": span["operation"],
            "service": span["service"],
            "span_id": span["span_id"],
            "start_time_us": span["start_time_us"],
            "status": span["status"],
        }
        for span in error_spans
    ]
    if not error_spans:
        return {
            "all_error_span_ids": [],
            "all_errors": [],
            "critical_path": restoration.get("critical_path"),
            "downstream_missing_nodes": [],
            "downstream_present_span_ids": [],
            "failed": False,
            "root_cause": None,
        }

    first = error_spans[0]
    error_id = first["span_id"]
    descendant_ids = _definition_descendants(error_id, definition_children)
    missing_ids = sorted(descendant_ids - observed)
    present_ids = sorted(descendant_ids & observed)
    missing_nodes: list[dict[str, Any]] = []
    for missing_id in missing_ids:
        expected = definition_by_id.get(missing_id)
        if expected is None:
            continue
        expected_parent = expected.get("parent_span_id")
        missing_nodes.append(
            {
                "duration_us": _int_value(expected.get("duration_us"), 0),
                "operation": str(expected.get("operation", "unknown")),
                "parent_span_id": expected_parent,
                "service": str(expected.get("service", "unknown")),
                "span_id": missing_id,
                "start_time_us": _int_value(expected.get("start_time_us"), 0),
            }
        )
    missing_nodes.sort(key=lambda item: (item["start_time_us"], item["span_id"]))
    observed_children = set(first.get("children_span_ids", []))
    return {
        "all_error_span_ids": [span["span_id"] for span in error_spans],
        "all_errors": all_errors,
        "critical_path": restoration.get("critical_path"),
        "downstream_missing_nodes": missing_nodes,
        "downstream_present_span_ids": present_ids,
        "failed": True,
        "root_cause": {
            "children_span_ids": sorted(observed_children),
            "duration_us": first["duration_us"],
            "error_type": first.get("attributes", {}).get("error.type"),
            "is_observed_leaf": not bool(observed_children),
            "operation": first["operation"],
            "service": first["service"],
            "span_id": error_id,
            "start_time_us": first["start_time_us"],
            "status": first["status"],
        },
    }
