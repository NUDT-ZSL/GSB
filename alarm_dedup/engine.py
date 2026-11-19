"""Pure deterministic clustering and root-cause correlation engine."""

from __future__ import annotations

import hashlib
import heapq
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import canonical_json, digest
from .parsing import SEVERITIES, Topology

REPRESENTATIVE_ORDER = [
    "severity_desc",
    "instance_weight_desc",
    "logical_ts_asc",
    "event_seq_asc",
]
CLUSTER_ORDER = ["logical_ts_asc", "event_seq_asc"]
CANDIDATE_ORDER = [
    "has_own_unsuppressed_alert_desc",
    "earliest_alert_ts_asc",
    "instance_weight_desc",
    "causal_distance_asc",
    "instance_id_asc",
]


def _round(value: float) -> float:
    return round(float(value), 6) + 0.0


def make_cluster_key(service: str, rule: str, event_seqs: Iterable[int]) -> str:
    anchor = min(event_seqs)
    raw = f"{service}\x1f{rule}\x1f{anchor}".encode()
    return "C-" + hashlib.sha256(raw).hexdigest()[:24]


def _event_order(event: dict[str, Any]) -> tuple[int, int]:
    return event["logical_ts"], event["event_seq"]


@dataclass(frozen=True)
class ClusterSet:
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class CandidateSet:
    payload: dict[str, Any]
    digest: str
    basis_text: str


def _cluster_basis(
    ordered_events: list[dict[str, Any]],
    suppressed_event_seqs: list[int],
    total_weight: float,
    weighted_severity: float,
) -> dict[str, Any]:
    return {
        "aggregation_order": REPRESENTATIVE_ORDER,
        "weighted_aggregation": {
            "operations": ["numpy.sum(instance_weight)", "numpy.dot(severity_rank, instance_weight)"],
            "total_weight": _round(total_weight),
            "weighted_severity": _round(weighted_severity),
        },
        "ordered_event_seqs": [event["event_seq"] for event in ordered_events],
        "suppressed_event_seqs": suppressed_event_seqs,
    }


def _representative(
    events: list[dict[str, Any]],
    suppressed: set[int],
    weights: dict[str, float],
) -> dict[str, Any] | None:
    eligible = [
        event
        for event in events
        if event["event_seq"] not in suppressed
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda event: (
            -SEVERITIES[event["severity"]],
            -weights[event["instance_id"]],
            event["logical_ts"],
            event["event_seq"],
        ),
    )


def build_cluster_row(
    service: str,
    rule: str,
    ordered_events: list[dict[str, Any]],
    suppressed: set[int],
    weights: dict[str, float],
) -> dict[str, Any]:
    event_seqs = sorted(event["event_seq"] for event in ordered_events)
    cluster_key = make_cluster_key(service, rule, event_seqs)
    representative = _representative(ordered_events, suppressed, weights)
    suppressed_seqs = sorted(event["event_seq"] for event in ordered_events if event["event_seq"] in suppressed)
    severity_vector = np.asarray([SEVERITIES[event["severity"]] for event in ordered_events], dtype=np.int64)
    weight_vector = np.asarray([weights[event["instance_id"]] for event in ordered_events], dtype=np.float64)
    total_weight = float(np.sum(weight_vector))
    weighted_severity = float(np.dot(severity_vector, weight_vector))
    basis = _cluster_basis(ordered_events, suppressed_seqs, total_weight, weighted_severity)
    representative_summary = None
    if representative is not None:
        representative_summary = {
            "event_seq": representative["event_seq"],
            "instance_id": representative["instance_id"],
            "severity": representative["severity"],
            "logical_ts": representative["logical_ts"],
            "metric_value": representative["metric_value"],
        }
    core = {
        "cluster_key": cluster_key,
        "service": service,
        "rule": rule,
        "start_ts": min(event["logical_ts"] for event in ordered_events),
        "end_ts": max(event["logical_ts"] for event in ordered_events),
        "event_seqs": event_seqs,
        "ordered_event_seqs": [event["event_seq"] for event in ordered_events],
        "event_count": len(ordered_events),
        "suppressed_count": len(suppressed_seqs),
        "suppressed_event_seqs": suppressed_seqs,
        "representative_event_seq": representative["event_seq"] if representative else None,
        "representative": representative_summary,
        "basis": basis,
    }
    return {
        **core,
        "digest": digest(core),
        "basis_json": canonical_json(basis),
    }


