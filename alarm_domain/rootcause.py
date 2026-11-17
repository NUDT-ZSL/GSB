"""Deterministic in-cluster root-cause ranking and noise folding."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping

from .models import (
    SEVERITY_RANK,
    AlarmEvent,
    Cluster,
    Rule,
    Topology,
)
from .topology import TopologyGraph
from .util import canonical_json, sha256_json


def _candidate_reason(candidate: dict[str, object]) -> str:
    return (
        f"in_degree={candidate['in_degree']}; "
        f"severity={candidate['severity']}; "
        f"earliest_ts={candidate['earliest_ts']}; "
        "tie_break=service_name_ascending"
    )


def derive_root_causes(
    cluster: Cluster,
    events: Iterable[AlarmEvent],
    rules: Mapping[str, Rule] | Iterable[Rule],
    topology: Topology,
) -> dict[str, object]:
    if not isinstance(rules, Mapping):
        rules = {rule.rule_id: rule for rule in rules}
    events_by_id = {event.event_id: event for event in events}
    members = [events_by_id[event_id] for event_id in cluster.members]
    graph = TopologyGraph(topology)
    service_events: dict[str, list[AlarmEvent]] = defaultdict(list)
    for event in members:
        service_events[event.service].append(event)
    cluster_services = set(cluster.services)

    candidates: list[dict[str, object]] = []
    for service in cluster.services:
        service_members = sorted(
            service_events[service], key=lambda event: (event.timestamp, event.event_id)
        )
        severity = max(
            (rules[event.rule_id].severity for event in service_members),
            key=lambda value: SEVERITY_RANK[value],
        )
        candidate = {
            "service": service,
            "in_degree": graph.indegree(service),
            "cluster_in_degree": graph.induced_indegree(service, cluster_services),
            "severity": severity,
            "earliest_ts": service_members[0].timestamp,
            "support_alerts": [event.event_id for event in service_members],
            "support_rules": sorted({event.rule_id for event in service_members}),
        }
        candidate["reason"] = _candidate_reason(candidate)
        candidates.append(candidate)
    candidates.sort(
        key=lambda candidate: (
            -int(candidate["in_degree"]),
            -SEVERITY_RANK[str(candidate["severity"])],
            int(candidate["earliest_ts"]),
            str(candidate["service"]),
        )
    )
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank

    root = candidates[0]
    root_service = str(root["service"])
    retained: list[dict[str, object]] = []
    folded: list[dict[str, object]] = []
    for event in sorted(members, key=lambda item: (item.timestamp, item.event_id)):
        event_projection = {
            "event_id": event.event_id,
            "rule_id": event.rule_id,
            "service": event.service,
            "timestamp": event.timestamp,
            "value": event.value,
            "severity": rules[event.rule_id].severity,
        }
        if event.service != root_service and graph.upstream_of(event.service, root_service):
            folded.append(
                {
                    **event_projection,
                    "triggered_by_root_candidate": root_service,
                    "folded_into": root_service,
                    "reason": (
                        f"propagation: {event.service} has a dependency path to "
                        f"root candidate {root_service}"
                    ),
                }
            )
        else:
            retained.append(event_projection)

    return {
        "cluster_id": cluster.cluster_id,
        "cluster": cluster.to_dict(),
        "root_cause": {
            "selected_service": root_service,
            "rank": 1,
            "reason": (
                f"selected rank 1: in_degree={root['in_degree']}, "
                f"severity={root['severity']}, earliest_ts={root['earliest_ts']}; "
                "final tie-break is service name ascending"
            ),
            "candidates": candidates,
        },
        "deduplicated": {
            "cluster_id": cluster.cluster_id,
            "root_candidate": root_service,
            "retained_alerts": retained,
            "folded_alerts": folded,
            "retained_count": len(retained),
            "folded_count": len(folded),
            "summary": {
                "service": root_service,
                "severity": root["severity"],
                "start_ts": cluster.start_ts,
                "end_ts": cluster.end_ts,
                "member_count": len(members),
                "suppressed_count": len(folded),
            },
        },
    }


def analyze_clusters(
    clusters: Iterable[Cluster],
    events: Iterable[AlarmEvent],
    rules: Mapping[str, Rule] | Iterable[Rule],
    topology: Topology,
) -> list[dict[str, object]]:
    event_tuple = tuple(events)
    analyses = [
        derive_root_causes(cluster, event_tuple, rules, topology) for cluster in clusters
    ]
    analyses.sort(key=lambda item: (item["cluster"]["start_ts"], item["cluster_id"]))
    return analyses


def analysis_hash(analyses: Iterable[Mapping[str, object]]) -> str:
    return sha256_json(list(analyses))


def analysis_bytes(analyses: Iterable[Mapping[str, object]]) -> bytes:
    return canonical_json(list(analyses)).encode("utf-8")
