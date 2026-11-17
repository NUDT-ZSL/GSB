"""Deterministic chain reconstruction, drift adjudication and event clustering."""

from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .codes import Code
from .issues import Issue, issue_sort_key
from .models import BaselineRule, FileResult, RawEvidence

LINK_ERROR_CODES = {
    Code.LINK_TARGET_MISSING,
    Code.LINK_TARGET_TAMPERED,
    Code.LINK_TARGET_CONFLICT,
    Code.LINK_CROSS_CHAIN,
    Code.LINK_SEQUENCE_ORDER,
    Code.SELF_LINK,
    Code.LINK_CYCLE,
    Code.CHAIN_FORK,
    Code.CHAIN_MULTIPLE_ROOTS,
    Code.CHAIN_BROKEN,
}


@dataclass
class PreparedEvidence:
    deliveries: list[RawEvidence] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    tampered: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Adjudication:
    report: dict[str, Any]
    graph: dict[str, Any]


def _record_key(record: RawEvidence) -> tuple[Any, ...]:
    return (
        record.host_id,
        record.path,
        record.sequence,
        record.evidence_id,
        record.file_id,
        record.record_index,
    )


def _delivery_key(record: RawEvidence) -> tuple[Any, ...]:
    return (
        record.host_id,
        record.path,
        record.sequence,
        record.evidence_id,
        record.file_index,
        record.record_index,
    )


def _node_issue(record: RawEvidence, code: str, **details: Any) -> Issue:
    return Issue(
        code,
        file_id=record.file_id,
        file_index=record.file_index,
        record_index=record.record_index,
        host_id=record.host_id,
        sequence=record.sequence,
        path=record.path,
        details=details,
    )


def _record_report(record: RawEvidence) -> dict[str, Any]:
    item: dict[str, Any] = {
        "evidence_id": record.evidence_id,
        "aliases": list(record.aliases),
        "host_id": record.host_id,
        "path": record.path,
        "sequence": record.sequence,
        "value": record.value,
        "algorithm": record.algorithm,
        "digest": record.digest,
        "digest_bits": record.digest_bits,
        "previous_evidence_id": record.previous_evidence_id,
        "file_id": record.file_id,
        "file_index": record.file_index,
        "record_index": record.record_index,
        "sync_marker": record.sync_marker,
        "tampered": record.tampered,
        "conflict": record.conflict,
    }
    if record.has_sender_observed_value:
        item["sender_observed_value"] = record.sender_observed_value
    return item


def prepare_evidence(
    file_results: Iterable[FileResult],
) -> PreparedEvidence:
    """Deduplicate deliveries and identify digest conflicts/ID collisions."""
    prepared = PreparedEvidence()
    all_records: list[RawEvidence] = []
    for file_result in sorted(file_results, key=lambda item: item.source_id):
        all_records.extend(file_result.records)

    for record in all_records:
        if record.tampered:
            prepared.tampered.append(
                {
                    "host_id": record.host_id,
                    "sequence": record.sequence,
                    "path": record.path,
                    "evidence_id": record.evidence_id,
                    "file_id": record.file_id,
                    "file_index": record.file_index,
                    "record_index": record.record_index,
                    "digest": record.digest,
                    "code": Code.TAMPERED,
                }
            )

    groups: dict[tuple[str, int], list[RawEvidence]] = defaultdict(list)
    for record in all_records:
        groups[(record.host_id, record.sequence)].append(record)

    deliveries: list[RawEvidence] = []
    for (host_id, sequence), records in sorted(groups.items()):
        signatures = sorted({record.signature() for record in records})
        if len(signatures) > 1:
            conflict = {
                "host_id": host_id,
                "sequence": sequence,
                "signatures": [
                    {"algorithm": algorithm, "digest": digest}
                    for algorithm, digest in signatures
                ],
                "records": [
                    {
                        "evidence_id": record.evidence_id,
                        "path": record.path,
                        "file_id": record.file_id,
                        "file_index": record.file_index,
                        "record_index": record.record_index,
                    }
                    for record in sorted(records, key=_record_key)
                ],
            }
            prepared.conflicts.append(conflict)
            for record in sorted(records, key=_record_key):
                prepared.issues.append(
                    _node_issue(
                        record,
                        Code.EVIDENCE_CONFLICT,
                        signatures=conflict["signatures"],
                    )
                )
            chosen = copy.copy(sorted(records, key=_record_key)[0])
            aliases: set[str] = set()
            for record in records:
                aliases.add(record.evidence_id)
                aliases.update(record.aliases)
            chosen.aliases = sorted(aliases)
            chosen.conflict = True
            deliveries.append(chosen)
            continue

        chosen = copy.copy(
            sorted(
                records,
                key=lambda item: (
                    item.tampered,
                    item.file_index,
                    item.record_index,
                    item.evidence_id,
                ),
            )[0]
        )
        aliases: set[str] = set()
        for record in records:
            aliases.add(record.evidence_id)
            aliases.update(record.aliases)
        chosen.aliases = sorted(aliases)
        deliveries.append(chosen)

    deliveries.sort(key=_delivery_key)

    alias_groups: dict[str, set[int]] = defaultdict(set)
    for position, record in enumerate(deliveries):
        for alias in set(record.aliases) | {record.evidence_id}:
            alias_groups[alias].add(position)

    for position, record in enumerate(deliveries):
        conflicting_aliases = sorted(
            alias for alias in set(record.aliases) | {record.evidence_id}
            if len(alias_groups[alias]) > 1
        )
        if conflicting_aliases:
            record.conflict = True
            prepared.issues.append(
                _node_issue(
                    record,
                    Code.EVIDENCE_ID_DUPLICATE,
                    aliases=conflicting_aliases,
                )
            )

    prepared.deliveries = deliveries
    prepared.tampered.sort(
        key=lambda item: (
            item["host_id"],
            item["sequence"],
            item["path"],
            item["evidence_id"],
            item["file_index"],
            item["record_index"],
        )
    )
    prepared.issues.sort(key=issue_sort_key)
    return prepared


