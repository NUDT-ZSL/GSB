from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .hashing import sha256_object


@dataclass(frozen=True)
class WindowPlan:
    origin_ms: int
    length_ms: int
    slide_ms: int
    slots: tuple[int, ...]
    slot_to_window_no: dict[int, int]

    def window_no(self, slot: int) -> int:
        return self.slot_to_window_no[slot]


def build_window_plan(traces: list[dict[str, Any]], metric_definitions: dict[str, Any]) -> WindowPlan:
    origin_ms = int(metric_definitions.get("origin_ms", 0))
    length_ms = int(metric_definitions["window_length_ms"])
    slide_ms = int(metric_definitions["slide_ms"])
    clocks = [int(item["logical_clock_ms"]) for item in traces]
    min_clock = min(clocks) - origin_ms
    max_clock = max(clocks) - origin_ms
    first_slot = (min_clock - length_ms) // slide_ms + 1
    last_slot = max_clock // slide_ms
    slots = tuple(range(first_slot, last_slot + 1))
    mapping = {slot: index + 1 for index, slot in enumerate(slots)}
    return WindowPlan(origin_ms, length_ms, slide_ms, slots, mapping)


def owner_slot_for_clock(clock_ms: int, plan: WindowPlan) -> int:
    return (int(clock_ms) - plan.origin_ms) // plan.slide_ms


def window_bounds(slot: int, plan: WindowPlan) -> tuple[int, int]:
    start = plan.origin_ms + slot * plan.slide_ms
    return start, start + plan.length_ms


def assign_windows(
    traces: list[dict[str, Any]], plan: WindowPlan
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assignments: list[dict[str, Any]] = []
    owner_groups: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
    for trace in traces:
        slot = owner_slot_for_clock(trace["logical_clock_ms"], plan)
        window_no = plan.window_no(slot)
        assignment = {
            "request_seq": trace["request_seq"],
            "window_no": window_no,
            "slot_index": slot,
            "service_name": trace["service_name"],
            "instance_id": trace["instance_id"],
            "logical_clock_ms": trace["logical_clock_ms"],
        }
        assignments.append(assignment)
        key = (window_no, trace["service_name"], trace["instance_id"])
        owner_groups.setdefault(key, []).append(trace)

    summaries: list[dict[str, Any]] = []
    for (window_no, service_name, instance_id), records in sorted(
        owner_groups.items(), key=lambda item: (item[0][0], item[0][1], item[0][2])
    ):
        slot = next(slot for slot, number in plan.slot_to_window_no.items() if number == window_no)
        start_ms, end_ms = window_bounds(slot, plan)
        seqs = sorted(record["request_seq"] for record in records)
        summaries.append(
            {
                "window_no": window_no,
                "slot_index": slot,
                "service_name": service_name,
                "instance_id": instance_id,
                "window_start_ms": start_ms,
                "window_end_ms": end_ms,
                "ownership_start_ms": plan.origin_ms + slot * plan.slide_ms,
                "ownership_end_ms": plan.origin_ms + (slot + 1) * plan.slide_ms,
                "request_count": len(seqs),
                "request_seq_min": seqs[0],
                "request_seq_max": seqs[-1],
                "request_seqs": seqs,
                "request_set_digest": sha256_object(seqs),
            }
        )

    assigned_seqs = sorted(item["request_seq"] for item in assignments)
    expected_seqs = sorted(item["request_seq"] for item in traces)
    if assigned_seqs != expected_seqs or len(set(assigned_seqs)) != len(expected_seqs):
        raise AssertionError("window assignment is not an exact non-overlapping cover")
    return sorted(assignments, key=lambda item: item["request_seq"]), summaries


def aggregate_for_window(
    traces: list[dict[str, Any]],
    plan: WindowPlan,
    slot: int,
    service_name: str,
    instance_id: str,
) -> dict[str, Any]:
    start_ms, end_ms = window_bounds(slot, plan)
    records = [
        trace
        for trace in traces
        if trace["service_name"] == service_name
        and trace["instance_id"] == instance_id
        and start_ms <= trace["logical_clock_ms"] < end_ms
    ]
    records.sort(key=lambda item: (item["logical_clock_ms"], item["request_seq"]))
    durations = sorted(record["duration_ms"] for record in records)
    count = len(records)
    success_count = sum(1 for record in records if record["success"])
    dropped_count = sum(1 for record in records if record["dropped"])
    error_count = sum(1 for record in records if not record["success"] or record["dropped"])
    total_duration = sum((record["duration_ms"] for record in records), Decimal(0))
    seqs = [record["request_seq"] for record in records]
    return {
        "window_no": plan.window_no(slot),
        "slot_index": slot,
        "service_name": service_name,
        "instance_id": instance_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "request_count": count,
        "success_count": success_count,
        "dropped_count": dropped_count,
        "error_count": error_count,
        "total_duration_ms": total_duration,
        "durations": durations,
        "request_seqs": seqs,
        "request_seq_min": min(seqs) if seqs else None,
        "request_seq_max": max(seqs) if seqs else None,
        "aggregate_digest": sha256_object(
            {
                "seqs": sorted(seqs),
                "count": count,
                "total_duration": total_duration,
                "success": success_count,
                "dropped": dropped_count,
            }
        ),
    }


def all_observations(
    input_data: Any, plan: WindowPlan
) -> list[dict[str, Any]]:
    service_keys = [
        (service["service_name"], service["instance_id"], service)
        for service in input_data.scenario["services"]
    ]
    observations: list[dict[str, Any]] = []
    for slot in plan.slots:
        for service_name, instance_id, service in sorted(service_keys, key=lambda item: item[1]):
            aggregate = aggregate_for_window(
                input_data.traces, plan, slot, service_name, instance_id
            )
            if aggregate["request_count"] == 0:
                continue
            aggregate["service_level"] = service["service_level"]
            aggregate["expected_rate_rps"] = service["expected_rate_rps"]
            observations.append(aggregate)
    observations.sort(key=lambda item: (item["window_no"], item["instance_id"]))
    return observations
