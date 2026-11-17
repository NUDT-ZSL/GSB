"""拓扑 / 规则变更集：单调版本演进、受影响簇推导与整体替换视图。

变更集语义（全部强校验，非法变更抛 E_VERSION/E_INPUT）：
* 拓扑变更集包含 removed_edges 与 added_edges（source,target,weight），
  新拓扑版本号必须恰好为旧版本 + 1；
* 规则变更集按 rule_id 替换规则，版本号同样单调 +1。

受影响簇推导（保守、确定）：
* 边变更：簇内任一服务与变更边端点重合，或簇内关联路径用到该边；
* 规则变更：簇内存在 rule_id 被替换的事件，或成员服务在受影响服务集合内。
"""
from __future__ import annotations

import json

from .errors import E_VERSION, ObsDiagError
from .graph import TopologyGraph
from .models import (
    Cluster,
    Rule,
    RuleSet,
    SUPPORTED_METRICS,
    SUPPORTED_OPERATORS,
    SUPPORTED_SEVERITIES,
    Topology,
    VersionDiff,
)


def _load_change_document(path: str) -> dict:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        try:
            document = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ObsDiagError(E_VERSION, f"invalid changeset json: {exc}") from exc
    if not isinstance(document, dict):
        raise ObsDiagError(E_VERSION, "changeset root must be an object")
    return document


def apply_topology_changeset(topology: Topology, path: str) -> tuple[Topology, tuple, tuple]:
    document = _load_change_document(path)
    new_version = document.get("new_version")
    if not isinstance(new_version, int) or new_version != topology.version + 1:
        raise ObsDiagError(
            E_VERSION,
            f"new_version must be exactly old_version+1 ({topology.version + 1})",
        )
    removed = document.get("removed_edges", [])
    added = document.get("added_edges", [])
    if not isinstance(removed, list) or not isinstance(added, list):
        raise ObsDiagError(E_VERSION, "removed_edges/added_edges must be arrays")
    def _normalize(edges: list) -> tuple:
        normalized = []
        for edge in edges:
            if isinstance(edge, dict):
                normalized.append((str(edge["source"]), str(edge["target"]), int(edge["weight"])))
            else:
                normalized.append((str(edge[0]), str(edge[1]), int(edge[2])))
        return tuple(sorted(normalized))

    removed_norm = _normalize(removed)
    added_norm = _normalize(added)

    current = {(s, t): w for s, t, w in topology.edges}
    for source, target, _weight in removed_norm:
        if (source, target) not in current:
            raise ObsDiagError(E_VERSION, f"removed edge not present: {source}->{target}")
    for source, target, weight in added_norm:
        if source not in topology.services or target not in topology.services:
            raise ObsDiagError(E_VERSION, f"added edge endpoint unknown: {source}->{target}")
        if weight <= 0:
            raise ObsDiagError(E_VERSION, f"added edge weight must be positive: {source}->{target}")
        if (source, target) in current and (source, target) not in {
            (s, t) for s, t, _ in removed_norm
        }:
            raise ObsDiagError(E_VERSION, f"edge already exists: {source}->{target}")

    for source, target, _weight in removed_norm:
        current.pop((source, target), None)
    for source, target, weight in added_norm:
        current[(source, target)] = weight
    new_edges = [(source, target, weight) for (source, target), weight in current.items()]
    new_topology = Topology.build(new_version, list(topology.services), new_edges)
    return new_topology, removed_norm, added_norm


