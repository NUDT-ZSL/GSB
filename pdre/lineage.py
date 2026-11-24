"""Process lineage graph normalization and ambiguity detection."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from .canonical import short_stable_hash
from .codes import BusinessCode as Codes
from .models import EventRecord, LineageRecord


@dataclass(frozen=True)
class Assignment:
    unit_id: str
    unit_type: str
    anchor_guid: str
    root_guid: Optional[str] = None
    candidate_root_guids: tuple[str, ...] = ()
    provisional_root_guids: tuple[str, ...] = ()
    cycle_guids: tuple[str, ...] = ()
    issue_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "unit_type": self.unit_type,
            "anchor_guid": self.anchor_guid,
            "root_guid": self.root_guid,
            "candidate_root_guids": list(self.candidate_root_guids),
            "provisional_root_guids": list(self.provisional_root_guids),
            "cycle_guids": list(self.cycle_guids),
            "issue_codes": list(self.issue_codes),
        }


@dataclass
class LineageView:
    options: dict[str, dict[Optional[str], list[LineageRecord]]]
    reverse_children: dict[str, set[str]]
    known_guids: set[str]
    declared_guids: set[str]
    assignments: dict[str, Assignment]
    issues: list[dict[str, Any]]
    forks: list[dict[str, Any]]
    missing: list[dict[str, Any]]
    cycles: list[dict[str, Any]]
    self_references: list[dict[str, Any]]

    def descendants_of(self, process_guid: str) -> set[str]:
        result = {process_guid}
        stack = [process_guid]
        while stack:
            current = stack.pop()
            for child in self.reverse_children.get(current, set()):
                if child not in result:
                    result.add(child)
                    stack.append(child)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "known_guid_count": len(self.known_guids),
            "declared_guid_count": len(self.declared_guids),
            "issue_count": len(self.issues),
            "fork_count": len(self.forks),
            "missing_parent_count": len(self.missing),
            "cycle_count": len(self.cycles),
            "self_reference_count": len(self.self_references),
            "issues": self.issues,
            "forks": self.forks,
            "missing_parent_issues": self.missing,
            "cycle_issues": self.cycles,
            "self_reference_issues": self.self_references,
            "assignments": {
                guid: self.assignments[guid].to_dict()
                for guid in sorted(self.assignments)
            },
        }


def _evidence(records: list[LineageRecord]) -> dict[str, Any]:
    sessions = sorted({record.session_id for record in records})
    source_refs = sorted({record.source.source_ref for record in records})
    return {"session_ids": sessions, "source_refs": source_refs}


def _canonical_cycle(nodes: list[str]) -> list[str]:
    if not nodes:
        return []
    rotations = []
    for index, node in enumerate(nodes):
        if node == min(nodes):
            rotations.append(tuple(nodes[index:] + nodes[:index]))
    chosen = min(rotations) if rotations else tuple(nodes)
    return list(chosen)


def build_lineage_view(
    events: list[EventRecord], records: list[LineageRecord]
) -> LineageView:
    options: dict[str, dict[Optional[str], list[LineageRecord]]] = defaultdict(lambda: defaultdict(list))
    reverse_children: dict[str, set[str]] = defaultdict(set)
    declared_guids: set[str] = set()
    known_guids: set[str] = set()

    for record in records:
        declared_guids.add(record.process_guid)
        known_guids.add(record.process_guid)
        options[record.process_guid][record.parent_process_guid].append(record)
        if record.parent_process_guid is not None:
            known_guids.add(record.parent_process_guid)
            reverse_children[record.parent_process_guid].add(record.process_guid)

    for event in events:
        known_guids.add(event.process_guid)
        if event.parent_process_guid is not None:
            known_guids.add(event.parent_process_guid)

    missing: list[dict[str, Any]] = []
    for guid in sorted(known_guids - declared_guids):
        referenced_by_events = sorted(
            {
                event.event_seq
                for event in events
                if event.process_guid == guid or event.parent_process_guid == guid
            }
        )
        missing.append(
            {
                "code": Codes.LINEAGE_MISSING_PARENT,
                "process_guid": guid,
                "treated_as": "provisional_terminal_root",
                "referenced_by_event_seqs": referenced_by_events,
                "message": "no lineage declaration exists for this process GUID",
            }
        )

    self_references: list[dict[str, Any]] = []
    for child in sorted(options):
        if child in options[child]:
            evidence = _evidence(options[child][child])
            self_references.append(
                {
                    "code": Codes.LINEAGE_SELF_REFERENCE,
                    "process_guid": child,
                    "cycle_guids": [child, child],
                    **evidence,
                }
            )

    cycles: list[dict[str, Any]] = []
    seen_cycles: set[tuple[str, ...]] = set()
    adjacency = {
        child: sorted(parent for parent in child_options if parent is not None)
        for child, child_options in options.items()
    }

    for start in sorted(options):
        if start not in adjacency:
            continue

        def dfs(current: str, path: list[str]) -> None:
            for nxt in adjacency.get(current, []):
                if nxt == start and len(path) >= 2:
                    canonical = tuple(_canonical_cycle(path))
                    if canonical not in seen_cycles:
                        seen_cycles.add(canonical)
                        cycles.append(
                            {
                                "code": Codes.LINEAGE_CYCLE,
                                "process_guid": canonical[0],
                                "process_guids": list(canonical),
                                "cycle_guids": list(canonical) + [canonical[0]],
                                "length": len(canonical),
                            }
                        )
                    continue
                if nxt < start or nxt in path:
                    continue
                dfs(nxt, path + [nxt])

        dfs(start, [start])

    cycles.sort(key=lambda item: tuple(item["process_guids"]))

    forks: list[dict[str, Any]] = []
    for child in sorted(options):
        child_options = options[child]
        if len(child_options) <= 1:
            continue

        candidate_entries = []
        for parent in sorted(child_options, key=lambda value: (value is None, value or "")):
            candidate_entries.append(
                {
                    "parent_process_guid": parent,
                    **_evidence(child_options[parent]),
                }
            )

        candidate_roots, provisional_roots = _resolve_candidate_roots(child, options)
        forks.append(
            {
                "code": Codes.LINEAGE_FORK,
                "process_guid": child,
                "candidates": candidate_entries,
                "candidate_root_guids": sorted(candidate_roots),
                "provisional_root_guids": sorted(provisional_roots),
                "message": "process has multiple incompatible parent declarations across sessions",
            }
        )

    assignments = {
        event.process_guid: _classify(event.process_guid, options)
        for event in events
    }

    issues = [*missing, *self_references, *cycles, *forks]
    issues.sort(
        key=lambda item: (
            str(item.get("code", "")),
            str(item.get("process_guid", "")),
            short_stable_hash(item, 16),
        )
    )
    forks.sort(key=lambda item: item["process_guid"])
    return LineageView(
        options=dict(options),
        reverse_children=dict(reverse_children),
        known_guids=known_guids,
        declared_guids=declared_guids,
        assignments=assignments,
        issues=issues,
        forks=forks,
        missing=missing,
        cycles=cycles,
        self_references=self_references,
    )


def _resolve_candidate_roots(
    start: str,
    options: dict[str, dict[Optional[str], list[LineageRecord]]],
) -> tuple[set[str], set[str]]:
    roots: set[str] = set()
    provisional: set[str] = set()
    stack = [start]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        node_options = options.get(node)
        if node_options is None:
            roots.add(node)
            provisional.add(node)
            continue
        if None in node_options:
            roots.add(node)
        for parent in node_options:
            if parent is not None:
                stack.append(parent)
    return roots, provisional


def _classify(
    start: str,
    options: dict[str, dict[Optional[str], list[LineageRecord]]],
) -> Assignment:
    path: list[str] = []
    position: dict[str, int] = {}
    node = start

    while True:
        if node in position:
            cycle_nodes = path[position[node] :]
            canonical = _canonical_cycle(cycle_nodes)
            digest = short_stable_hash("|".join(canonical), 12)
            return Assignment(
                unit_id=f"unit:cycle:{canonical[0]}:{digest}",
                unit_type="cycle",
                anchor_guid=canonical[0],
                cycle_guids=tuple(canonical),
                issue_codes=(Codes.LINEAGE_CYCLE,),
            )

        position[node] = len(path)
        path.append(node)
        node_options = options.get(node)

        if node_options is None:
            return Assignment(
                unit_id=f"unit:missing:{node}",
                unit_type="missing_parent",
                anchor_guid=node,
                root_guid=node,
                candidate_root_guids=(node,),
                provisional_root_guids=(node,),
                issue_codes=(Codes.LINEAGE_MISSING_PARENT,),
            )

        if node in node_options:
            return Assignment(
                unit_id=f"unit:self:{node}",
                unit_type="self_reference",
                anchor_guid=node,
                root_guid=node,
                cycle_guids=(node, node),
                issue_codes=(Codes.LINEAGE_SELF_REFERENCE,),
            )

        if len(node_options) > 1:
            roots, provisional = _resolve_candidate_roots(node, options)
            return Assignment(
                unit_id=f"unit:fork:{node}",
                unit_type="fork",
                anchor_guid=node,
                candidate_root_guids=tuple(sorted(roots)),
                provisional_root_guids=tuple(sorted(provisional)),
                issue_codes=(Codes.LINEAGE_FORK,),
            )

        parent = next(iter(node_options))
        if parent is None:
            return Assignment(
                unit_id=f"unit:root:{node}",
                unit_type="root",
                anchor_guid=node,
                root_guid=node,
            )
        node = parent