def build_all_clusters(
    events: list[dict[str, Any]],
    window_ms: int,
    suppressed: set[int],
    topology: Topology,
) -> dict[str, dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        groups.setdefault((event["service"], event["rule"]), []).append(event)
    weights = {instance_id: instance.weight for instance_id, instance in topology.instances.items()}
    result: dict[str, dict[str, Any]] = {}
    for (service, rule), group_events in sorted(groups.items()):
        ordered = sorted(group_events, key=_event_order)
        current: list[dict[str, Any]] = []
        for event in ordered:
            if current and event["logical_ts"] - current[-1]["logical_ts"] > window_ms:
                row = build_cluster_row(service, rule, current, suppressed, weights)
                result[row["cluster_key"]] = row
                current = []
            current.append(event)
        if current:
            row = build_cluster_row(service, rule, current, suppressed, weights)
            result[row["cluster_key"]] = row
    return result


def recluster_groups(
    events: list[dict[str, Any]],
    groups: set[tuple[str, str]],
    window_ms: int,
    suppressed: set[int],
    topology: Topology,
) -> dict[str, dict[str, Any]]:
    selected = [event for event in events if (event["service"], event["rule"]) in groups]
    return build_all_clusters(selected, window_ms, suppressed, topology)


def upstream_closure(topology: Topology, service: str) -> dict[str, int]:
    distances = {service: 0}
    queue: list[tuple[int, str]] = [(0, service)]
    while queue:
        distance, current = heapq.heappop(queue)
        if distances.get(current, distance) != distance:
            continue
        for dependency in sorted(topology.services[current].upstream_dependencies):
            candidate_distance = distance + 1
            if dependency not in distances or candidate_distance < distances[dependency]:
                distances[dependency] = candidate_distance
                heapq.heappush(queue, (candidate_distance, dependency))
    return distances


def downstream_closure(topology: Topology, service: str) -> list[str]:
    reverse = topology.downstream
    seen = {service}
    queue = [service]
    while queue:
        current = queue.pop(0)
        for dependent in reverse[current]:
            if dependent not in seen:
                seen.add(dependent)
                queue.append(dependent)
    return sorted(seen)


def _candidate_basis_text(candidate: dict[str, Any]) -> str:
    return (
        f"rank={candidate['rank']};"
        f"has_own_unsuppressed_alert={int(candidate['has_own_unsuppressed_alert'])};"
        f"earliest_alert_ts={candidate['earliest_alert_ts']};"
        f"own_alert_count={candidate['own_alert_count']};"
        f"instance_weight={candidate['instance_weight']:.6f};"
        f"causal_distance={candidate['causal_distance']};"
        f"affected_instance_count={len(candidate['affected_instance_ids'])}"
    )


def build_candidate_set(
    topology: Topology,
    cluster: dict[str, Any],
    all_events: list[dict[str, Any]],
    suppressed: set[int],
    cluster_by_event: dict[int, str],
) -> CandidateSet:
    target_service = cluster["service"]
    distances = upstream_closure(topology, target_service)
    candidate_services = sorted(distances, key=lambda service: (distances[service], service))
    instances_by_service: dict[str, list[Any]] = {}
    for instance in topology.instances.values():
        instances_by_service.setdefault(instance.service, []).append(instance)
    for values in instances_by_service.values():
        values.sort(key=lambda instance: instance.instance_id)

    unsuppressed_events = [event for event in all_events if event["event_seq"] not in suppressed]
    events_by_instance: dict[str, list[dict[str, Any]]] = {}
    for event in unsuppressed_events:
        events_by_instance.setdefault(event["instance_id"], []).append(event)
    for values in events_by_instance.values():
        values.sort(key=_event_order)

    upstream_instance_ids = sorted(
        instance.instance_id
        for instance in topology.instances.values()
        if instance.service in distances
    )
    candidate_rows: list[dict[str, Any]] = []
    for service in candidate_services:
        for instance in instances_by_service.get(service, []):
            own_events = events_by_instance.get(instance.instance_id, [])
            earliest = own_events[0]["logical_ts"] if own_events else None
            latest = own_events[-1]["logical_ts"] if own_events else None
            observations = [
                {
                    "event_seq": event["event_seq"],
                    "logical_ts": event["logical_ts"],
                    "severity": event["severity"],
                    "metric_value": event["metric_value"],
                    "rule": event["rule"],
                    "cluster_key": cluster_by_event.get(event["event_seq"]),
                }
                for event in own_events
            ]
            candidate_rows.append(
                {
                    "candidate_instance_id": instance.instance_id,
                    "service": instance.service,
                    "fault_domain": instance.fault_domain,
                    "instance_weight": instance.weight,
                    "causal_distance": distances[service],
                    "has_own_unsuppressed_alert": bool(own_events),
                    "own_alert_count": len(own_events),
                    "earliest_alert_ts": earliest,
                    "latest_alert_ts": latest,
                    "affected_instance_ids": upstream_instance_ids,
                    "observations": observations,
                }
            )

    candidate_rows.sort(
        key=lambda candidate: (
            0 if candidate["has_own_unsuppressed_alert"] else 1,
            candidate["earliest_alert_ts"] if candidate["earliest_alert_ts"] is not None else 2**63 - 1,
            -candidate["instance_weight"],
            candidate["causal_distance"],
            candidate["candidate_instance_id"],
        )
    )
    for rank, candidate in enumerate(candidate_rows, start=1):
        candidate["rank"] = rank
        candidate["is_primary"] = rank == 1
        candidate["basis_text"] = _candidate_basis_text(candidate)

    affected_union = list(upstream_instance_ids)
    payload = {
        "cluster_key": cluster["cluster_key"],
        "target_service": target_service,
        "upstream_services": [
            {"service": service, "causal_distance": distances[service]}
            for service in candidate_services
        ],
        "upstream_instance_ids": upstream_instance_ids,
        "affected_instance_ids": affected_union,
        "aggregation_order": CANDIDATE_ORDER,
        "candidates": candidate_rows,
    }
    primary = candidate_rows[0] if candidate_rows else None
    basis_text = (
        f"cluster={cluster['cluster_key']};"
        f"target_service={target_service};"
        f"aggregation_order={','.join(CANDIDATE_ORDER)};"
        f"primary={primary['candidate_instance_id'] if primary else 'none'};"
        f"primary_rank_basis={primary['basis_text'] if primary else 'none'}"
    )
    return CandidateSet(payload, digest(payload), basis_text)


def build_all_candidates(
    topology: Topology,
    clusters: Iterable[dict[str, Any]],
    all_events: list[dict[str, Any]],
    suppressed: set[int],
) -> dict[str, CandidateSet]:
    cluster_list = list(clusters)
    cluster_by_event: dict[int, str] = {}
    for cluster in cluster_list:
        for event_seq in cluster["event_seqs"]:
            cluster_by_event[event_seq] = cluster["cluster_key"]
    return {
        cluster["cluster_key"]: build_candidate_set(
            topology,
            cluster,
            all_events,
            suppressed,
            cluster_by_event,
        )
        for cluster in sorted(cluster_list, key=lambda item: item["cluster_key"])
    }
