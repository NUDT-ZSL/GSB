"""告警簇关联、根因候选推导与衍生告警折叠。

关联规则（与输入顺序无关）：
1. 事件按稳定键排序后依次处理；
2. 两个事件时间差 <= window_ms，且服务处于同一依赖链（拓扑任一方向可达），
   则并入同一簇；并查集保证窗口重叠时传递合并；
3. 簇标识由簇内成员稳定排序后的 member_key 序列做 SHA-256 推导，
   与拓扑/规则版本共同绑定，跨版本结论不会混用。

根因规则（确定性）：按 (簇内入度降序, 严重级降序, 最早时间升序, 服务名升序)
排序候选服务；非根因且沿依赖边可到达根因服务的事件折叠为传播性衍生告警。
"""
from __future__ import annotations

import hashlib

from .graph import TopologyGraph
from .models import (
    AlertEvent,
    Cluster,
    FoldedAlert,
    PathLink,
    RootCause,
    RuleSet,
    SEVERITY_RANK,
    Topology,
    canonical_dumps,
)


class _UnionFind:
    def __init__(self, size: int):
        self._parent = list(range(size))

    def find(self, index: int) -> int:
        while self._parent[index] != index:
            self._parent[index] = self._parent[self._parent[index]]
            index = self._parent[index]
        return index

    def union(self, left: int, right: int) -> None:
        root_left, root_right = self.find(left), self.find(right)
        if root_left != root_right:
            # 以较小下标为根，保持并查集结构确定
            if root_left > root_right:
                root_left, root_right = root_right, root_left
            self._parent[root_right] = root_left


def _content_hash(events: list[AlertEvent]) -> str:
    ordered = sorted(events, key=lambda event: event.member_key())
    payload = canonical_dumps(
        {"members": [list(event.member_key()) for event in ordered]}
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cluster_id(events: list[AlertEvent], topology_version: int, rule_version: int) -> str:
    # 簇标识只由成员内容推导；拓扑/规则版本通过 Cluster 上的独立字段绑定，
    # 使同一成员集合在版本演进后仍可被 content_hash/cluster_id 追踪与比对。
    return "CL-" + _content_hash(events)[:12]


def _build_reach_paths(
    events: list[AlertEvent], graph: TopologyGraph
) -> tuple[tuple[str, tuple[PathLink, ...]], ...]:
    services = sorted({event.service for event in events})
    records: list[tuple[str, tuple[PathLink, ...]]] = []
    for service in services:
        paths: list[tuple[PathLink, ...]] = []
        for other in services:
            if other == service:
                continue
            path = graph.shortest_path(service, other)
            if path:
                paths.append(path)
        if paths:
            # 同一服务的多条路径按其规范化 JSON 排序，顺序可复现
            paths.sort(key=lambda path: canonical_dumps([link.__dict__ for link in path]))
            joined: list[PathLink] = []
            for path in paths:
                joined.extend(path)
            records.append((service, tuple(joined)))
    return tuple(sorted(records))


def _derive_root_causes(
    events: list[AlertEvent], graph: TopologyGraph
) -> tuple[RootCause, tuple[FoldedAlert, ...]]:
    member_services = frozenset(event.service for event in events)
    per_service: dict[str, list[AlertEvent]] = {}
    for event in events:
        per_service.setdefault(event.service, []).append(event)

    scored: list[tuple] = []
    for service in sorted(member_services):
        service_events = sorted(per_service[service], key=lambda item: item.stable_key())
        in_degree = graph.local_in_degree(service, member_services)
        max_severity = max(event.severity for event in service_events)
        earliest = min(event.timestamp_ms for event in service_events)
        scored.append((-in_degree, -SEVERITY_RANK[max_severity], earliest, service))
    scored.sort()

    root_causes: list[RootCause] = []
    for rank, (neg_degree, neg_sev, earliest, service) in enumerate(scored, start=1):
        support = tuple(sorted(per_service[service], key=lambda item: item.stable_key()))
        reasons = (
            f"in_degree={-neg_degree} within cluster",
            f"max_severity={support and max(event.severity for event in support)}",
            f"earliest_timestamp_ms={earliest}",
            f"service_id={service}",
        )
        root_causes.append(
            RootCause(
                rank=rank,
                service=service,
                in_degree=-neg_degree,
                severity=max(event.severity for event in support),
                earliest_ts=earliest,
                supporting_events=support,
                reasons=reasons,
            )
        )

    primary_service = root_causes[0].service
    folded: list[FoldedAlert] = []
    for event in sorted(events, key=lambda item: item.stable_key()):
        if event.service == primary_service:
            continue
        path = graph.shortest_path(event.service, primary_service)
        if path is None:
            continue
        chain = "->".join([event.service] + [link.target for link in path])
        folded.append(
            FoldedAlert(
                event=event,
                triggered_by_service=primary_service,
                reason=(
                    f"propagated along dependency chain {chain}; "
                    f"root candidate {primary_service} rank=1"
                ),
            )
        )
    folded.sort(key=lambda item: item.event.stable_key())
    return tuple(root_causes), tuple(folded)


def correlate(
    events: tuple[AlertEvent, ...],
    topology: Topology,
    ruleset: RuleSet,
    window_ms: int,
) -> tuple[Cluster, ...]:
    if window_ms < 0:
        raise ValueError("window_ms must be >= 0")
    graph = TopologyGraph(topology)
    ordered = sorted(events, key=lambda event: event.stable_key())
    union_find = _UnionFind(len(ordered))

    for left in range(len(ordered)):
        for right in range(left + 1, len(ordered)):
            if ordered[right].timestamp_ms - ordered[left].timestamp_ms > window_ms:
                break
            if ordered[left].service == ordered[right].service:
                union_find.union(left, right)
                continue
            # 同一依赖链：两个服务必须处于同一有向可达路径（任一方向）
            path = graph.shortest_path(ordered[left].service, ordered[right].service)
            if path is None:
                path = graph.shortest_path(ordered[right].service, ordered[left].service)
            if path is not None:
                union_find.union(left, right)

    groups: dict[int, list[AlertEvent]] = {}
    for index, event in enumerate(ordered):
        groups.setdefault(union_find.find(index), []).append(event)

    clusters: list[Cluster] = []
    for member_events in groups.values():
        member_events.sort(key=lambda event: event.stable_key())
        root_causes, folded = _derive_root_causes(member_events, graph)
        clusters.append(
            Cluster(
                cluster_id=_cluster_id(member_events, topology.version, ruleset.version),
                content_hash=_content_hash(member_events),
                topology_version=topology.version,
                rule_version=ruleset.version,
                window_ms=window_ms,
                members=tuple(member_events),
                reach_paths=_build_reach_paths(member_events, graph),
                root_causes=root_causes,
                folded=folded,
            )
        )
    clusters.sort(key=lambda cluster: (cluster.members[0].timestamp_ms, cluster.cluster_id))
    return tuple(clusters)