def apply_rule_changeset(ruleset: RuleSet, topology: Topology, path: str) -> tuple[RuleSet, tuple]:
    document = _load_change_document(path)
    new_version = document.get("new_version")
    if not isinstance(new_version, int) or new_version != ruleset.version + 1:
        raise ObsDiagError(
            E_VERSION,
            f"new_version must be exactly old_version+1 ({ruleset.version + 1})",
        )
    replacements = document.get("replace_rules", [])
    if not isinstance(replacements, list) or not replacements:
        raise ObsDiagError(E_VERSION, "replace_rules must be a non-empty array")
    by_id = {rule.rule_id: rule for rule in ruleset.rules}
    replaced_ids: list[str] = []
    for raw in replacements:
        rule_id = raw.get("rule_id")
        if rule_id not in by_id:
            raise ObsDiagError(E_VERSION, f"unknown rule_id in changeset: {rule_id}")
        service = raw.get("service", by_id[rule_id].service)
        metric = raw.get("metric", by_id[rule_id].metric)
        operator = raw.get("operator", by_id[rule_id].operator)
        threshold = raw.get("threshold", by_id[rule_id].threshold)
        severity = raw.get("severity", by_id[rule_id].severity)
        if service not in topology.services:
            raise ObsDiagError(E_VERSION, f"unknown service for rule {rule_id}: {service}")
        if metric not in SUPPORTED_METRICS:
            raise ObsDiagError(E_VERSION, f"unsupported metric for rule {rule_id}: {metric}")
        if operator not in SUPPORTED_OPERATORS:
            raise ObsDiagError(E_VERSION, f"unsupported operator for rule {rule_id}: {operator}")
        if severity not in SUPPORTED_SEVERITIES:
            raise ObsDiagError(E_VERSION, f"unsupported severity for rule {rule_id}: {severity}")
        by_id[rule_id] = Rule(
            rule_id=rule_id,
            service=service,
            metric=metric,
            operator=operator,
            threshold=float(threshold),
            severity=severity,
        )
        replaced_ids.append(rule_id)
    return RuleSet.build(new_version, list(by_id.values())), tuple(sorted(replaced_ids))


def affected_by_edge_change(
    clusters: tuple[Cluster, ...],
    old_graph: TopologyGraph,
    removed_edges: tuple,
    added_edges: tuple,
) -> tuple[str, ...]:
    endpoints = {endpoint for edge in (*removed_edges, *added_edges) for endpoint in edge[:2]}
    changed_pairs = {(source, target) for source, target, _ in (*removed_edges, *added_edges)}
    affected: set[str] = set()
    for cluster in clusters:
        services = {event.service for event in cluster.members}
        if services & endpoints:
            affected.add(cluster.cluster_id)
            continue
        for _service, links in cluster.reach_paths:
            if any((link.source, link.target) in changed_pairs for link in links):
                affected.add(cluster.cluster_id)
                break
    return tuple(sorted(affected))


def affected_by_rule_change(
    clusters: tuple[Cluster, ...], replaced_rule_ids: tuple, topology: Topology
) -> tuple[str, ...]:
    rule_id_set = set(replaced_rule_ids)
    affected: set[str] = set()
    for cluster in clusters:
        if any(event.rule_id in rule_id_set for event in cluster.members):
            affected.add(cluster.cluster_id)
    return tuple(sorted(affected))


def build_version_diff(
    old_clusters: tuple[Cluster, ...],
    new_clusters: tuple[Cluster, ...],
    old_topology_version: int,
    new_topology_version: int,
    old_rule_version: int,
    new_rule_version: int,
    removed_edges: tuple = (),
    added_edges: tuple = (),
    replaced_rules: tuple = (),
    predicted_invalid: tuple[str, ...] = (),
) -> VersionDiff:
    old_by_hash = {cluster.content_hash: cluster for cluster in old_clusters}
    new_hashes = {cluster.content_hash for cluster in new_clusters}
    retained = tuple(
        sorted(
            cluster.cluster_id
            for cluster in old_clusters
            if cluster.content_hash in new_hashes
        )
    )
    invalidated = tuple(
        sorted(
            cluster.cluster_id
            for cluster in old_clusters
            if cluster.content_hash not in new_hashes
        )
    )
    new_ids = tuple(
        sorted(
            cluster.cluster_id
            for cluster in new_clusters
            if cluster.content_hash not in old_by_hash
        )
    )
    # 预测受影响集合必须覆盖全部实际失效簇，否则视为演进推导错误
    if set(invalidated) - set(predicted_invalid):
        raise ObsDiagError(
            E_VERSION,
            "unpredicted invalidated clusters: "
            + ",".join(sorted(set(invalidated) - set(predicted_invalid))),
        )
    return VersionDiff(
        old_topology_version=old_topology_version,
        new_topology_version=new_topology_version,
        old_rule_version=old_rule_version,
        new_rule_version=new_rule_version,
        removed_edges=tuple(sorted(removed_edges)),
        added_edges=tuple(sorted(added_edges)),
        replaced_rules=tuple(sorted(replaced_rules)),
        invalidated_cluster_ids=invalidated,
        retained_cluster_ids=retained,
        new_cluster_ids=new_ids,
    )
