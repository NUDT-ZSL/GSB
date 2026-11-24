"""Host-local process lineage, event clustering and window diagnostics."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

from .codes import Code
from .errors import HostTimeout
from .models import Alert, BoundAlert, Cluster
from .util import cluster_id_for


def _canonical_cycle(sequence: list[str]) -> tuple[str, ...]:
    core = sequence[:-1] if len(sequence) > 1 and sequence[0] == sequence[-1] else sequence
    if not core:
        return tuple(sequence)
    rotations = [
        tuple(core[index:] + core[:index])
        for index in range(len(core))
    ]
    best = min(rotations)
    return best + (best[0],)


class _DisjointSet:
    def __init__(self, nodes: Iterable[str]):
        self.parent = {node: node for node in nodes}
        self.rank = {node: 0 for node in nodes}

    def find(self, node: str) -> str:
        parent = self.parent[node]
        while parent != self.parent[parent]:
            parent = self.parent[parent]
        while node != parent:
            next_node = self.parent[node]
            self.parent[node] = parent
            node = next_node
        return parent

    def union(self, left: str, right: str) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left == root_right:
            return
        if self.rank[root_left] < self.rank[root_right]:
            root_left, root_right = root_right, root_left
        self.parent[root_right] = root_left
        if self.rank[root_left] == self.rank[root_right]:
            self.rank[root_left] += 1


class ProcessLineage:
    """Undirected lineage connectivity plus deterministic directed cycle scan."""

    def __init__(
        self,
        alerts: Iterable[Alert],
        *,
        deadline: float | None = None,
    ):
        self.deadline = deadline
        nodes: set[str] = set()
        adjacency: dict[str, set[str]] = {}
        for index, alert in enumerate(alerts):
            if index % 256 == 0:
                _check_deadline(deadline)
            nodes.add(alert.process_guid)
            if alert.parent_process_guid:
                nodes.add(alert.parent_process_guid)
                adjacency.setdefault(alert.process_guid, set()).add(
                    alert.parent_process_guid
                )
        self.nodes = tuple(sorted(nodes))
        self.adjacency = {
            node: tuple(sorted(adjacency.get(node, ()))) for node in self.nodes
        }
        self._dsu = _DisjointSet(self.nodes)
        for index, (child, parents) in enumerate(self.adjacency.items()):
            if index % 256 == 0:
                _check_deadline(deadline)
            for parent in parents:
                self._dsu.union(child, parent)
        self.cycles = self._find_cycles(deadline)

    def reachable(self, left: str, right: str) -> bool:
        if left == right:
            return True
        if left not in self._dsu.parent or right not in self._dsu.parent:
            return False
        return self._dsu.find(left) == self._dsu.find(right)

    def _find_cycles(
        self, deadline: float | None
    ) -> tuple[tuple[str, ...], ...]:
        state: dict[str, int] = {}
        cycles: set[tuple[str, ...]] = set()
        for start in self.nodes:
            if state.get(start, 0) != 0:
                continue
            path: list[str] = []
            positions: dict[str, int] = {}
            stack: list[tuple[str, int]] = [(start, 0)]
            while stack:
                _check_deadline(deadline)
                node, neighbour_index = stack[-1]
                if state.get(node, 0) == 0:
                    state[node] = 1
                    positions[node] = len(path)
                    path.append(node)
                neighbours = self.adjacency.get(node, ())
                if neighbour_index >= len(neighbours):
                    state[node] = 2
                    positions.pop(node, None)
                    path.pop()
                    stack.pop()
                    continue
                neighbour = neighbours[neighbour_index]
                stack[-1] = (node, neighbour_index + 1)
                if state.get(neighbour, 0) == 1:
                    begin = positions[neighbour]
                    cycles.add(_canonical_cycle(path[begin:] + [neighbour]))
                elif state.get(neighbour, 0) == 0:
                    stack.append((neighbour, 0))
        return tuple(sorted(cycles))


@dataclass
class _WorkingCluster:
    alerts: list[BoundAlert] = field(default_factory=list)
    processes: set[str] = field(default_factory=set)
    accumulated_error_bound_ms: int = 0
    min_boundary_margin_ms: int | None = None
    boundary_margin_total_ms: int = 0
    boundary_checks: int = 0

    @property
    def representative(self) -> BoundAlert:
        return min(
            self.alerts,
            key=lambda item: (item.alert.alert_ts_ms, item.alert.alert_id),
        )


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.perf_counter() >= deadline:
        raise HostTimeout()


def _window_threshold(bound: BoundAlert, path: str) -> int | float:
    if path == "fixed":
        return bound.version.dedup_window_ms
    if path == "float64":
        # Quantized v1 seconds are a floor.  The reference path evaluates the
        # upper edge of the declared sub-second uncertainty interval.
        return float(
            bound.version.dedup_window_ms + bound.version.window_error_ms
        )
    raise ValueError(f"unknown precision path: {path}")


def _cluster_group(
    *,
    host_id: str,
    merge_key: str,
    alerts: list[BoundAlert],
    path: str,
    lineage: ProcessLineage,
    deadline: float | None,
) -> dict[str, Cluster]:
    ordered = sorted(
        alerts,
        key=lambda item: (item.alert.alert_ts_ms, item.alert.alert_id),
    )
    clusters: list[_WorkingCluster] = []
    for bound in ordered:
        _check_deadline(deadline)
        delta = 0
        candidates: list[int] = []
        for index, working in enumerate(clusters):
            representative = working.representative
            delta = bound.alert.alert_ts_ms - representative.alert.alert_ts_ms
            threshold = _window_threshold(bound, path)
            if path == "float64":
                delta_s = (
                    float(bound.alert.alert_ts_ms) / 1000.0
                    - float(representative.alert.alert_ts_ms) / 1000.0
                )
                inside = delta_s <= threshold / 1000.0
            else:
                inside = delta <= threshold
            if not inside:
                continue
            if any(
                lineage.reachable(bound.alert.process_guid, process)
                for process in working.processes
            ):
                candidates.append(index)

        if not candidates:
            working = _WorkingCluster()
            working.alerts.append(bound)
            working.processes.add(bound.alert.process_guid)
            working.accumulated_error_bound_ms = bound.version.window_error_ms
            working.min_boundary_margin_ms = bound.version.dedup_window_ms
            clusters.append(working)
            continue

        if len(candidates) > 1:
            # Stable union of all eligible clusters.  The current alert is
            # reachable to each, so the combined component is valid.
            keep = candidates[0]
            for index in reversed(candidates[1:]):
                merged = clusters.pop(index)
                target = clusters[keep if index > keep else keep]
                target.alerts.extend(merged.alerts)
                target.processes.update(merged.processes)
                target.accumulated_error_bound_ms += (
                    merged.accumulated_error_bound_ms
                )
                if merged.min_boundary_margin_ms is not None:
                    if target.min_boundary_margin_ms is None:
                        target.min_boundary_margin_ms = (
                            merged.min_boundary_margin_ms
                        )
                    else:
                        target.min_boundary_margin_ms = min(
                            target.min_boundary_margin_ms,
                            merged.min_boundary_margin_ms,
                        )
                target.boundary_margin_total_ms += (
                    merged.boundary_margin_total_ms
                )
                target.boundary_checks += merged.boundary_checks
                if index < keep:
                    keep -= 1
            working = clusters[keep]
        else:
            working = clusters[candidates[0]]

        representative = working.representative
        nominal_delta = (
            bound.alert.alert_ts_ms - representative.alert.alert_ts_ms
        )
        margin = max(0, bound.version.dedup_window_ms - nominal_delta)
        if working.min_boundary_margin_ms is None:
            working.min_boundary_margin_ms = margin
        else:
            working.min_boundary_margin_ms = min(
                working.min_boundary_margin_ms, margin
            )
        working.alerts.append(bound)
        working.processes.add(bound.alert.process_guid)
        working.accumulated_error_bound_ms += bound.version.window_error_ms
        working.boundary_margin_total_ms += margin
        working.boundary_checks += 1

    result: dict[str, Cluster] = {}
    for working in clusters:
        representative = working.representative
        ordered_members = sorted(
            working.alerts,
            key=lambda item: (item.alert.alert_ts_ms, item.alert.alert_id),
        )
        cluster_id = cluster_id_for(
            host_id,
            merge_key,
            representative.alert.alert_id,
            tuple(item.alert.alert_id for item in ordered_members),
        )
        cluster = Cluster(
            cluster_id=cluster_id,
            host_id=host_id,
            rule_key=representative.alert.rule_key,
            payload_digest=representative.alert.payload_digest,
            merge_key=merge_key,
            representative_alert_id=representative.alert.alert_id,
            representative_ts_ms=representative.alert.alert_ts_ms,
            alerts=tuple(
                (item.alert.alert_id, item.alert.alert_ts_ms)
                for item in ordered_members
            ),
            window_ms=representative.version.dedup_window_ms,
            window_error_ms=representative.version.window_error_ms,
            accumulated_error_bound_ms=working.accumulated_error_bound_ms,
            min_boundary_margin_ms=(
                working.min_boundary_margin_ms
                if working.min_boundary_margin_ms is not None
                else representative.version.dedup_window_ms
            ),
            boundary_margin_total_ms=working.boundary_margin_total_ms,
            boundary_checks=working.boundary_checks,
            process_guids=tuple(sorted(working.processes)),
        )
        result[cluster_id] = cluster
    return result


@dataclass
class HostClusterResult:
    clusters: dict[str, Cluster]
    window_ambiguities: list[dict]
    proc_cycles: list[dict]
    elapsed_ms: float


def cluster_host(
    *,
    host_id: str,
    all_alerts: list[Alert],
    bound_alerts: list[BoundAlert],
    deadline: float | None,
    include_cycles: bool = True,
) -> HostClusterResult:
    started = time.perf_counter()
    _check_deadline(deadline)
    lineage = ProcessLineage(all_alerts, deadline=deadline)
    groups: dict[str, list[BoundAlert]] = {}
    for bound in bound_alerts:
        groups.setdefault(bound.merge_key, []).append(bound)

    fixed_clusters: dict[str, Cluster] = {}
    float_clusters: dict[str, Cluster] = {}
    for merge_key in sorted(groups):
        fixed_clusters.update(
            _cluster_group(
                host_id=host_id,
                merge_key=merge_key,
                alerts=groups[merge_key],
                path="fixed",
                lineage=lineage,
                deadline=deadline,
            )
        )
        float_clusters.update(
            _cluster_group(
                host_id=host_id,
                merge_key=merge_key,
                alerts=groups[merge_key],
                path="float64",
                lineage=lineage,
                deadline=deadline,
            )
        )

    fixed_for_alert: dict[str, str] = {}
    for cluster_id, cluster in fixed_clusters.items():
        for alert_id, _ in cluster.alerts:
            fixed_for_alert[alert_id] = cluster_id
    float_for_alert: dict[str, str] = {}
    for cluster_id, cluster in float_clusters.items():
        for alert_id, _ in cluster.alerts:
            float_for_alert[alert_id] = cluster_id

    bound_by_id = {item.alert.alert_id: item for item in bound_alerts}
    window_ambiguities: list[dict] = []
    for alert_id in sorted(bound_by_id):
        fixed_id = fixed_for_alert.get(alert_id)
        float_id = float_for_alert.get(alert_id)
        if fixed_id == float_id:
            continue
        bound = bound_by_id[alert_id]
        if bound.version.source != "v1" or bound.version.window_error_ms <= 0:
            continue
        float_cluster = float_clusters.get(float_id or "")
        fixed_cluster = fixed_clusters.get(fixed_id or "")
        if float_cluster is None:
            continue
        representative_id = float_cluster.representative_alert_id
        if representative_id == alert_id:
            continue
        representative = bound_by_id.get(representative_id)
        if representative is None:
            representative = min(
                (
                    item
                    for item in bound_alerts
                    if item.alert.alert_id in float_cluster.alert_ids
                ),
                key=lambda item: (
                    item.alert.alert_ts_ms,
                    item.alert.alert_id,
                ),
            )
        delta_ms = (
            bound.alert.alert_ts_ms - representative.alert.alert_ts_ms
        )
        residual_ms = delta_ms - bound.version.dedup_window_ms
        window_ambiguities.append(
            {
                "code": Code.E_WINDOW_AMBIGUOUS,
                "host_id": host_id,
                "alert_id": alert_id,
                "rule_key": bound.alert.rule_key,
                "fixed_path": "integer_ms",
                "reference_path": "float64_upper_bound",
                "fixed_cluster_id": fixed_id,
                "float_cluster_id": float_id,
                "window_ms": bound.version.dedup_window_ms,
                "window_error_ms": bound.version.window_error_ms,
                "residual_ms": residual_ms,
                "residual_seconds": residual_ms / 1000.0,
                "accumulated_error_bound_ms": (
                    float_cluster.accumulated_error_bound_ms
                ),
                "message": (
                    "v1 second quantization makes the join decision differ "
                    "between integer and float64 reference paths"
                ),
            }
        )

    cycles: list[dict] = []
    if include_cycles:
        for cycle in lineage.cycles:
            cycles.append(
                {
                    "code": Code.E_PROC_CYCLE,
                    "host_id": host_id,
                    "cycle": list(cycle),
                    "process_guids": list(dict.fromkeys(cycle[:-1])),
                    "message": "process parent lineage contains a cycle",
                }
            )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return HostClusterResult(
        clusters=fixed_clusters,
        window_ambiguities=window_ambiguities,
        proc_cycles=cycles,
        elapsed_ms=elapsed_ms,
    )
