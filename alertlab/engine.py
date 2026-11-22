from __future__ import annotations

import math
from typing import Any, Iterable

from .rules import SEVERITY_RANK, MergedRule
from .topology import Topology
from .util import canonical_json, sha256_json


def _event_view(event: dict[str, Any], rule: MergedRule) -> dict[str, Any]:
    matched = sorted(set(event.get("labels", {})) & set(rule.silence_labels))
    return {
        "event_uid": event["event_uid"],
        "seq": event["seq"],
        "instance_id": event["instance_id"],
        "rule_name": event["rule_name"],
        "severity": event["severity"],
        "logical_clock_ms": event["event_ts_ms"],
        "metric_value": event["metric_value"],
        "labels": dict(sorted(event.get("labels", {}).items())),
        "silenced": bool(matched),
        "silence_labels_matched": matched,
    }


def select_representative(
    events: list[dict[str, Any]], rule: MergedRule, topology: Topology
) -> dict[str, Any]:
    def key(event: dict[str, Any]) -> tuple[Any, ...]:
        view = _event_view(event, rule)
        return (
            1 if view["silenced"] else 0,
            -SEVERITY_RANK[event["severity"]],
            -SEVERITY_RANK[rule.severity],
            -topology.instance_weight[event["instance_id"]],
            event["event_ts_ms"],
            event["seq"],
            event["event_uid"],
        )

    chosen = min(events, key=key)
    return _event_view(chosen, rule)


def build_cluster(
    events: list[dict[str, Any]],
    rule: MergedRule,
    topology: Topology,
    *,
    window_ms: int,
) -> dict[str, Any]:
    ordered = sorted(
        events, key=lambda event: (event["event_ts_ms"], event["seq"], event["event_uid"])
    )
    event_uids = sorted(event["event_uid"] for event in ordered)
    cluster_id = sha256_json(
        {
            "schema": "alertlab.cluster/v1",
            "rule_name": rule.rule_name,
            "service": rule.service,
            "window_ms": window_ms,
            "event_uids": event_uids,
        }
    )
    representative = select_representative(ordered, rule, topology)
    silence_suppressed = 0
    total_silenced = 0
    for event in ordered:
        view = _event_view(event, rule)
        if view["silenced"]:
            total_silenced += 1
            if event["event_uid"] != representative["event_uid"]:
                silence_suppressed += 1
    suppressed_count = max(0, len(ordered) - 1)
    return {
        "schema": "alertlab.cluster/v1",
        "cluster_id": cluster_id,
        "rule_name": rule.rule_name,
        "service": rule.service,
        "rule_source_version": rule.source_version,
        "rule_generation": rule.generation,
        "rule_effective_at_ms": rule.effective_at_ms,
        "window_ms": window_ms,
        "first_event_ms": ordered[0]["event_ts_ms"],
        "last_event_ms": ordered[-1]["event_ts_ms"],
        "occurrence_count": len(ordered),
        "affected_instances": sorted({event["instance_id"] for event in ordered}),
        "event_uids": event_uids,
        "events": [_event_view(event, rule) for event in ordered],
        "representative": representative,
        "severity": rule.severity,
        "rule_silence_labels": list(rule.silence_labels),
        "suppressed_count": suppressed_count,
        "silence_suppressed_count": silence_suppressed,
        "dedup_suppressed_count": suppressed_count - silence_suppressed,
        "silenced_event_count": total_silenced,
        "instance_weights": {
            instance_id: topology.instance_weight[instance_id]
            for instance_id in sorted({event["instance_id"] for event in ordered})
        },
    }