def _cycle_sequences(
    nodes: list[RawEvidence],
    pointer: dict[str, RawEvidence],
) -> list[list[str]]:
    state: dict[str, int] = {}
    cycles: list[list[str]] = []
    for start in sorted(nodes, key=_record_key):
        if start.evidence_id in state:
            continue
        local_position: dict[str, int] = {}
        path: list[str] = []
        current = start
        while True:
            current_id = current.evidence_id
            if current_id in local_position:
                cycle = path[local_position[current_id] :]
                cycles.append(sorted(cycle))
                break
            if current_id in state:
                break
            local_position[current_id] = len(path)
            path.append(current_id)
            target = pointer.get(current_id)
            if target is None:
                break
            current = target
        for node_id in path:
            state.setdefault(node_id, 1)
    return cycles


def _build_chains(
    prepared: PreparedEvidence,
    baseline_index: dict[tuple[str, str], list[BaselineRule]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Issue]]:
    issues: list[Issue] = []
    chain_reports: list[dict[str, Any]] = []
    drift_points: list[dict[str, Any]] = []

    valid_nodes = [
        record
        for record in prepared.deliveries
        if not record.tampered and not record.conflict
    ]
    id_index: dict[str, RawEvidence] = {}
    for record in valid_nodes:
        for alias in set(record.aliases) | {record.evidence_id}:
            id_index[alias] = record

    excluded_reason: dict[str, str] = {}
    for record in prepared.deliveries:
        if record.tampered:
            for alias in set(record.aliases) | {record.evidence_id}:
                excluded_reason.setdefault(alias, Code.LINK_TARGET_TAMPERED)
        elif record.conflict:
            for alias in set(record.aliases) | {record.evidence_id}:
                excluded_reason.setdefault(alias, Code.LINK_TARGET_CONFLICT)

    by_config: dict[tuple[str, str], list[RawEvidence]] = defaultdict(list)
    for record in valid_nodes:
        by_config[(record.host_id, record.path)].append(record)

    for (host_id, path), nodes in sorted(by_config.items()):
        nodes.sort(key=_record_key)
        chain_issues: list[Issue] = []
        pointer: dict[str, RawEvidence] = {}
        children: dict[str, list[RawEvidence]] = defaultdict(list)
        roots: list[RawEvidence] = []
        self_linked: list[RawEvidence] = []

        for record in nodes:
            previous = record.previous_evidence_id
            if previous is None:
                roots.append(record)
                continue
            if previous in set(record.aliases) | {record.evidence_id}:
                self_linked.append(record)
                chain_issues.append(
                    _node_issue(record, Code.SELF_LINK, sequences=[record.sequence])
                )
                continue
            target = id_index.get(previous)
            if target is None:
                code = excluded_reason.get(previous, Code.LINK_TARGET_MISSING)
                chain_issues.append(
                    _node_issue(
                        record,
                        code,
                        target_evidence_id=previous,
                        sequences=[record.sequence],
                    )
                )
                continue
            if target.host_id != host_id or target.path != path:
                chain_issues.append(
                    _node_issue(
                        record,
                        Code.LINK_CROSS_CHAIN,
                        target_evidence_id=previous,
                        target_host_id=target.host_id,
                        target_path=target.path,
                        sequences=[record.sequence],
                    )
                )
                continue
            pointer[record.evidence_id] = target
            children[target.evidence_id].append(record)
            if target.sequence >= record.sequence:
                chain_issues.append(
                    _node_issue(
                        record,
                        Code.LINK_SEQUENCE_ORDER,
                        target_evidence_id=target.evidence_id,
                        target_sequence=target.sequence,
                        sequences=[record.sequence, target.sequence],
                    )
                )

        for target_id, linked_nodes in sorted(children.items()):
            if len(linked_nodes) > 1:
                first = next(node for node in nodes if node.evidence_id == target_id)
                sequences = sorted([first.sequence] + [node.sequence for node in linked_nodes])
                chain_issues.append(
                    _node_issue(
                        first,
                        Code.CHAIN_FORK,
                        target_evidence_id=target_id,
                        linked_sequences=[node.sequence for node in linked_nodes],
                        sequences=sequences,
                    )
                )

        if len(roots) > 1:
            anchor = roots[0]
            chain_issues.append(
                _node_issue(
                    anchor,
                    Code.CHAIN_MULTIPLE_ROOTS,
                    root_sequences=[record.sequence for record in roots],
                    sequences=[record.sequence for record in roots],
                )
            )

        cycle_ids: set[str] = set()
        for cycle in _cycle_sequences(nodes, pointer):
            cycle_ids.update(cycle)
            cycle_nodes = sorted(
                (node for node in nodes if node.evidence_id in set(cycle)),
                key=_record_key,
            )
            if cycle_nodes:
                chain_issues.append(
                    _node_issue(
                        cycle_nodes[0],
                        Code.LINK_CYCLE,
                        evidence_ids=cycle,
                        sequences=[node.sequence for node in cycle_nodes],
                    )
                )

        structural_codes = {
            issue.code for issue in chain_issues if issue.code in LINK_ERROR_CODES
        }
        if structural_codes:
            chain_issues.append(
                _node_issue(
                    nodes[0],
                    Code.CHAIN_BROKEN,
                    causes=sorted(structural_codes),
                    sequences=[node.sequence for node in nodes],
                )
            )

        if cycle_ids:
            status = "cycle"
        elif self_linked:
            status = "self_link"
        elif structural_codes:
            status = "broken"
        else:
            status = "ok"

        drift: dict[str, Any] | None = None
        unevaluated: list[int] = []
        rules = baseline_index.get((host_id, path), [])
        if status == "ok":
            if not rules:
                status = "baseline_missing"
                chain_issues.append(
                    _node_issue(nodes[0], Code.BASELINE_MISSING, sequences=[])
                )
            else:
                for record in nodes:
                    active_rules = [rule for rule in rules if rule.active_at(record.sequence)]
                    if not active_rules:
                        unevaluated.append(record.sequence)
                        continue
                    if any(rule.permitted(record.value) for rule in active_rules):
                        continue
                    drift = {
                        "host_id": host_id,
                        "path": path,
                        "sequence": record.sequence,
                        "evidence_id": record.evidence_id,
                        "value": record.value,
                        "baseline_source": active_rules[0].source,
                        "authorization_sources": [
                            rule.to_dict() for rule in active_rules
                        ],
                    }
                    drift_points.append(drift)
                    break
                if drift is None and unevaluated and len(unevaluated) == len(nodes):
                    status = "baseline_not_effective"
                    chain_issues.append(
                        _node_issue(
                            nodes[0],
                            Code.BASELINE_NOT_EFFECTIVE,
                            sequences=unevaluated,
                        )
                    )

        chain_issues.sort(key=issue_sort_key)
        issues.extend(chain_issues)
        chain_reports.append(
            {
                "host_id": host_id,
                "path": path,
                "status": status,
                "sequences": [record.sequence for record in nodes],
                "observations": [_record_report(record) for record in nodes],
                "errors": [issue.to_dict() for issue in chain_issues],
                "drift": copy.deepcopy(drift),
                "unevaluated_sequences": unevaluated,
            }
        )

    chain_reports.sort(key=lambda item: (item["host_id"], item["path"]))
    drift_points.sort(
        key=lambda item: (
            item["sequence"],
            item["host_id"],
            item["path"],
            item["evidence_id"],
        )
    )
    return chain_reports, drift_points, issues


