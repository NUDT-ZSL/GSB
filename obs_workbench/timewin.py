from __future__ import annotations

import json
from typing import Any, Iterable

from .canonical import canonical_json, digest
from .samples import SEVERITY_RANK
from .topology import Topology


def event_seq_from_row(row: Any) -> int | str:
    if int(row["seq_kind"]) == 0:
        return int(row["seq_num"])
    return str(row["seq_text"])


def window_key(instance_id: str, rule_name: str, start_ms: int, end_ms: int) -> str:
    return f"{instance_id}\u0000{rule_name}\u0000{start_ms}\u0000{end_ms}"


def window_bounds(occurred_clock_ms: int, window_ms: int) -> tuple[int, int]:
    start = (occurred_clock_ms // window_ms) * window_ms
    return start, start + window_ms - 1


def closes_at(watermark_ms: int, end_ms: int, allowed_lateness_ms: int) -> bool:
    return watermark_ms > end_ms + allowed_lateness_ms


def is_late(occurred_clock_ms: int, watermark_ms: int | None) -> bool:
    return watermark_ms is not None and occurred_clock_ms < watermark_ms


def event_to_dict(row: Any) -> dict[str, Any]:
    return {
        "_db_key": str(row["event_seq"]),
        "arrived_clock_ms": int(row["arrived_clock_ms"]),
        "event_seq": event_seq_from_row(row),
        "instance_id": str(row["instance_id"]),
        "membership": str(row["membership"]) if "membership" in row.keys() else "ON_TIME",
        "metric_value": float(row["metric_value"]),
        "occurred_clock_ms": int(row["occurred_clock_ms"]),
        "rule_name": str(row["rule_name"]),
        "severity": str(row["severity"]),
        "silenced_maintenance": bool(row["silenced_maintenance"]),
        "silenced_standalone": bool(row["silenced_standalone"]),
    }


def _dedupe_fingerprint(event: dict[str, Any]) -> str:
    return canonical_json(
        [
            event["occurred_clock_ms"],
            event["severity"],
            float(event["metric_value"]),
        ]
    )


def suppression_reasons(event: dict[str, Any], topology: Topology) -> list[str]:
    instance = topology.instances[event["instance_id"]]
    reasons: list[str] = []
    if event["silenced_standalone"]:
        reasons.append("event:silenced_maintenance")
    for silence in sorted(topology.silences.values(), key=lambda item: item.silence_id):
        if silence.matches(
            instance.instance_id,
            instance.service_name,
            event["rule_name"],
            event["occurred_clock_ms"],
        ):
            reasons.append(f"silence:{silence.silence_id}:{silence.kind}")
    return sorted(set(reasons))


def _representative_sort_key(event: dict[str, Any], topology: Topology) -> tuple[int, float, int, int, str]:
    instance = topology.instances[event["instance_id"]]
    raw = event["event_seq"]
    if isinstance(raw, int):
        seq_key = (0, raw, "")
    else:
        seq_key = (1, 0, str(raw))
    return (
        -SEVERITY_RANK[event["severity"]],
        -float(instance.weight),
        seq_key[0],
        seq_key[1],
        seq_key[2],
    )


def build_evidence_chain(
    topology: Topology,
    representative: dict[str, Any],
) -> list[dict[str, Any]]:
    source_instance = topology.instances[representative["instance_id"]]
    records: list[dict[str, Any]] = []
    for path in topology.upstream_paths(source_instance.service_name):
        service_name = path[-1]
        if len(path) == 1:
            candidates = (source_instance,)
            kind = "source"
        else:
            candidates = topology.instances_for_service(service_name)
            kind = "upstream"
        for instance in candidates:
            records.append(
                {
                    "depth": len(path) - 1,
                    "edge_path": [
                        f"{path[index]}->{path[index + 1]}" for index in range(len(path) - 1)
                    ],
                    "failure_domain": instance.failure_domain,
                    "instance_id": instance.instance_id,
                    "kind": kind,
                    "path": list(path),
                    "representative_event_seq": representative["event_seq"],
                    "service_name": service_name,
                    "source_instance_id": source_instance.instance_id,
                    "topology_version": topology.version,
                }
            )
    records.sort(
        key=lambda item: (
            0 if item["kind"] == "source" else 1,
            item["depth"],
            tuple(item["path"]),
            item["instance_id"],
        )
    )
    return records


def build_window_revision(
    *,
    topology: Topology,
    window: dict[str, Any],
    events: Iterable[dict[str, Any]],
    closed_at_watermark_ms: int,
    late_event_seqs: set[str],
    include_late_closed: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    member_events = [
        event
        for event in events
        if include_late_closed or event["membership"] != "LATE_CLOSED"
    ]
    member_events.sort(
        key=lambda event: (
            event["occurred_clock_ms"],
            int(event["event_seq"]) if isinstance(event["event_seq"], int) else 10**30,
            str(event["event_seq"]),
        )
    )

    suppression: dict[str, list[str]] = {
        str(event["event_seq"]): suppression_reasons(event, topology) for event in member_events
    }
    suppressed_count = sum(1 for reasons in suppression.values() if reasons)
    fingerprints = {_dedupe_fingerprint(event) for event in member_events}

    eligible = [
        event
        for event in member_events
        if not suppression[str(event["event_seq"])]
    ]
    eligible.sort(key=lambda event: _representative_sort_key(event, topology))
    representative: dict[str, Any] | None = None
    seen_fingerprints: set[str] = set()
    for event in eligible:
        fingerprint = _dedupe_fingerprint(event)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        instance = topology.instances[event["instance_id"]]
        representative = {
            "arrived_clock_ms": event["arrived_clock_ms"],
            "event_seq": event["event_seq"],
            "instance_id": event["instance_id"],
            "instance_weight": instance.weight,
            "metric_value": event["metric_value"],
            "occurred_clock_ms": event["occurred_clock_ms"],
            "rule_name": event["rule_name"],
            "severity": event["severity"],
            "suppressed_reasons": [],
        }
        break

    if not member_events:
        conclusion = "EMPTY"
    elif representative is None:
        conclusion = "SUPPRESSED_ONLY"
    else:
        conclusion = "ACTIONABLE"

    included_late_values = {
        event["event_seq"]
        for event in member_events
        if str(event["_db_key"]) in late_event_seqs
    }
    included_late = sorted(
        included_late_values,
        key=lambda value: (0, value, "") if isinstance(value, int) else (1, 0, str(value)),
    )
    result: dict[str, Any] = {
        "closed_at_watermark_ms": closed_at_watermark_ms,
        "conclusion": conclusion,
        "deduplicated_event_count": len(fingerprints),
        "duplicate_event_count": len(member_events) - len(fingerprints),
        "first_occurred_clock_ms": (
            min(event["occurred_clock_ms"] for event in member_events) if member_events else None
        ),
        "input_mode": "reaudit_with_late_ledger" if include_late_closed else "closure",
        "last_occurred_clock_ms": (
            max(event["occurred_clock_ms"] for event in member_events) if member_events else None
        ),
        "late_event_count": len(included_late),
        "raw_event_count": len(member_events),
        "reevaluated_late_event_seqs": included_late,
        "representative_event": representative,
        "suppressed_count": suppressed_count,
        "window_end_ms": window["window_end_ms"],
        "window_key": window["window_key"],
        "window_start_ms": window["window_start_ms"],
        "instance_id": window["instance_id"],
        "rule_name": window["rule_name"],
    }
    evidence = build_evidence_chain(topology, representative) if representative else []
    revision_digest = digest({"evidence_chain": evidence, "result": result})
    return result, evidence, revision_digest


def parse_revision_jsons(result_json: str, evidence_json: str) -> tuple[Any, Any]:
    try:
        return json.loads(result_json), json.loads(evidence_json)
    except json.JSONDecodeError:
        return None, None
