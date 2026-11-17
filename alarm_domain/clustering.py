"""Input-order-independent alarm clustering by time and topology."""

from __future__ import annotations

from typing import Iterable

from .errors import spec_error
from .models import AlarmEvent, Cluster, Topology
from .topology import TopologyGraph
from .util import canonical_json, is_int, sha256_json


def _cluster_id(members: tuple[AlarmEvent, ...]) -> str:
    material = [[event.event_id, event.timestamp] for event in members]
    return "cl-" + sha256_json(material)[:16]


def cluster_events(
    events: Iterable[AlarmEvent],
    topology: Topology,
    window_ms: int,
) -> tuple[Cluster, ...]:
    """Cluster events using pairwise time neighborhoods and topology reachability.

    Events are first ordered by ``(timestamp, event_id)``.  Streaming-style
    union-find then makes the result independent of caller traversal order.
    """

    if not is_int(window_ms) or window_ms <= 0:
        raise spec_error(
            "window_ms must be a positive integer",
            file="<api>",
            line=1,
            detail={"window_ms": window_ms},
        )
    ordered = tuple(sorted(events, key=lambda event: (event.timestamp, event.event_id)))
    if not ordered:
        return ()
    seen_ids: set[str] = set()
    for event in ordered:
        if event.event_id in seen_ids:
            raise spec_error(
                f"duplicate event_id {event.event_id}",
                file="<api>",
                line=1,
                detail={"event_id": event.event_id},
            )
        seen_ids.add(event.event_id)

    graph = TopologyGraph(topology)
    parent = list(range(len(ordered)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    for left_index, left_event in enumerate(ordered):
        for right_index in range(left_index + 1, len(ordered)):
            right_event = ordered[right_index]
            if right_event.timestamp - left_event.timestamp > window_ms:
                break
            if left_event.service == right_event.service or graph.comparable(
                left_event.service, right_event.service
            ):
                union(left_index, right_index)

    groups: dict[int, list[AlarmEvent]] = {}
    for index, event in enumerate(ordered):
        groups.setdefault(find(index), []).append(event)

    clusters: list[Cluster] = []
    for members_list in groups.values():
        members = tuple(
            sorted(members_list, key=lambda event: (event.timestamp, event.event_id))
        )
        services = tuple(sorted({event.service for event in members}))
        clusters.append(
            Cluster(
                cluster_id=_cluster_id(members),
                members=tuple(event.event_id for event in members),
                services=services,
                start_ts=members[0].timestamp,
                end_ts=members[-1].timestamp,
            )
        )
    clusters.sort(key=lambda cluster: (cluster.start_ts, cluster.end_ts, cluster.cluster_id))
    return tuple(clusters)


def cluster_summary(clusters: tuple[Cluster, ...], window_ms: int) -> dict[str, object]:
    summaries = [cluster.to_dict() for cluster in clusters]
    payload: dict[str, object] = {
        "schema": "alarm-clusters-v1",
        "window_ms": window_ms,
        "clusters": summaries,
    }
    return {
        **payload,
        "summary_hash": sha256_json(payload),
    }


def cluster_summary_bytes(clusters: tuple[Cluster, ...], window_ms: int) -> bytes:
    return canonical_json(cluster_summary(clusters, window_ms)).encode("utf-8")