def _event_clusters(drift_points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for drift in drift_points:
        by_path[drift["path"]].append(drift)
    clusters: list[dict[str, Any]] = []
    for path, members in sorted(by_path.items()):
        ordered = sorted(
            members,
            key=lambda item: (
                item["sequence"],
                item["host_id"],
                item["evidence_id"],
            ),
        )
        host_ids = sorted({item["host_id"] for item in ordered})
        clusters.append(
            {
                "cluster_id": f"event:{path}",
                "path": path,
                "host_ids": host_ids,
                "cross_host": len(host_ids) > 1,
                "first_sequence": ordered[0]["sequence"],
                "member_count": len(ordered),
                "members": copy.deepcopy(ordered),
            }
        )
    return clusters


def _chain_break_list(chain_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    codes = LINK_ERROR_CODES - {Code.CHAIN_BROKEN}
    for chain in chain_reports:
        for issue in chain["errors"]:
            if issue["code"] not in codes:
                continue
            result.append(
                {
                    "host_id": chain["host_id"],
                    "path": chain["path"],
                    "chain_status": chain["status"],
                    "code": issue["code"],
                    "sequences": issue.get("details", {}).get(
                        "sequences", chain["sequences"]
                    ),
                    "file_id": issue.get("file_id"),
                    "file_index": issue.get("file_index"),
                    "record_index": issue.get("record_index"),
                }
            )
    result.sort(
        key=lambda item: (
            item["host_id"],
            item["path"],
            item["code"],
            tuple(item["sequences"]),
        )
    )
    return result


def build_graph(
    prepared: PreparedEvidence,
    chain_reports: list[dict[str, Any]],
    drift_points: list[dict[str, Any]],
) -> dict[str, Any]:
    chain_map = {
        (chain["host_id"], chain["path"]): chain for chain in chain_reports
    }
    drift_keys = {
        (item["host_id"], item["path"], item["sequence"], item["evidence_id"])
        for item in drift_points
    }
    all_alias_map: dict[str, list[RawEvidence]] = defaultdict(list)
    for record in prepared.deliveries:
        for alias in set(record.aliases) | {record.evidence_id}:
            all_alias_map[alias].append(record)

    nodes: list[dict[str, Any]] = []
    for record in sorted(prepared.deliveries, key=_delivery_key):
        chain = chain_map.get((record.host_id, record.path))
        if record.tampered:
            chain_status = "excluded_tampered"
        elif record.conflict:
            chain_status = "excluded_conflict"
        elif chain is not None:
            chain_status = chain["status"]
        else:
            chain_status = "excluded"
        node = _record_report(record)
        node.update(
            {
                "node_id": record.evidence_id,
                "chain_status": chain_status,
                "drift": (
                    record.host_id,
                    record.path,
                    record.sequence,
                    record.evidence_id,
                )
                in drift_keys,
                "excluded": bool(record.tampered or record.conflict),
                "root": record.previous_evidence_id is None,
            }
        )
        nodes.append(node)

    cycle_ids: set[str] = set()
    for chain in chain_reports:
        for issue in chain["errors"]:
            if issue["code"] == Code.LINK_CYCLE:
                cycle_ids.update(issue.get("details", {}).get("evidence_ids", []))

    edges: list[dict[str, Any]] = []
    for record in sorted(prepared.deliveries, key=_delivery_key):
        previous = record.previous_evidence_id
        if previous is None:
            continue
        candidates = all_alias_map.get(previous, [])
        target = candidates[0] if len(candidates) == 1 else None
        if previous in set(record.aliases) | {record.evidence_id}:
            status = "self"
        elif len(candidates) > 1:
            status = "duplicate_id"
        elif target is None:
            status = "missing"
        elif target.tampered:
            status = "tampered_target"
        elif target.conflict:
            status = "conflict_target"
        elif target.host_id != record.host_id or target.path != record.path:
            status = "cross_chain"
        elif target.sequence >= record.sequence:
            status = "sequence_order"
        elif record.evidence_id in cycle_ids and target.evidence_id in cycle_ids:
            status = "cycle"
        else:
            status = "ok"
        edges.append(
            {
                "from_node": target.evidence_id if target is not None else previous,
                "to_node": record.evidence_id,
                "target_evidence_id": previous,
                "host_id": record.host_id,
                "path": record.path,
                "resolved": target is not None,
                "status": status,
            }
        )

    nodes.sort(key=lambda item: (item["host_id"], item["path"], item["sequence"], item["node_id"]))
    edges.sort(
        key=lambda item: (
            item["host_id"],
            item["path"],
            item["to_node"],
            item["from_node"],
        )
    )
    return {
        "schema_version": 1,
        "nodes": nodes,
        "edges": edges,
    }


def adjudicate(
    *,
    file_results: list[FileResult],
    baseline_rules: list[BaselineRule],
    baseline_issues: list[Issue],
    context_issues: list[Issue] | None = None,
) -> Adjudication:
    prepared = prepare_evidence(file_results)
    baseline_index: dict[tuple[str, str], list[BaselineRule]] = defaultdict(list)
    for rule in baseline_rules:
        baseline_index[(rule.host_id, rule.path)].append(rule)
    for rules in baseline_index.values():
        rules.sort(
            key=lambda item: (
                item.effective_from_seq if item.effective_from_seq is not None else -1,
                item.effective_to_seq if item.effective_to_seq is not None else 2**63 - 1,
                item.source,
                item.declaration_index,
            )
        )

    chains, drift_points, chain_issues = _build_chains(prepared, baseline_index)
    clusters = _event_clusters(drift_points)
    graph = build_graph(prepared, chains, drift_points)
    chain_breaks = _chain_break_list(chains)

    all_issues: list[Issue] = []
    all_issues.extend(baseline_issues)
    all_issues.extend(context_issues or [])
    for file_result in file_results:
        all_issues.extend(file_result.issues)
    all_issues.extend(prepared.issues)
    all_issues.extend(chain_issues)

    unique_issues: dict[tuple[Any, ...], Issue] = {}
    for issue in all_issues:
        key = (
            issue.code,
            issue.file_id,
            issue.file_index,
            issue.record_index,
            issue.host_id,
            issue.sequence,
            issue.path,
            repr(sorted(issue.details.items(), key=lambda item: item[0])),
        )
        unique_issues.setdefault(key, issue)
    ordered_issues = sorted(unique_issues.values(), key=issue_sort_key)

    issue_counts_by_file: dict[str, int] = defaultdict(int)
    for issue in ordered_issues:
        if issue.file_id:
            issue_counts_by_file[issue.file_id] += 1

    files = []
    for file_result in sorted(file_results, key=lambda item: item.source_id):
        item = file_result.to_dict()
        item["cross_file_issue_count"] = max(
            0, issue_counts_by_file[file_result.file_id] - len(file_result.issues)
        )
        files.append(item)

    evidence = [_record_report(record) for record in prepared.deliveries]
    evidence.sort(
        key=lambda item: (
            item["host_id"],
            item["path"],
            item["sequence"],
            item["evidence_id"],
        )
    )

    chain_status_counts: dict[str, int] = defaultdict(int)
    for chain in chains:
        chain_status_counts[chain["status"]] += 1
    file_status_counts: dict[str, int] = defaultdict(int)
    for file_result in file_results:
        file_status_counts[file_result.status] += 1
    code_counts: dict[str, int] = defaultdict(int)
    for issue in ordered_issues:
        code_counts[issue.code] += 1

    report: dict[str, Any] = {
        "schema_version": 1,
        "application": "config-forensics",
        "files": files,
        "evidence": evidence,
        "chains": chains,
        "drift_points": drift_points,
        "event_clusters": clusters,
        "chain_breaks": chain_breaks,
        "tampered": copy.deepcopy(prepared.tampered),
        "conflicts": copy.deepcopy(prepared.conflicts),
        "errors": [issue.to_dict() for issue in ordered_issues],
        "summary": {
            "file_count": len(files),
            "evidence_delivery_count": sum(len(item.records) for item in file_results),
            "unique_evidence_count": len(prepared.deliveries),
            "chain_count": len(chains),
            "drift_point_count": len(drift_points),
            "event_cluster_count": len(clusters),
            "cross_host_event_cluster_count": sum(
                1 for cluster in clusters if cluster["cross_host"]
            ),
            "chain_break_count": len(chain_breaks),
            "tampered_count": len(prepared.tampered),
            "conflict_count": len(prepared.conflicts),
            "file_status_counts": dict(sorted(file_status_counts.items())),
            "chain_status_counts": dict(sorted(chain_status_counts.items())),
            "error_code_counts": dict(sorted(code_counts.items())),
        },
    }
    return Adjudication(report=report, graph=graph)