def cluster_events(
    events: Iterable[dict[str, Any]],
    rules: dict[str, MergedRule],
    topology: Topology,
    *,
    window_ms: int,
    injected_clock_ms: int,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        rule = rules.get(event["rule_name"])
        if rule is None or rule.tombstone:
            continue
        if event.get("late", 0):
            continue
        if event["event_ts_ms"] > injected_clock_ms:
            continue
        if event["instance_id"] not in rule.instances:
            continue
        if event["event_ts_ms"] < rule.effective_at_ms:
            continue
        grouped.setdefault((rule.service, rule.rule_name), []).append(event)

    clusters: list[dict[str, Any]] = []
    for (_, rule_name), group_events in sorted(grouped.items()):
        ordered = sorted(
            group_events,
            key=lambda event: (event["event_ts_ms"], event["seq"], event["event_uid"]),
        )
        current: list[dict[str, Any]] = []
        last_timestamp: int | None = None
        for event in ordered:
            if (
                last_timestamp is not None
                and event["event_ts_ms"] - last_timestamp > window_ms
            ):
                clusters.append(
                    build_cluster(current, rules[rule_name], topology, window_ms=window_ms)
                )
                current = []
                last_timestamp = None
            current.append(event)
            last_timestamp = event["event_ts_ms"]
        if current:
            clusters.append(
                build_cluster(current, rules[rule_name], topology, window_ms=window_ms)
            )
    clusters.sort(
        key=lambda cluster: (
            cluster["service"],
            cluster["rule_name"],
            cluster["first_event_ms"],
            cluster["cluster_id"],
        )
    )
    return clusters


def _signal_events_for_instance(
    events: Iterable[dict[str, Any]],
    instance_id: str,
    lower_ms: int,
    upper_ms: int,
) -> list[dict[str, Any]]:
    result = [
        event
        for event in events
        if event["instance_id"] == instance_id
        and not event.get("late", 0)
        and event.get("eligible_for_signal", 1)
        and lower_ms <= event["event_ts_ms"] <= upper_ms
    ]
    result.sort(
        key=lambda event: (
            event["event_ts_ms"],
            event["seq"],
            event["event_uid"],
        )
    )
    return result


def root_candidates(
    cluster: dict[str, Any],
    rule: MergedRule,
    topology: Topology,
    events: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    service_paths = topology.upstream_closure(
        rule.service,
        override_upstream=(
            rule.upstream_dependencies if rule.source_version == "v2" else None
        ),
    )
    lower = cluster["first_event_ms"] - cluster["window_ms"]
    upper = cluster["last_event_ms"] + cluster["window_ms"]
    representative_ts = cluster["representative"]["logical_clock_ms"]
    closure_instances = {
        instance_id
        for service_name in service_paths
        for instance_id, _ in topology.services[service_name].instances
    }
    all_signal_instances = sorted(
        {
            event["instance_id"]
            for event in events
            if not event.get("late", 0)
            and event.get("eligible_for_signal", 1)
            and event["instance_id"] in closure_instances
            and lower <= event["event_ts_ms"] <= upper
        }
    )
    candidates: list[dict[str, Any]] = []
    for service, (distance, path) in service_paths.items():
        topology_service = topology.services[service]
        for instance_id, weight in topology_service.instances:
            signals = _signal_events_for_instance(events, instance_id, lower, upper)
            time_diff = (
                signals[0]["event_ts_ms"] - representative_ts if signals else None
            )
            candidates.append(
                {
                    "schema": "alertlab.root_candidate/v1",
                    "candidate_id": f"{service}/{instance_id}",
                    "service": service,
                    "instance_id": instance_id,
                    "fault_domain": topology_service.fault_domain,
                    "weight": weight,
                    "dependency_distance": distance,
                    "dependency_path": list(path),
                    "has_signal": bool(signals),
                    "time_diff_ms": time_diff,
                    "earliest_signal_ms": (
                        signals[0]["event_ts_ms"] if signals else None
                    ),
                    "participating_instances": all_signal_instances,
                    "candidate_signal_instances": sorted(
                        {signal["instance_id"] for signal in signals}
                    ),
                    "signals": [
                        {
                            "event_uid": signal["event_uid"],
                            "rule_name": signal["rule_name"],
                            "logical_clock_ms": signal["event_ts_ms"],
                            "severity": signal["severity"],
                        }
                        for signal in signals
                    ],
                    "evidence": {
                        "affected_instances": cluster["affected_instances"],
                        "dependency_path": list(path),
                        "time_diff_ms": time_diff,
                        "weight": weight,
                        "sort_order": [
                            "has_signal_desc",
                            "time_diff_ms_asc",
                            "weight_desc",
                            "dependency_distance_asc",
                            "service_asc",
                            "instance_asc",
                        ],
                    },
                }
            )

    def sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        time_diff = candidate["time_diff_ms"]
        return (
            0 if candidate["has_signal"] else 1,
            time_diff if time_diff is not None else math.inf,
            -candidate["weight"],
            candidate["dependency_distance"],
            candidate["service"],
            candidate["instance_id"],
        )

    candidates.sort(key=sort_key)
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
        candidate["cluster_id"] = cluster["cluster_id"]
    return candidates


def candidate_hash(candidate: dict[str, Any]) -> str:
    return sha256_json(candidate)


def cluster_hash(cluster: dict[str, Any]) -> str:
    return sha256_json(cluster)


def clone_json(value: Any) -> Any:
    import json

    return json.loads(canonical_json(value))
