#!/usr/bin/env python3
"""Independent reference judge for the ticket-rule rehearsal project.

This script intentionally imports only the Python standard library and does not
import ticket_gate. It recomputes planning, propagation, degradation, and
verification results from the JSON project files.
"""

from __future__ import annotations

import argparse
import heapq
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def source(relative_file: str, *parts: str) -> str:
    return relative_file + "#" + "".join("/" + pointer(str(part)) for part in parts)


def rule_value(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticket_whitelist": tuple(sorted(set(raw["ticket_whitelist"]))),
        "start": raw["entry_window"]["start"],
        "end": raw["entry_window"]["end"],
        "start_at": parse_time(raw["entry_window"]["start"]),
        "end_at": parse_time(raw["entry_window"]["end"]),
        "reentry_policy": raw["reentry_policy"],
        "requires_adult": raw["child_policy"]["requires_adult"],
    }


def comparable(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticket_whitelist": list(rule["ticket_whitelist"]),
        "entry_window.start": rule["start"],
        "entry_window.end": rule["end"],
        "reentry_policy": rule["reentry_policy"],
        "child_policy.requires_adult": rule["requires_adult"],
    }


class ReferenceProject:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest = load_json(root / "manifest.json")
        self.seed = int(self.manifest["seed"])
        self.project_id = str(self.manifest["project_id"])
        files = self.manifest["files"]
        self.events_file = str(files["events"])
        events_doc = load_json(root / self.events_file)
        gates_doc = load_json(root / str(files["gates"]))
        topology_doc = load_json(root / str(files["topology"]))
        changes_doc = load_json(root / str(files["changes"]))
        faults_doc = load_json(root / str(files["faults"]))
        requests_doc = load_json(root / str(files["verification_requests"]))

        self.events: dict[str, dict[str, Any]] = {}
        self.rules: dict[tuple[str, str], dict[str, Any]] = {}
        self.sources: dict[tuple[str, str], dict[str, str]] = {}
        for event_id, raw_event in events_doc["events"].items():
            self.events[event_id] = {
                "current_version": raw_event["current_version"],
                "versions": set(raw_event["versions"]),
            }
            for version, raw_rule in raw_event["versions"].items():
                base = ("events", event_id, "versions", version, "rules")
                self.rules[(event_id, version)] = rule_value(raw_rule)
                self.sources[(event_id, version)] = {
                    "ticket_whitelist": source(
                        self.events_file, *base, "ticket_whitelist"
                    ),
                    "entry_window.start": source(
                        self.events_file, *base, "entry_window", "start"
                    ),
                    "entry_window.end": source(
                        self.events_file, *base, "entry_window", "end"
                    ),
                    "reentry_policy": source(
                        self.events_file, *base, "reentry_policy"
                    ),
                    "child_policy.requires_adult": source(
                        self.events_file, *base, "child_policy", "requires_adult"
                    ),
                }

        self.gates: dict[str, dict[str, Any]] = {}
        for raw_gate in gates_doc["gates"]:
            gate_id = str(raw_gate["gate_id"])
            self.gates[gate_id] = {
                "gate_id": gate_id,
                "entrance_id": str(raw_gate["entrance_id"]),
                "seat_zone_ids": tuple(sorted(set(raw_gate["seat_zone_ids"]))),
                "subscriptions": tuple(sorted(set(raw_gate["subscriptions"]))),
            }

        self.entrance_deps = {
            str(item["entrance_id"]): tuple(
                sorted(set(item.get("depends_on_entrance_ids", [])))
            )
            for item in topology_doc.get("entrances", [])
        }
        self.zone_deps = {
            str(item["seat_zone_id"]): tuple(
                sorted(set(item.get("depends_on_seat_zone_ids", [])))
            )
            for item in topology_doc.get("seat_zones", [])
        }
        self.changes = {
            str(item["event_id"]): (
                str(item["from_version"]),
                str(item["to_version"]),
            )
            for item in changes_doc["changes"]
        }
        self.faults: dict[str, dict[int, dict[str, Any]]] = {}
        for gate_id, raw_fault in faults_doc.get("faults", {}).items():
            self.faults[gate_id] = {
                int(item["attempt"]): {
                    "error_code": str(item["error_code"]),
                    "retryable": bool(item["retryable"]),
                }
                for item in raw_fault.get("attempts", [])
            }
        self.requests = sorted(requests_doc["requests"], key=lambda item: item["request_id"])

    def target_version(self, event_id: str) -> str:
        change = self.changes.get(event_id)
        return change[1] if change else self.events[event_id]["current_version"]

    def baseline_version(self, event_id: str) -> str:
        change = self.changes.get(event_id)
        return change[0] if change else self.events[event_id]["current_version"]

    def target_versions(self, gate: dict[str, Any]) -> dict[str, str]:
        return {
            event_id: self.target_version(event_id)
            for event_id in gate["subscriptions"]
        }

    def baseline_versions(self, gate: dict[str, Any]) -> dict[str, str]:
        return {
            event_id: self.baseline_version(event_id)
            for event_id in gate["subscriptions"]
        }

    def affected(self) -> list[str]:
        changed = set(self.changes)
        return sorted(
            gate_id
            for gate_id, gate in self.gates.items()
            if changed.intersection(gate["subscriptions"])
        )

    def dependencies(self) -> dict[tuple[str, str], set[str]]:
        by_entrance: dict[str, list[str]] = {}
        by_zone: dict[str, list[str]] = {}
        for gate_id, gate in self.gates.items():
            by_entrance.setdefault(gate["entrance_id"], []).append(gate_id)
            for zone_id in gate["seat_zone_ids"]:
                by_zone.setdefault(zone_id, []).append(gate_id)
        result: dict[tuple[str, str], set[str]] = {}
        for gate_id in sorted(self.gates):
            gate = self.gates[gate_id]
            for upstream_entrance in self.entrance_deps.get(gate["entrance_id"], ()):
                for upstream_gate in by_entrance.get(upstream_entrance, ()):
                    if upstream_gate != gate_id:
                        result.setdefault((upstream_gate, gate_id), set()).add(
                            f"entrance:{upstream_entrance}"
                        )
            for zone_id in gate["seat_zone_ids"]:
                for upstream_zone in self.zone_deps.get(zone_id, ()):
                    for upstream_gate in by_zone.get(upstream_zone, ()):
                        if upstream_gate != gate_id:
                            result.setdefault((upstream_gate, gate_id), set()).add(
                                f"seat_zone:{upstream_zone}"
                            )
        return result

    def conflicts(self, affected: list[str]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for gate_id in affected:
            subscriptions = self.gates[gate_id]["subscriptions"]
            for left_index in range(len(subscriptions)):
                for right_index in range(left_index + 1, len(subscriptions)):
                    left_event = subscriptions[left_index]
                    right_event = subscriptions[right_index]
                    left_version = self.target_version(left_event)
                    right_version = self.target_version(right_event)
                    left_rule = self.rules[(left_event, left_version)]
                    right_rule = self.rules[(right_event, right_version)]
                    if not (
                        max(left_rule["start_at"], right_rule["start_at"])
                        < min(left_rule["end_at"], right_rule["end_at"])
                    ):
                        continue
                    shared = sorted(
                        set(left_rule["ticket_whitelist"]).intersection(
                            right_rule["ticket_whitelist"]
                        )
                    )
                    if not shared:
                        continue
                    left_policy = {
                        "reentry_policy": left_rule["reentry_policy"],
                        "child_policy.requires_adult": left_rule["requires_adult"],
                    }
                    right_policy = {
                        "reentry_policy": right_rule["reentry_policy"],
                        "child_policy.requires_adult": right_rule["requires_adult"],
                    }
                    fields = tuple(
                        field_name
                        for field_name in left_policy
                        if left_policy[field_name] != right_policy[field_name]
                    )
                    if not fields:
                        continue
                    output.append(
                        {
                            "gate_id": gate_id,
                            "event_ids": [left_event, right_event],
                            "versions": {
                                left_event: left_version,
                                right_event: right_version,
                            },
                            "overlapping_ticket_types": shared,
                            "fields": list(fields),
                            "sources": {
                                field_name: {
                                    left_event: self.sources[
                                        (left_event, left_version)
                                    ][field_name],
                                    right_event: self.sources[
                                        (right_event, right_version)
                                    ][field_name],
                                }
                                for field_name in sorted(fields)
                            },
                        }
                    )
        return output


def tarjan(
    nodes: tuple[str, ...], adjacency: dict[str, set[str]]
) -> list[tuple[str, ...]]:
    next_index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal next_index
        indices[node] = next_index
        lowlinks[node] = next_index
        next_index += 1
        stack.append(node)
        on_stack.add(node)
        for neighbor in sorted(adjacency.get(node, ())):
            if neighbor not in indices:
                visit(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                item = stack.pop()
                on_stack.remove(item)
                component.append(item)
                if item == node:
                    break
            components.append(tuple(sorted(component)))

    for node in nodes:
        if node not in indices:
            visit(node)
    return components


def make_batches(
    affected: list[str], dependency_map: dict[tuple[str, str], set[str]]
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    node_set = set(affected)
    edges = [
        (upstream, downstream, reasons)
        for (upstream, downstream), reasons in dependency_map.items()
        if upstream in node_set and downstream in node_set
    ]
    adjacency: dict[str, set[str]] = {gate_id: set() for gate_id in affected}
    for upstream, downstream, _ in edges:
        adjacency[upstream].add(downstream)
    components = tarjan(tuple(affected), adjacency)
    component_of = {
        gate_id: component for component in components for gate_id in component
    }
    cyclic = {
        component
        for component in components
        if len(component) > 1
        or any(neighbor == component[0] for neighbor in adjacency.get(component[0], ()))
    }
    component_edges: dict[tuple[str, ...], set[tuple[str, ...]]] = {
        component: set() for component in components
    }
    indegree = {component: 0 for component in components}
    for upstream, downstream, _ in edges:
        left = component_of[upstream]
        right = component_of[downstream]
        if left != right and right not in component_edges[left]:
            component_edges[left].add(right)
            indegree[right] += 1
    ready: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for component, degree in indegree.items():
        if degree == 0:
            heapq.heappush(ready, (component, component))
    batches: list[dict[str, Any]] = []
    batch_no = 0
    while ready:
        level: list[tuple[str, ...]] = []
        while ready:
            level.append(heapq.heappop(ready)[1])
        level.sort()
        gates: list[str] = []
        cycle_gates: list[str] = []
        for component in level:
            gates.extend(component)
            if component in cyclic:
                cycle_gates.extend(component)
        batches.append(
            {
                "batch_no": batch_no,
                "gate_ids": sorted(gates),
                "cycle_gate_ids": sorted(cycle_gates),
            }
        )
        next_level: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        for component in level:
            for downstream in sorted(component_edges[component]):
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    next_level.append((downstream, downstream))
        for item in next_level:
            heapq.heappush(ready, item)
        batch_no += 1
    cycles = sorted(component for component in components if component in cyclic)
    return batches, [list(cycle) for cycle in cycles]


def simulate(project: ReferenceProject, batches: list[dict[str, Any]]) -> dict[str, Any]:
    affected = project.affected()
    states = {gate_id: "pending" for gate_id in affected}
    active = {
        gate_id: project.baseline_versions(project.gates[gate_id])
        for gate_id in affected
    }
    targets = {
        gate_id: project.target_versions(project.gates[gate_id])
        for gate_id in affected
    }
    attempts = {gate_id: 0 for gate_id in affected}
    activated: dict[str, int | None] = {gate_id: None for gate_id in affected}
    snapshots: dict[str, dict[int, dict[str, str]]] = {
        gate_id: {-1: dict(active[gate_id])} for gate_id in affected
    }
    histories: dict[str, list[tuple[int, str]]] = {
        gate_id: [(-1, "pending")] for gate_id in affected
    }
    retry_due: dict[str, int] = {}
    last_error: dict[str, dict[str, Any] | None] = {
        gate_id: None for gate_id in affected
    }

    def run_batch(batch_no: int, base_gate_ids: list[str]) -> None:
        due = sorted(
            gate_id
            for gate_id, due_batch in retry_due.items()
            if due_batch <= batch_no
        )
        for gate_id in sorted(set(base_gate_ids).union(due)):
            if states[gate_id] not in {"pending", "failed"}:
                continue
            if states[gate_id] == "failed" and gate_id not in retry_due:
                continue
            attempts[gate_id] += 1
            fault = project.faults.get(gate_id, {}).get(attempts[gate_id])
            if fault is None:
                states[gate_id] = "active"
                active[gate_id] = dict(targets[gate_id])
                activated[gate_id] = batch_no
                snapshots[gate_id][batch_no] = dict(targets[gate_id])
                histories[gate_id].append((batch_no, "active"))
                last_error[gate_id] = None
                retry_due.pop(gate_id, None)
            else:
                states[gate_id] = "failed"
                last_error[gate_id] = {
                    "error_code": fault["error_code"],
                    "retryable": fault["retryable"],
                }
                histories[gate_id].append((batch_no, "failed"))
                if fault["retryable"]:
                    retry_due[gate_id] = batch_no + 1
                else:
                    retry_due.pop(gate_id, None)

    for batch in batches:
        run_batch(int(batch["batch_no"]), list(batch["gate_ids"]))
    next_batch = max((int(batch["batch_no"]) for batch in batches), default=-1) + 1
    while retry_due:
        run_batch(next_batch, [])
        next_batch += 1

    return {
        "states": states,
        "active": active,
        "targets": targets,
        "attempts": attempts,
        "activated": activated,
        "snapshots": snapshots,
        "histories": histories,
        "last_error": last_error,
    }


def trace_field(
    project: ReferenceProject,
    event_id: str,
    version: str,
    field_name: str,
    value: Any,
) -> dict[str, Any]:
    return {
        "field": field_name,
        "source": project.sources[(event_id, version)][field_name],
        "value": value,
    }


def judge_request(
    project: ReferenceProject,
    request: dict[str, Any],
    simulation: dict[str, Any],
) -> dict[str, Any]:
    gate_id = str(request["gate_id"])
    after_batch = request.get("after_batch")
    if after_batch is None:
        state = simulation["states"][gate_id]
        versions = simulation["active"][gate_id]
    else:
        histories = [
            item for item in simulation["histories"][gate_id] if item[0] <= after_batch
        ]
        state = histories[-1][1] if histories else "pending"
        candidates = [
            (batch_no, value)
            for batch_no, value in simulation["snapshots"][gate_id].items()
            if batch_no <= after_batch
        ]
        versions = dict(max(candidates, key=lambda item: item[0])[1])

    event_id = str(request["event_id"])
    version = versions[event_id]
    rule = project.rules[(event_id, version)]
    target_versions = simulation["targets"][gate_id]
    mode = (
        "current"
        if state == "active" and versions == target_versions
        else "degraded"
    )
    checked = [
        trace_field(
            project,
            event_id,
            version,
            "ticket_whitelist",
            list(rule["ticket_whitelist"]),
        ),
        trace_field(project, event_id, version, "entry_window.start", rule["start"]),
        trace_field(project, event_id, version, "entry_window.end", rule["end"]),
        trace_field(
            project,
            event_id,
            version,
            "child_policy.requires_adult",
            rule["requires_adult"],
        ),
        trace_field(
            project, event_id, version, "reentry_policy", rule["reentry_policy"]
        ),
    ]
    trace: dict[str, Any] = {
        "event_id": event_id,
        "rule_version": version,
        "checked_fields": checked,
        "decisive": None,
    }
    decision = "ALLOW"
    reason = "ALLOWED"
    decisive_field = ""

    if request["ticket_type"] not in rule["ticket_whitelist"]:
        decision = "REJECT"
        reason = "TICKET_NOT_WHITELISTED"
        decisive_field = "ticket_whitelist"
        trace["decisive"] = trace_field(
            project,
            event_id,
            version,
            "ticket_whitelist",
            list(rule["ticket_whitelist"]),
        )
    elif parse_time(request["at"]) < rule["start_at"]:
        decision = "REJECT"
        reason = "BEFORE_ENTRY_WINDOW"
        decisive_field = "entry_window.start"
        trace["decisive"] = trace_field(
            project, event_id, version, "entry_window.start", rule["start"]
        )
    elif parse_time(request["at"]) > rule["end_at"]:
        decision = "REJECT"
        reason = "AFTER_ENTRY_WINDOW"
        decisive_field = "entry_window.end"
        trace["decisive"] = trace_field(
            project, event_id, version, "entry_window.end", rule["end"]
        )
    elif (
        bool(request.get("is_child", request["ticket_type"] == "CHILD"))
        and rule["requires_adult"]
        and int(request.get("accompanied_adult_count", 0)) < 1
    ):
        decision = "REJECT"
        reason = "CHILD_REQUIRES_ADULT"
        decisive_field = "child_policy.requires_adult"
        trace["decisive"] = trace_field(
            project,
            event_id,
            version,
            "child_policy.requires_adult",
            True,
        )
    elif (
        rule["reentry_policy"] == "single"
        and int(request.get("prior_entries", 0)) >= 1
    ):
        decision = "REJECT"
        reason = "REENTRY_NOT_ALLOWED"
        decisive_field = "reentry_policy"
        trace["decisive"] = trace_field(
            project, event_id, version, "reentry_policy", rule["reentry_policy"]
        )

    return {
        "request_id": request["request_id"],
        "gate_id": gate_id,
        "event_id": event_id,
        "snapshot_after_batch": after_batch,
        "state_at_snapshot": state,
        "rule_mode": mode,
        "decision": decision,
        "reason_code": reason,
        "rule_version": version,
        "effective_versions": dict(sorted(versions.items())),
        "target_versions": dict(sorted(target_versions.items())),
        "inputs": {
            "at": request["at"],
            "ticket_type": request["ticket_type"],
            "prior_entries": int(request.get("prior_entries", 0)),
            "is_child": bool(
                request.get("is_child", request["ticket_type"] == "CHILD")
            ),
            "accompanied_adult_count": int(
                request.get("accompanied_adult_count", 0)
            ),
        },
        "trace": trace,
    }


def degradation(
    project: ReferenceProject, simulation: dict[str, Any]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for gate_id in project.affected():
        active = simulation["active"][gate_id]
        target = simulation["targets"][gate_id]
        if active == target:
            continue
        differences: list[dict[str, Any]] = []
        for event_id in sorted(set(active).union(target)):
            active_version = active.get(event_id)
            target_version = target.get(event_id)
            if active_version == target_version:
                continue
            fields: list[dict[str, Any]] = []
            if active_version is not None and target_version is not None:
                active_fields = comparable(project.rules[(event_id, active_version)])
                target_fields = comparable(project.rules[(event_id, target_version)])
                for field_name in sorted(active_fields):
                    if active_fields[field_name] == target_fields[field_name]:
                        continue
                    fields.append(
                        {
                            "field": field_name,
                            "active_value": active_fields[field_name],
                            "target_value": target_fields[field_name],
                            "active_source": project.sources[
                                (event_id, active_version)
                            ][field_name],
                            "target_source": project.sources[
                                (event_id, target_version)
                            ][field_name],
                        }
                    )
            differences.append(
                {
                    "event_id": event_id,
                    "active_version": active_version,
                    "target_version": target_version,
                    "fields": fields,
                }
            )
        state = simulation["states"][gate_id]
        error = simulation["last_error"][gate_id]
        if state == "failed" and error and not error["retryable"]:
            reason = "nonretryable_failure"
        elif state == "failed":
            reason = "retry_pending"
        else:
            reason = "not_activated"
        output.append(
            {
                "gate_id": gate_id,
                "state": state,
                "state_label": {
                    "pending": "待传播",
                    "propagating": "传播中",
                    "active": "已生效",
                    "failed": "传播失败",
                }[state],
                "reason": reason,
                "frozen_versions": dict(sorted(active.items())),
                "latest_versions": dict(sorted(target.items())),
                "event_differences": differences,
            }
        )
    return output


def stats(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    by_result: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    for decision in decisions:
        by_result[decision["decision"]] = by_result.get(decision["decision"], 0) + 1
        by_reason[decision["reason_code"]] = (
            by_reason.get(decision["reason_code"], 0) + 1
        )
        by_mode[decision["rule_mode"]] = by_mode.get(decision["rule_mode"], 0) + 1
    return {
        "total": len(decisions),
        "by_result": dict(sorted(by_result.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "by_rule_mode": dict(sorted(by_mode.items())),
        "degraded_decisions": by_mode.get("degraded", 0),
    }


def build_reference(project: ReferenceProject) -> dict[str, Any]:
    affected = project.affected()
    dependency_map = project.dependencies()
    affected_set = set(affected)
    dependencies = [
        {
            "upstream_gate_id": upstream,
            "downstream_gate_id": downstream,
            "reasons": sorted(reasons),
        }
        for (upstream, downstream), reasons in sorted(dependency_map.items())
        if upstream in affected_set and downstream in affected_set
    ]
    batches, cycles = make_batches(affected, dependency_map)
    simulation = simulate(project, batches)
    decisions = [
        judge_request(project, request, simulation) for request in project.requests
    ]
    gate_versions = [
        {
            "gate_id": gate_id,
            "state": simulation["states"][gate_id],
            "target_versions": dict(sorted(simulation["targets"][gate_id].items())),
            "active_versions": dict(sorted(simulation["active"][gate_id].items())),
            "activated_batch": simulation["activated"][gate_id],
            "attempt_count": simulation["attempts"][gate_id],
        }
        for gate_id in affected
    ]
    return {
        "reference_version": 1,
        "project_id": project.project_id,
        "seed": project.seed,
        "affected_gate_ids": affected,
        "dependencies": dependencies,
        "batches": batches,
        "cycles": cycles,
        "multi_subscribed_conflicting_gates": project.conflicts(affected),
        "gate_versions": gate_versions,
        "degraded_gates": degradation(project, simulation),
        "decisions": decisions,
        "decision_stats": stats(decisions),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent reference judge")
    parser.add_argument("--project", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    project = ReferenceProject(Path(args.project).resolve())
    reference = build_reference(project)
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(reference, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(
        json.dumps(
            {
                "affected_gates": len(reference["affected_gate_ids"]),
                "batches": len(reference["batches"]),
                "decisions": len(reference["decisions"]),
                "degraded_gates": len(reference["degraded_gates"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
